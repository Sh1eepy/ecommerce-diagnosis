"""指标查询：只读连接，供 Tool 与检测器使用。

安全约束：
- 全部参数化 SQL（防注入）
- 查询窗口上限 90 天（防 Agent 滥用资源）
- 走 agent_ro 只读连接
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text

from app.db import get_read_engine
from app.metrics.registry import BASE_COLUMNS, compute_metrics, funnel_stages

DATE_RANGE_MAX_DAYS = 90


def _check_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("start 不能晚于 end")
    if (end - start).days > DATE_RANGE_MAX_DAYS:
        raise ValueError(f"查询窗口超过 {DATE_RANGE_MAX_DAYS} 天上限")


def _rows(sql: str, params: dict) -> list[dict]:
    with get_read_engine().connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def daily_series(
    item_id: int,
    start: date,
    end: date,
    metric_names: list[str],
    dimension_type: str = "all",
    dimension: str = "all",
) -> list[dict]:
    """商品在窗口内的日指标序列。"""
    _check_range(start, end)
    cols = ", ".join(BASE_COLUMNS)
    rows = _rows(
        f"""SELECT stat_date, {cols}
            FROM daily_item_stat
            WHERE item_id = :item_id AND stat_date BETWEEN :start AND :end
              AND dimension_type = :dt AND dimension = :d
            ORDER BY stat_date""",
        {"item_id": item_id, "start": start, "end": end, "dt": dimension_type, "d": dimension},
    )
    out = []
    for r in rows:
        out.append({"date": str(r["stat_date"]), **compute_metrics(r, metric_names)})
    return out


def item_summary(item_id: int, start: date, end: date, metric_names: list[str]) -> dict:
    """窗口汇总 + 与上一等长窗口对比（用于"历史趋势/环比"）。"""
    _check_range(start, end)
    span = (end - start).days + 1
    prev_start = start - timedelta(days=span)
    cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in BASE_COLUMNS)

    def _agg(s: date, e: date) -> list[dict]:
        return _rows(
            f"""SELECT {cols}
                FROM daily_item_stat
                WHERE item_id=:item_id AND dimension_type='all' AND dimension='all'
                  AND stat_date BETWEEN :start AND :end""",
            {"item_id": item_id, "start": s, "end": e},
        )

    cur_rows = _agg(start, end)
    prev_rows = _agg(prev_start, start - timedelta(days=1))
    cur = compute_metrics(cur_rows[0], metric_names) if cur_rows else {m: 0.0 for m in metric_names}
    prev = compute_metrics(prev_rows[0], metric_names) if prev_rows else None
    return {"window": [str(prev_start), str(end)], "current": cur, "previous": prev}


def funnel(item_id: int, start: date, end: date) -> dict:
    """商品窗口漏斗：view → addtocart → transaction。"""
    _check_range(start, end)
    cols = (
        "COALESCE(SUM(view_count),0) AS view_count, "
        "COALESCE(SUM(addtocart_count),0) AS addtocart_count, "
        "COALESCE(SUM(transaction_count),0) AS transaction_count"
    )
    rows = _rows(
        f"""SELECT {cols}
            FROM daily_item_stat
            WHERE item_id=:item_id AND dimension_type='all' AND dimension='all'
              AND stat_date BETWEEN :start AND :end""",
        {"item_id": item_id, "start": start, "end": end},
    )
    r = rows[0] if rows else {}
    v, a, t = r.get("view_count", 0), r.get("addtocart_count", 0), r.get("transaction_count", 0)
    return {
        "stages": [
            {"stage": "view", "label": "曝光/浏览", "count": v},
            {"stage": "addtocart", "label": "加购", "count": a,
             "rate_from_view": round(a / v, 4) if v else 0.0},
            {"stage": "transaction", "label": "成交", "count": t,
             "rate_from_view": round(t / v, 4) if v else 0.0,
             "rate_from_addcart": round(t / a, 4) if a else 0.0},
        ]
    }


def dimension_breakdown(
    item_id: int,
    start: date,
    end: date,
    dimension_type: str,
    metric_names: list[str],
) -> list[dict]:
    """按维度值拆解（channel/device/user_type/activity/day_type/new_user...）。"""
    _check_range(start, end)
    if dimension_type not in {"day_type", "new_user", "channel", "device", "user_type", "activity"}:
        raise ValueError(f"不支持的维度类型: {dimension_type}")
    cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in BASE_COLUMNS)
    rows = _rows(
        f"""SELECT dimension, {cols}
            FROM daily_item_stat
            WHERE item_id=:item_id AND dimension_type=:dt
              AND stat_date BETWEEN :start AND :end
            GROUP BY dimension ORDER BY dimension""",
        {"item_id": item_id, "dt": dimension_type, "start": start, "end": end},
    )
    out = []
    for r in rows:
        out.append({"dimension": r["dimension"], **compute_metrics(r, metric_names)})
    return out
