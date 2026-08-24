"""Agent Loop：LLM 决策 → Tool 执行 → 结果回填 → 循环直到 final。

安全上限（防止失控）：
- max_steps：最大循环轮数
- 单步 timeout + LLM 调用超时/重试
- token 预算：累计输入超预算即终止
- 工具白名单：只能执行注册表内 Tool
- Context 裁剪：防止无限膨胀
"""
from __future__ import annotations

import json
import time
from datetime import date

from app.agent import default_registry
from app.agent.context import (
    append_investigation_state,
    append_tool_result,
    build_initial_messages,
    truncate_context,
)
from app.agent.investigation import InvestigationState
from app.agent.quality import evaluate_report
from app.agent.tool import ToolRegistry
from app.agent.workflow import Workflow
from app.alerting import send_diagnosis_alert
from app.config import settings
from app.db import write_session
from app.llm import get_llm, LLMClient
from app.models import AgentRun, DiagnosticReport, ToolCallLog
from app.tracing import log_agent_step, new_run_id, set_run_id

STOP_FINAL = "final"
STOP_MAX_STEPS = "max_steps"
STOP_LLM_ERROR = "llm_error"
STOP_TOKEN_BUDGET = "token_budget"
STOP_TOTAL_TIMEOUT = "total_timeout"
STOP_INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def _parse_decision(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1]) if len(lines) >= 2 else content.strip("`")
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"type": "invalid", "raw": content[:200]}


def _normalize_report(report) -> dict:
    if not isinstance(report, dict):
        return {
            "facts": [], "analysis": {"key_finding": "", "impact": ""},
            "conclusion": str(report or ""), "suggestions": [], "hypotheses": [],
        }
    return {
        "facts": report.get("facts") or [],
        "analysis": report.get("analysis") or {"key_finding": "", "impact": ""},
        "conclusion": report.get("conclusion") or "",
        "suggestions": report.get("suggestions") or [],
        "hypotheses": report.get("hypotheses") or [],
    }


def _partial_report() -> dict:
    return {
        "facts": [],
        "analysis": {"key_finding": "达到步数上限，证据收集不完整", "impact": "报告仅供参考"},
        "conclusion": "诊断未完成：达到 max_steps/预算上限。请扩大上限或缩小问题范围后重试。",
        "suggestions": [],
        "hypotheses": [],
    }


