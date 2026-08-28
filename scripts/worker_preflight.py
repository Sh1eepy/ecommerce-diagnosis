"""Worker 启动前只读盘点：不建表、不领取/回收任务、不调用模型或告警。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def inspect_queue(connection, now: datetime | None = None) -> dict:
    """单条聚合 SELECT；不读取任务正文、结果、错误正文或领取凭证。"""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    row = connection.execute(text("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status = 'retrying' THEN 1 ELSE 0 END) AS retrying,
          SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
          SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
          SUM(CASE WHEN status = 'incomplete' THEN 1 ELSE 0 END) AS incomplete,
          SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed,
          SUM(CASE WHEN status NOT IN
            ('pending', 'retrying', 'running', 'succeeded', 'incomplete', 'failed', 'error')
            OR status IS NULL THEN 1 ELSE 0 END) AS unknown,
          SUM(CASE WHEN status IN ('pending', 'retrying')
            AND (retry_after IS NULL OR retry_after <= :now)
            THEN 1 ELSE 0 END) AS due_queue_candidates,
          SUM(CASE WHEN status = 'running' AND
            (lease_token IS NULL OR lease_until IS NULL OR lease_until <= :now)
            THEN 1 ELSE 0 END) AS recoverable_running_candidates,
          SUM(CASE WHEN status IN ('pending', 'retrying', 'running')
            AND attempts >= max_retries THEN 1 ELSE 0 END) AS attempts_exhausted,
          SUM(CASE WHEN status IN ('pending', 'retrying', 'running')
            AND deadline_at <= :now THEN 1 ELSE 0 END) AS deadline_expired,
          SUM(CASE WHEN status IN ('pending', 'retrying', 'running')
            AND deadline_at IS NULL AND (attempts > 0 OR status != 'pending')
            THEN 1 ELSE 0 END) AS missing_previous_deadline
        FROM task
    """).bindparams(now=now)).mappings().one()
    counts = {key: int(value or 0) for key, value in row.items()}
    counts["unfinished"] = counts["pending"] + counts["retrying"] + counts["running"]
    return counts


def build_report(connection, config) -> dict:
    now = datetime.now(timezone.utc)
    database = {"driver": config.DB_DRIVER}
    if config.DB_DRIVER == "mysql":
        database.update(host=config.DB_HOST, port=config.DB_PORT, name=config.DB_NAME)
    return {
        "checked_at": now.isoformat(),
        "database": database,
        # 与当前 get_llm 工厂判断保持一致；不构造客户端，不检查/打印 Key 内容。
        "model_mode": "external_api" if config.LLM_API_KEY else "mock",
        "alert_configured": bool(config.ALERT_WEBHOOK_URL),
        "worker_concurrency": config.WORKER_CONCURRENCY,
        "queue": inspect_queue(connection, now.replace(tzinfo=None)),
        "read_only": True,
    }


def main() -> int:
    try:
        # 在异常脱敏边界内加载配置，不让连接/配置异常输出凭据。
        from app.config import settings
        from app.db import get_write_engine

        # 使用 Worker 的连接配置，但本入口只发 SELECT，不调用 init_db/队列写入函数。
        with get_write_engine().connect() as connection:
            report = build_report(connection, settings)
    except Exception as error:
        print(f"只读检查失败：{type(error).__name__}；请核对连接、权限及迁移状态。", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("只读盘点完成：未建表、未领取或回收任务、未调用模型或告警。")
    print("候选数不是实际重跑数；未校验每个 checkpoint，部分计数会重叠。")
    print("这是当前快照，不是启动许可；启动前还需确认没有其他 Worker/调度器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
