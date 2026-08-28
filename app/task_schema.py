"""显式、可重复执行的 Worker 表结构升级；不在启动时自动 ALTER 业务表。"""
from sqlalchemy import inspect, text


_COLUMNS = {
    "lease_token": "VARCHAR(32) NULL",
    "lease_until": "DATETIME NULL",
    "heartbeat_at": "DATETIME NULL",
    "deadline_at": "DATETIME NULL",
}


def migration_statements(engine) -> list[str]:
    inspector = inspect(engine)
    if not inspector.has_table("task"):
        return []  # 新库由 init_db 创建完整表。
    existing = {column["name"] for column in inspector.get_columns("task")}
    return [f"ALTER TABLE task ADD COLUMN {name} {kind}"
            for name, kind in _COLUMNS.items() if name not in existing]


def migrate_task_schema(engine) -> list[str]:
    statements = migration_statements(engine)
    # MySQL DDL 会隐式提交；只增加可空列，部分成功后可重新执行，不能承诺事务回滚。
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    return statements


def require_task_schema(engine) -> None:
    if migration_statements(engine):
        raise RuntimeError("Worker 表结构需要升级：请停服务、备份数据库，再运行 scripts/migrate_worker_schema.py；默认只预览")
