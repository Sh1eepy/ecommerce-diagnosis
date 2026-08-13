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
    append_tool_result,
    build_initial_messages,
    truncate_context,
)
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
            "conclusion": str(report or ""), "suggestions": [],
        }
    return {
        "facts": report.get("facts") or [],
        "analysis": report.get("analysis") or {"key_finding": "", "impact": ""},
        "conclusion": report.get("conclusion") or "",
        "suggestions": report.get("suggestions") or [],
    }


def _partial_report() -> dict:
    return {
        "facts": [],
        "analysis": {"key_finding": "达到步数上限，证据收集不完整", "impact": "报告仅供参考"},
        "conclusion": "诊断未完成：达到 max_steps/预算上限。请扩大上限或缩小问题范围后重试。",
        "suggestions": [],
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
        tool_logs: list[dict] = []
        used_tools: list[str] = []

        for step in range(1, settings.AGENT_MAX_STEPS + 1):
            steps = step
            if tokens_in > settings.AGENT_TOKEN_BUDGET:
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

            if dtype == "tool_call":
                tool, args = decision.get("tool", ""), decision.get("args") or {}
                result = self.registry.execute(tool, args, run_id=run_id, step=step)
                tool_calls += 1
                workflow.observe(tool, result)
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
                append_tool_result(messages, tool, result)

            elif dtype == "final":
                missing = workflow.missing_critical()
                if missing and nudges < 2:
                    nudges += 1
                    messages.append({
                        "role": "user",
                        "content": (
                            f"关键环节未覆盖（缺少工具: {', '.join(missing)}）。"
                            "请先调用这些工具收集证据后再输出 final。"
                            f"当前进度: {workflow.progress_text()}"
                        ),
                    })
                else:
                    report = _normalize_report(decision.get("report"))
                    stop_reason = STOP_FINAL
                    break

            else:
                messages.append({
                    "role": "user",
                    "content": "输出格式错误：必须输出合法 JSON，type 只能是 tool_call 或 final。",
                })

            messages = truncate_context(messages)

        else:  # for-else：正常走完循环未 break
            stop_reason = STOP_MAX_STEPS
            report = _partial_report()

        if report is None:
            report = _partial_report()

        duration_ms = int((time.perf_counter() - started) * 1000)
        self._persist(
            run_id, item_id, start, end, anomaly_id, stop_reason,
            steps, tool_calls, tokens_in, tokens_out,
            llm_calls, llm_duration_ms, duration_ms, error, report, tool_logs,
        )
        result = {
            "run_id": run_id,
            "item_id": item_id,
            "window": [str(start), str(end)],
            "anomaly_id": anomaly_id,
            "status": "ok" if not error else "error",
            "stop_reason": stop_reason,
            "report": report,
            "steps": steps,
            "tool_calls": tool_calls,
            "tools_used": used_tools,
            "model": self.llm.model,
        }
        try:
            send_diagnosis_alert(result)
        except Exception as e:  # noqa: BLE001  告警失败不影响诊断结果
            log_agent_step(run_id, steps, "alert_error", str(e)[:200])
        return result

    def _persist(self, run_id, item_id, start, end, anomaly_id, stop_reason,
                 steps, tool_calls, tokens_in, tokens_out,
                 llm_calls, llm_duration_ms, duration_ms, error, report, tool_logs):
        with write_session() as s:
            s.add(AgentRun(
                run_id=run_id, item_id=item_id,
                window_start=start, window_end=end,
                anomaly_id=anomaly_id,
                status="succeeded" if not error else "error",
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
