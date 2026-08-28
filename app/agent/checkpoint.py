"""Agent Checkpoint：每个完整步骤后保存，崩溃重试时从最后一致状态恢复。"""
from __future__ import annotations

import json
from typing import Any

from app.db import write_session
from app.agent.state import CheckpointState, FailureInfo
from app.models import AgentCheckpoint, utcnow
from app.task_ownership import lock_owned_task


def load_checkpoint(run_id: str) -> AgentCheckpoint | None:
    with write_session() as session:
        return session.query(AgentCheckpoint).filter_by(run_id=run_id).first()


def save_checkpoint(
    *, run_id: str, task_id: int | None, item_id: int, start, end,
    anomaly_id: int | None, step: int, state: dict[str, Any],
    status: str = "active", result: dict | None = None,
) -> None:
    if status not in {"active", "waiting_retry", "completed", "failed", "stopped"}:
        raise ValueError("未知 checkpoint 状态")
    if status == "waiting_retry":
        if not isinstance(result, dict) or result.get("status") != "error":
            raise ValueError("等待重试必须保存结构化失败结果")
        failure = FailureInfo.model_validate(result.get("failure"))
        if not failure.retryable or failure.kind != "retryable":
            raise ValueError("不可重试的结果不能保存为等待恢复")
    elif status != "active" and (result is None or terminal_status(result) != status):
        raise ValueError("checkpoint 状态与结果不一致")
    payload = CheckpointState.model_validate(state).model_dump_json()
    with write_session() as session:
        if task_id is not None:
            lock_owned_task(session, task_id, run_id=run_id)
        row = session.query(AgentCheckpoint).filter_by(run_id=run_id).with_for_update().first()
        if row is None:
            row = AgentCheckpoint(
                run_id=run_id, task_id=task_id, item_id=item_id,
                window_start=start, window_end=end, anomaly_id=anomaly_id,
            )
            session.add(row)
        elif (row.task_id, row.item_id, row.window_start, row.window_end, row.anomaly_id) != (
            task_id, item_id, start, end, anomaly_id,
        ):
            raise ValueError("checkpoint 与当前诊断目标不一致")
        elif row.status in {"completed", "failed", "stopped"}:
            raise ValueError("禁止覆盖终态 checkpoint")
        row.task_id = task_id
        row.step = step
        row.status = status
        row.state_json = payload
        row.result_json = json.dumps(result, ensure_ascii=False) if result is not None else ""
        row.updated_at = utcnow()
        session.commit()


def complete_checkpoint(run_id: str, result: dict) -> None:
    with write_session() as session:
        row = session.query(AgentCheckpoint).filter_by(run_id=run_id).first()
        if row is None:
            return
        if row.task_id is not None:
            lock_owned_task(session, row.task_id, run_id=run_id)
            session.refresh(row)
        if row.status in {"completed", "failed", "stopped"}:
            raise ValueError("禁止覆盖终态 checkpoint")
        row.status = terminal_status(result)
        row.result_json = json.dumps(result, ensure_ascii=False, default=str)
        row.updated_at = utcnow()
        session.commit()


def decode_state(row: AgentCheckpoint) -> dict:
    try:
        value = json.loads(row.state_json or "{}")
        return CheckpointState.model_validate(value).model_dump()
    except (ValueError, TypeError) as error:
        # 旧/损坏存档不能静默变成空状态，否则会重新执行工具并重置预算。
        raise ValueError("checkpoint 格式不兼容或损坏；需检查存档或创建新任务") from error


def terminal_status(result: dict) -> str:
    status = {"ok": "completed", "error": "failed", "incomplete": "stopped"}.get(result.get("status"))
    failure = result.get("failure")
    if failure is not None:
        failure = FailureInfo.model_validate(failure)
    if status is None or (failure is not None and failure.retryable):
        raise ValueError("非终态结果不能标记 checkpoint 完成")
    return status


def decode_result(row: AgentCheckpoint) -> dict | None:
    if row.status not in {"completed", "failed", "stopped"}:
        return None
    try:
        value = json.loads(row.result_json)
        if not isinstance(value, dict):
            raise ValueError("invalid result")
        expected = terminal_status(value)
        # 兼容旧 completed 的失败/证据不足结果，但绝不自动重放旧失败。
        if row.status != "completed" and row.status != expected:
            raise ValueError("inconsistent result")
        return value
    except (ValueError, TypeError) as error:
        raise ValueError("终态 checkpoint 结果损坏，禁止静默重新执行") from error
