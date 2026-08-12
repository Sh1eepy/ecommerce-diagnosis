"""健康检查。"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db import get_read_engine

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    try:
        with get_read_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "up"}
    except Exception as e:  # noqa: BLE001
        return {"status": "degraded", "db": f"{type(e).__name__}: {e}"}
