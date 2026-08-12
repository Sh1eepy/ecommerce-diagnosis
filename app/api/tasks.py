"""任务查询接口。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from app.security import verify_api_key
from app.tasks.queue import get_task

router = APIRouter(tags=["tasks"])


@router.get("/tasks/{task_id}")
def task_status(task_id: int, _: str = Depends(verify_api_key)) -> dict:
    t = get_task(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": t.id,
        "task_type": t.task_type,
        "status": t.status,
        "attempts": t.attempts,
        "priority": t.priority,
        "run_id": t.run_id,
        "anomaly_id": t.anomaly_id,
        "error": t.error,
        "result": json.loads(t.result_json) if t.result_json else None,
        "created_at": str(t.created_at),
    }