class Agent:
    def __init__(self, llm: LLMClient | None = None, registry: ToolRegistry | None = None):
        self.llm = llm or get_llm()
        self.registry = registry or default_registry()

    def run(
        self,
        item_id: int,
        start: date,
        end: date,
        anomaly: str = "",
        run_id: str | None = None,
        anomaly_id: int | None = None,
    ) -> dict:
        run_id = run_id or new_run_id()
        set_run_id(run_id)
        started = time.perf_counter()

        workflow = Workflow()
        investigation = InvestigationState()
        messages = build_initial_messages(
            self.registry.describe(), item_id, str(start), str(end), anomaly
        )
        steps = 0
        tool_calls = 0
        tokens_in = 0
        tokens_out = 0
        llm_calls = 0
        llm_duration_ms = 0
        nudges = 0
        stop_reason = ""
        report = None
        error = ""
        quality = {"passed": False, "scores": {}, "errors": ["诊断未完成"]}
        tool_logs: list[dict] = []
        used_tools: list[str] = []

        for step in range(1, settings.AGENT_MAX_STEPS + 1):
            steps = step
            elapsed = time.perf_counter() - started
            if elapsed >= settings.AGENT_TOTAL_TIMEOUT_SECONDS:
                stop_reason = STOP_TOTAL_TIMEOUT
                break
            if tokens_in >= settings.AGENT_TOKEN_BUDGET:
                stop_reason = STOP_TOKEN_BUDGET
                break

            t0 = time.perf_counter()
            try:
                resp = self.llm.chat(messages, timeout=settings.AGENT_STEP_TIMEOUT_SECONDS)
            except Exception as e:  # noqa: BLE001
                error = f"LLM 调用失败: {type(e).__name__}: {e}"
                log_agent_step(run_id, step, "llm_error", error)
                stop_reason = STOP_LLM_ERROR
                break
            latency = (time.perf_counter() - t0) * 1000.0
            llm_calls += 1
            llm_duration_ms += int(latency)
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            log_agent_step(
                run_id, step, "llm", resp.content[:300], round(latency, 2),
                {"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out},
            )

            decision = _parse_decision(resp.content)
            dtype = decision.get("type")
            # 保存模型上一轮决策，避免上下文只有一串 user/tool 消息而没有决策轨迹。
            messages.append({"role": "assistant", "content": resp.content})
            hypothesis_id = investigation.apply_updates(decision)

            if dtype == "tool_call":
                tool, args = decision.get("tool", ""), decision.get("args") or {}
                result = self.registry.execute(tool, args, run_id=run_id, step=step)
                tool_calls += 1
                workflow.observe(tool, result)
                call_id = investigation.observe_tool(step, tool, result, hypothesis_id)
                if call_id:
                    used_tools.append(tool)
                tool_logs.append({
                    "run_id": run_id,
                    "step": step,
                    "tool": tool,
                    "args_json": json.dumps(args, ensure_ascii=False)[:2000],
                    "result_summary": result.get("text", "")[:500],
                    "rows": result.get("rows", 0),
                    "latency_ms": result.get("_meta", {}).get("latency_ms", 0.0),
                    "status": "ok" if result.get("ok") else "error",
                })
                append_tool_result(
                    messages, tool, result, call_id=call_id,
                    evidence=investigation.evidence.get(call_id) if call_id else None,
                )
                append_investigation_state(messages, investigation.snapshot())

            elif dtype == "final":
                candidate = _normalize_report(decision.get("report"))
                quality = evaluate_report(candidate, investigation.evidence)
                blockers = []
                if not workflow.can_finalize():
                    blockers.append("尚无任何成功的工具证据")
                blockers.extend(quality["errors"])
                if blockers and nudges < 2:
                    nudges += 1
                    messages.append({
                        "role": "user",
                        "content": (
                            "final 未通过证据质量门槛，请修正后重试。问题："
                            + "；".join(blockers[:8])
                            + f"。当前进度: {workflow.progress_text()}。"
                            "可以继续调用最能区分现有假设的工具；不要求固定调用全部工具。"
                        ),
                    })
                else:
                    if blockers:
                        report = _partial_report()
                        report["analysis"]["key_finding"] = "证据质量门槛未通过"
                        report["analysis"]["quality_errors"] = blockers
                        stop_reason = STOP_INSUFFICIENT_EVIDENCE
                    else:
                        report = candidate
                        stop_reason = STOP_FINAL
                    break

            else:
                raw = str(decision.get("raw") or "")
                log_agent_step(
                    run_id, step, "invalid_json",
                    f"len={len(raw)} head={raw[:300]} tail={raw[-300:]}",
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "上一响应不是完整合法 JSON，可能被截断。请重新输出更紧凑的单个 JSON；"
                        "reasoning/statement/expected_evidence 各不超过80字，type 只能是 tool_call 或 final。"
                    ),
                })

            messages = truncate_context(messages)

            if time.perf_counter() - started >= settings.AGENT_TOTAL_TIMEOUT_SECONDS:
                stop_reason = STOP_TOTAL_TIMEOUT
                break

        else:  # for-else：正常走完循环未 break
            stop_reason = STOP_MAX_STEPS
            report = _partial_report()

        if report is None:
            report = _partial_report()

        duration_ms = int((time.perf_counter() - started) * 1000)
        run_status = "succeeded" if stop_reason == STOP_FINAL else ("failed" if error else "incomplete")
        self._persist(
            run_id, item_id, start, end, anomaly_id, stop_reason,
            steps, tool_calls, tokens_in, tokens_out,
            llm_calls, llm_duration_ms, duration_ms, error, report, tool_logs, run_status,
        )
        result = {
            "run_id": run_id,
            "item_id": item_id,
            "window": [str(start), str(end)],
            "anomaly_id": anomaly_id,
            "status": "ok" if stop_reason == STOP_FINAL else ("error" if error else "incomplete"),
            "stop_reason": stop_reason,
            "report": report,
            "steps": steps,
            "tool_calls": tool_calls,
            "tools_used": used_tools,
            "investigation": investigation.snapshot(),
            "evidence": investigation.evidence,
            "quality": quality,
            "model": self.llm.model,
        }
        try:
            send_diagnosis_alert(result)
        except Exception as e:  # noqa: BLE001  告警失败不影响诊断结果
            log_agent_step(run_id, steps, "alert_error", str(e)[:200])
        return result

    def _persist(self, run_id, item_id, start, end, anomaly_id, stop_reason,
                  steps, tool_calls, tokens_in, tokens_out,
                  llm_calls, llm_duration_ms, duration_ms, error, report, tool_logs, run_status):
        with write_session() as s:
            s.add(AgentRun(
                run_id=run_id, item_id=item_id,
                window_start=start, window_end=end,
                anomaly_id=anomaly_id,
                status=run_status,
                steps=steps, tool_calls=tool_calls,
                tokens_in=tokens_in, tokens_out=tokens_out,
                llm_calls=llm_calls, llm_duration_ms=llm_duration_ms,
                duration_ms=duration_ms, error=error,
            ))
            for tl in tool_logs:
                s.add(ToolCallLog(**tl))
            s.add(DiagnosticReport(
                run_id=run_id, anomaly_id=anomaly_id, item_id=item_id,
                window_start=start, window_end=end,
                content_json=json.dumps(report, ensure_ascii=False),
                model=self.llm.model,
            ))
            s.commit()
