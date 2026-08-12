"""数据库连接管理。

- 写连接（agent_app）：服务层 / 数据导入 / 任务系统使用
- 只读连接（agent_ro）：Agent Tool 专用，并挂载 SQL 日志监听器（sql_logs/）
"""
from __future__ import annotations

import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base
from app.tracing import current_run_id, log_sql

_write_engine = None
_read_engine = None
_write_factory: sessionmaker | None = None
_read_factory: sessionmaker | None = None


def get_write_engine():
    global _write_engine
    if _write_engine is None:
        _write_engine = create_engine(settings.write_url(), pool_pre_ping=True)
    return _write_engine


def get_read_engine():
    global _read_engine
    if _read_engine is None:
        _read_engine = create_engine(settings.read_url(), pool_pre_ping=True)
        event.listen(_read_engine, "before_cursor_execute", _sql_before)
        event.listen(_read_engine, "after_cursor_execute", _sql_after)
    return _read_engine


def _sql_before(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("_sql_pending", []).append(
        (statement, parameters, time.perf_counter())
    )


def _sql_after(conn, cursor, statement, parameters, context, executemany):
    pending = conn.info.get("_sql_pending")
    if pending:
        stmt, params, start = pending.pop()
        dur = (time.perf_counter() - start) * 1000.0
        log_sql(
            current_run_id() or "cli",
            _truncate_sql(stmt),
            _safe_params(params),
            round(dur, 2),
            cursor.rowcount or 0,
        )


def _truncate_sql(sql: str, limit: int = 300) -> str:
    sql = " ".join(str(sql).split())
    return sql if len(sql) <= limit else sql[:limit] + "..."


def _safe_params(params) -> dict | str:
    try:
        if isinstance(params, dict):
            return {k: str(v)[:80] for k, v in params.items()}
        return str(params)[:300]
    except Exception:  # noqa: BLE001
        return "<?>"


def write_session() -> Session:
    global _write_factory
    if _write_factory is None:
        _write_factory = sessionmaker(bind=get_write_engine(), expire_on_commit=False)
    return _write_factory()


def read_session() -> Session:
    global _read_factory
    if _read_factory is None:
        _read_factory = sessionmaker(bind=get_read_engine(), expire_on_commit=False)
    return _read_factory()


def init_db() -> None:
    Base.metadata.create_all(get_write_engine())
