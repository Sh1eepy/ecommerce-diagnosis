"""链路追踪：run_id 贯穿 + 三类 JSONL 日志。

logs/
├── agent_runs/{run_id}.jsonl   每一步 Agent 决策 / LLM 调用
├── tool_calls/{run_id}.jsonl   每次工具调用
└── sql_logs/{run_id}.jsonl     每条 SQL 语句(参数化)与耗时

用途：线上"变慢/不准"排查时，凭 run_id 可完整重放一次诊断过程。
"""
from __future__ import annotations

import contextvars
import json
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_run_id", default=None
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def set_run_id(run_id: str) -> str:
    """设置当前上下文（线程/异步任务）的 run_id。"""
    _run_id_var.set(run_id)
    return run_id


def current_run_id() -> str | None:
    return _run_id_var.get()


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _append(category: str, run_id: str, obj: dict) -> None:
    run_id = run_id or current_run_id() or "cli"
    base = Path(settings.LOG_DIR) / category
    base.mkdir(parents=True, exist_ok=True)
    line = {"ts": _ts(), "run_id": run_id, **obj}
    with open(base / f"{run_id}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")



def log_agent_step(
    run_id: str,
    step: int,
    kind: str,
    detail: str,
    duration_ms: float | None = None,
    tokens: dict | None = None,
) -> None:
    _append("agent_runs", run_id, {
        "step": step,
        "kind": kind,
        "detail": detail,
        "duration_ms": duration_ms,
        "tokens": tokens,
    })


def log_tool_call(
    run_id: str,
    step: int,
    tool: str,
    args: dict,
    result_summary: str,
    rows: int,
    latency_ms: float,
    status: str = "ok",
) -> None:
    _append("tool_calls", run_id, {
        "step": step,
        "tool": tool,
        "args": args,
        "result_summary": result_summary[:500],
        "rows": rows,
        "latency_ms": latency_ms,
        "status": status,
    })


def log_sql(run_id: str, statement: str, params: dict, duration_ms: float, rows: int) -> None:
    _append("sql_logs", run_id, {
        "statement": statement,
        "params": params,
        "duration_ms": duration_ms,
        "rows": rows,
    })
