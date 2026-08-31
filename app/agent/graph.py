"""LangGraph 调查图：显式节点调度，完整业务步骤才写现有 checkpoint。

图运行状态与数据库恢复格式分离，不新增第二套持久化权威来源。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langsmith import tracing_context

from app.agent.context import append_investigation_state, append_tool_result, compact_context
from app.agent.investigation import InvestigationState
from app.agent.quality import evaluate_report
from app.agent.state import FailureInfo, RunBudget
from app.agent.tool import ToolRegistry
from app.agent.workflow import Workflow
from app.llm.base import LLMClient
from app.llm.langchain_adapter import invoke_chat


@dataclass
class LoopState:
    """覆盖式 state channel；不用 append reducer，防止恢复或压缩后消息翻倍。"""

    messages: list[dict] = field(default_factory=list)
    investigation: InvestigationState = field(default_factory=InvestigationState)
    workflow: Workflow = field(default_factory=Workflow)
    steps: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    llm_attempts: int = 0
    llm_duration_ms: int = 0
    nudges: int = 0
    stop_reason: str = ""
    report: dict | None = None
    error: str = ""
    quality: dict = field(default_factory=dict)
    tool_logs: list[dict] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    next_step: int = 1
    resume_at: int = 1
    retry_not_before: float = 0.0
    failure: FailureInfo | None = None
    final_only: bool = False
    output_limit: int = 0
    decision: dict = field(default_factory=dict)
    hypothesis_id: str | None = None


@dataclass(frozen=True)
class InvestigationContext:
    """每次 invoke 独立注入依赖，不进入图状态或 checkpoint。"""

    llm: LLMClient
    registry: ToolRegistry
    budget: RunBudget
    run_id: str
    remaining_seconds: Callable
    check_owner: Callable
    save_step: Callable
    parse_decision: Callable
    normalize_report: Callable
    partial_report: Callable
    log_step: Callable
    clock: object
    max_output_tokens: int
    step_timeout_seconds: float


class InvestigationGraph:
    """可复用的图定义；节点仅从 LangGraph Runtime 获取当前调用依赖。"""

    def __init__(self):
        builder = StateGraph(LoopState, context_schema=InvestigationContext)
        for name in ("prepare", "model", "tools", "review", "invalid", "checkpoint"):
            builder.add_node(name, getattr(self, name))
        builder.add_edge(START, "prepare")
        builder.add_conditional_edges("prepare", lambda s: END if s.stop_reason else "model", [END, "model"])
        builder.add_conditional_edges("model", self.route_decision, [END, "tools", "review", "invalid"])
        for name in ("tools", "review", "invalid"):
            builder.add_conditional_edges(name, lambda s: END if s.stop_reason else "checkpoint", [END, "checkpoint"])
        builder.add_conditional_edges("checkpoint", lambda s: END if s.stop_reason else "prepare", [END, "prepare"])
        # 不启用图级重试/缓存/内存 checkpointer；原 Provider 与带租约的 DB 存档负责这些契约。
        self.compiled = builder.compile(name="ecommerce_diagnosis")

    def invoke(self, state: LoopState, *, context: InvestigationContext) -> LoopState:
        # 一轮业务步骤最多四个图节点；图的 superstep 上限不等于模型次数。
        with tracing_context(enabled=False):
            result = self.compiled.invoke(vars(state), config={
                "recursion_limit": 4 * context.budget.max_steps + 4, "callbacks": [],
            }, context=context)
        return LoopState(**result)

    def describe(self) -> str:
        """本地 Mermaid，不请求远程图片服务，不触发图执行。"""
        return self.compiled.get_graph().draw_mermaid()

    def prepare(self, s: LoopState, runtime: Runtime[InvestigationContext]) -> dict:
        ctx = runtime.context
        if s.next_step > ctx.budget.max_steps:
            s.stop_reason, s.report = "max_steps", ctx.partial_report()
            return vars(s)
        ctx.check_owner()
        s.steps = s.resume_at = s.next_step
        if ctx.remaining_seconds() <= 0:
            s.stop_reason = "total_timeout"
            return vars(s)
        if s.tokens_in + s.tokens_out >= ctx.budget.token_limit:
            s.stop_reason = "token_budget"
            return vars(s)
        s.messages = compact_context(s.messages, s.investigation) if s.investigation.evidence else s.messages
        remaining = ctx.budget.token_limit - s.tokens_in - s.tokens_out
        estimated = ctx.llm.estimate_input_tokens(s.messages)
        s.final_only = bool(s.investigation.evidence) and (
            remaining < 3 * (estimated + ctx.llm.output_token_reserve(ctx.max_output_tokens)))
        if s.final_only:
            s.messages.append({"role": "user", "content":
                "[预算收尾]停止新增工具查询。请用已有事实输出简短 final，明确未知原因和核查建议；"
                "修正全部质量问题，不确定就保留未知。最多3条事实、2条建议。"})
            estimated = ctx.llm.estimate_input_tokens(s.messages)
        s.output_limit = min(ctx.max_output_tokens, remaining - estimated)
        if s.output_limit < ctx.llm.minimum_output_tokens:
            ctx.log_step(ctx.run_id, s.steps, "budget_preflight", json.dumps({
                "remaining": remaining, "estimated_input": estimated, "reason": "cannot_fit_request"}))
            s.stop_reason = "token_budget"
            return vars(s)
        ctx.log_step(ctx.run_id, s.steps, "budget_preflight", json.dumps({
            "remaining": remaining, "estimated_input": estimated,
            "max_output": s.output_limit, "final_only": s.final_only}))
        return vars(s)

    def model(self, s: LoopState, runtime: Runtime[InvestigationContext]) -> dict:
        ctx = runtime.context
        t0 = ctx.clock.perf_counter()
        try:
            resp = invoke_chat(ctx.llm, s.messages,
                               timeout=min(ctx.step_timeout_seconds, ctx.remaining_seconds()),
                               max_tokens=s.output_limit)
        except Exception as e:
            s.failure = FailureInfo.from_exception(e)
            s.llm_attempts += s.failure.attempts
            s.llm_duration_ms += int((ctx.clock.perf_counter() - t0) * 1000)
            s.failure.retryable = s.failure.retryable and s.failure.retry_after_seconds < ctx.remaining_seconds()
            s.retry_not_before = ctx.clock.time() + s.failure.retry_after_seconds if s.failure.retryable else 0.0
            s.error = s.failure.summary()
            ctx.log_step(ctx.run_id, s.steps, "llm_error", s.error)
            s.stop_reason = "llm_error"
            return vars(s)
        latency = (ctx.clock.perf_counter() - t0) * 1000
        s.llm_calls += 1
        s.llm_attempts += resp.attempts
        s.llm_duration_ms += int(latency)
        s.tokens_in += resp.tokens_in
        s.tokens_out += resp.tokens_out
        s.messages.append({"role": "assistant", "content": resp.content})
        if ctx.remaining_seconds() <= 0:
            s.stop_reason = "total_timeout"
            return vars(s)
        if s.tokens_in + s.tokens_out > ctx.budget.token_limit:
            s.stop_reason = "token_budget"
            return vars(s)
        ctx.log_step(ctx.run_id, s.steps, "llm", resp.content[:300], round(latency, 2),
                      {"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out})
        decision = ctx.parse_decision(resp.content)
        s.decision = decision if isinstance(decision, dict) else {"type": "invalid"}
        s.hypothesis_id = s.investigation.apply_updates(s.decision)
        return vars(s)

    @staticmethod
    def route_decision(s: LoopState) -> str:
        if s.stop_reason:
            return END
        return {"tool_call": "tools", "final": "review"}.get(s.decision.get("type"), "invalid")

    def tools(self, s: LoopState, runtime: Runtime[InvestigationContext]) -> dict:
        ctx = runtime.context
        if s.final_only:
            s.stop_reason = "token_budget"
            return vars(s)
        tool, args = s.decision.get("tool", ""), s.decision.get("args") or {}
        ctx.check_owner()
        result = ctx.registry.execute(tool, args, run_id=ctx.run_id, step=s.steps)
        s.tool_calls += 1
        s.workflow.observe(tool, result)
        call_id = s.investigation.observe_tool(s.steps, tool, result, s.hypothesis_id)
        if call_id:
            s.investigation.evidence[call_id]["args"] = args
            s.used_tools.append(tool)
        s.tool_logs.append({
            "run_id": ctx.run_id, "step": s.steps, "tool": tool,
            "args_json": json.dumps(args, ensure_ascii=False)[:2000],
            "result_summary": result.get("text", "")[:500], "rows": result.get("rows", 0),
            "latency_ms": result.get("_meta", {}).get("latency_ms", 0.0),
            "status": "ok" if result.get("ok") else "error",
        })
        append_tool_result(s.messages, tool, result, call_id=call_id,
                           evidence=s.investigation.evidence.get(call_id) if call_id else None)
        append_investigation_state(s.messages, s.investigation.snapshot())
        return vars(s)

    def review(self, s: LoopState, runtime: Runtime[InvestigationContext]) -> dict:
        ctx = runtime.context
        candidate = ctx.normalize_report(s.decision.get("report"))
        s.quality = evaluate_report(candidate, s.investigation.evidence)
        blockers = [] if s.workflow.can_finalize() else ["尚无任何成功的工具证据"]
        blockers.extend(s.quality["errors"])
        # compact_context / checkpoint 已保留上一失败候选及对应修正意见。
        # 新增工具证据后该对消息会被移除，因此相同文字可在新证据下重新评估。
        previous = None
        for i in range(len(s.messages) - 1, 0, -1):
            message = s.messages[i]
            if message.get("role") == "user" and message.get("content", "").startswith("final 未通过"):
                before = s.messages[i - 1]
                if before.get("role") == "assistant":
                    decision = ctx.parse_decision(before.get("content", ""))
                    if isinstance(decision, dict) and decision.get("type") == "final":
                        previous = ctx.normalize_report(decision.get("report"))
                break

        def canonical(value):
            if isinstance(value, dict):
                return {k: canonical(v) for k, v in value.items()}
            if isinstance(value, list):
                return [canonical(v) for v in value]
            return re.sub(r"\s+", "", value) if isinstance(value, str) else value

        no_progress = bool(blockers) and previous is not None and canonical(previous) == canonical(candidate)
        if no_progress:
            ctx.log_step(ctx.run_id, s.steps, "review_no_progress", "失败候选无实质变化，停止继续修正")
        if blockers and not no_progress and s.nudges < 2:
            s.nudges += 1
            s.messages.append({"role": "user", "content": (
                "final 未通过证据质量门槛，请修正后重试。问题：" + "；".join(blockers[:8])
                + f"。当前进度: {s.workflow.progress_text()}。"
                "逐项修改被指出的字段和原句，勿原样重交。"
                + ("本轮仅修正已有报告，不再新增工具查询。" if s.final_only else
                   "可以继续调用最能区分现有假设的工具；不要求固定调用全部工具。"))})
        else:
            if blockers:
                s.report = ctx.partial_report("insufficient_evidence")
                s.report["analysis"]["key_finding"] = (
                    "重复修正未取得进展，已停止继续调用模型" if no_progress else "证据质量门槛未通过")
                s.report["analysis"]["quality_errors"] = blockers
                s.stop_reason = "insufficient_evidence"
            else:
                s.report = candidate
                s.report["analysis"]["evidence_limits"] = s.quality["evidence_limits"]
                s.report["analysis"]["limitations"] = list(dict.fromkeys(
                    s.report["analysis"]["limitations"] + list(s.quality["evidence_limits"].values())))
                s.stop_reason = "final"
            s.resume_at = s.steps + 1
        return vars(s)

    def invalid(self, s: LoopState, runtime: Runtime[InvestigationContext]) -> dict:
        ctx = runtime.context
        raw = str(s.decision.get("raw") or "")
        ctx.log_step(ctx.run_id, s.steps, "invalid_json", f"len={len(raw)} head={raw[:300]} tail={raw[-300:]}")
        s.messages.append({"role": "user", "content": (
            "上一响应不是完整合法 JSON，可能被截断。请重新输出更紧凑的单个 JSON；"
            "reasoning/statement/expected_evidence 各不超过80字，type 只能是 tool_call 或 final。")})
        return vars(s)

    def checkpoint(self, s: LoopState, runtime: Runtime[InvestigationContext]) -> dict:
        ctx = runtime.context
        s.messages = compact_context(s.messages, s.investigation)
        s.next_step = s.resume_at = s.steps + 1
        s.retry_not_before = 0.0
        ctx.save_step(s)  # 仍是带事务内 lease 校验的完整业务步骤存档。
        if ctx.remaining_seconds() <= 0:
            s.stop_reason = "total_timeout"
        return vars(s)
