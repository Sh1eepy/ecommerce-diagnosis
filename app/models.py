"""ORM 模型。

权限语义：
- Agent Tool 只通过 agent_ro 只读连接访问这些表（无写权限）。
- 写操作仅通过 agent_app 账号（服务层/导入/任务系统），且代码层 Agent 无任何写路径。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class DailyItemStat(Base):
    """按天×商品×维度聚合的商品经营日表（指标层主表）。"""

    __tablename__ = "daily_item_stat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    dimension_type: Mapped[str] = mapped_column(String(16), default="all", index=True)
    dimension: Mapped[str] = mapped_column(String(32), default="all", index=True)
    uv: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    click_count: Mapped[int] = mapped_column(Integer, default=0)
    addtocart_count: Mapped[int] = mapped_column(Integer, default=0)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    gmv: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index("idx_item_date_dim", "item_id", "stat_date", "dimension_type", "dimension"),
    )


class RawEvent(Base):
    """Retailrocket 原始行为事件（staging 表，仅导入/审计使用）。"""

    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    visitor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    transaction_id: Mapped[str] = mapped_column(String(64), default="")
    event_date: Mapped[date] = mapped_column(Date, index=True)


class CategoryTree(Base):
    __tablename__ = "category_tree"

    category_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(BigInteger, default=0)


class ItemPrice(Base):
    """商品最新价格（prop=790，V1 用最新快照近似）。"""

    __tablename__ = "item_price"

    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    ts_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class ItemCategory(Base):
    """商品所属类目（item_properties.categoryid，未哈希）。"""

    __tablename__ = "item_category"

    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_id: Mapped[int] = mapped_column(BigInteger, default=0)


class ItemAvailability(Base):
    """商品可用性变更日志（item_properties.available，未哈希）。"""

    __tablename__ = "item_availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    available: Mapped[int] = mapped_column(Integer, default=1)


class AnomalyEvent(Base):
    """规则引擎产出的异常事件（Agent 的调查对象）。"""

    __tablename__ = "anomaly_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    metric: Mapped[str] = mapped_column(String(32), index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_name: Mapped[str] = mapped_column(String(64))
    date_start: Mapped[date] = mapped_column(Date)
    date_end: Mapped[date] = mapped_column(Date)
    baseline_value: Mapped[float] = mapped_column(Float)
    current_value: Mapped[float] = mapped_column(Float)
    change_pct: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(8), default="high")
    status: Mapped[str] = mapped_column(String(8), default="open")
    description: Mapped[str] = mapped_column(Text, default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRun(Base):
    """一次 Agent 诊断的总览（结构化记录，明细在 logs/）。"""

    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    anomaly_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ToolCallLog(Base):
    """工具调用审计日志。"""

    __tablename__ = "tool_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    step: Mapped[int] = mapped_column(Integer, default=0)
    tool: Mapped[str] = mapped_column(String(32))
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(8), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    """任务队列表（DB 队列）。状态机：pending→running→succeeded|failed|retrying。"""

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(32), default="diagnose")
    anomaly_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DiagnosticReport(Base):
    """诊断报告（结构化内容存 JSON）。"""

    __tablename__ = "diagnostic_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    anomaly_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
