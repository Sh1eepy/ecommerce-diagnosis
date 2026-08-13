"""指标查询：只读连接，供 Tool 与检测器使用。

安全约束：
- 全部参数化 SQL（防注入）
- 查询窗口上限 90 天（防 Agent 滥用资源）
- 走 agent_ro 只读连接
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import text

from app.db import get_read_engine
from app.metrics.registry import BASE_COLUMNS, compute_metrics, funnel_stages

DATE_RANGE_MAX_DAYS = 90


def _check_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("start 不能晚于 end")
    if (end - start).days > DATE_RANGE_MAX_DAYS:
        raise ValueError(f"查询窗口超过 {DATE_RANGE_MAX_DAYS} 天上限")


def _date_to_ms(d: date) -> int:
    return int(datetime.combine(d, time.min).timestamp() * 1000)


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


def item_unavailable_periods(item_id: int, start: date, end: date) -> list[dict]:
    """商品在窗口内的不可用记录（available=0 的变更点）。

    available 来自 item_properties（未哈希），是"商品下架→成交骤降"的直接证据。
    """
    _check_range(start, end)
    rows = _rows(
        """SELECT ts_ms, available FROM item_availability
           WHERE item_id=:item_id AND ts_ms BETWEEN :s AND :e AND available=0
           ORDER BY ts_ms LIMIT 20""",
        {"item_id": item_id, "s": _date_to_ms(start), "e": _date_to_ms(end + timedelta(days=1)) - 1},
    )
    return [{"date": datetime.fromtimestamp(r["ts_ms"] / 1000.0).strftime("%Y-%m-%d")} for r in rows]


def item_price(item_id: int) -> float | None:
    """商品最新价格（V1 近似）。"""
    rows = _rows("SELECT price FROM item_price WHERE item_id=:item_id", {"item_id": item_id})
    return rows[0]["price"] if rows else None


def dimension_breakdown(
    item_id: int,
    start: date,
    end: date,
    dimension_type: str,
    metric_names: list[str],
) -> list[dict]:
    """按维度值拆解（channel/device/user_type/activity/day_type/new_user...）。"""
    _check_range(start, end)
    if dimension_type not in {"day_type", "new_user", "category", "channel", "device", "user_type", "activity"}:
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


def item_category_id(item_id: int) -> int | None:
    """商品所属类目（item_category，未哈希的 categoryid）。"""
    rows = _rows("SELECT category_id FROM item_category WHERE item_id=:item_id", {"item_id": item_id})
    return int(rows[0]["category_id"]) if rows else None


def _agg_over_window(cols_sql: str, where_sql: str, params: dict) -> dict:
    rows = _rows(
        f"SELECT {cols_sql} FROM daily_item_stat WHERE {where_sql}",
        params,
    )
    return rows[0] if rows else {}


def category_total_raw(category_id: int, start: date, end: date) -> dict:
    """类目整体在窗口内的原始聚合行（SUM 各基础列）。"""
    cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in BASE_COLUMNS)
    return _agg_over_window(
        cols,
        "dimension_type='category' AND dimension=:cat AND stat_date BETWEEN :start AND :end",
        {"cat": str(category_id), "start": start, "end": end},
    )


def category_summary(category_id: int, start: date, end: date, metric_names: list[str]) -> dict:
    """类目整体指标（计算派生指标）。"""
    _check_range(start, end)
    return compute_metrics(category_total_raw(category_id, start, end), metric_names)


def item_summary_raw(item_id: int, start: date, end: date) -> dict:
    """商品自身在窗口内的原始聚合行（用于从类目总量中扣减）。"""
    cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in BASE_COLUMNS)
    return _agg_over_window(
        cols,
        "item_id=:item_id AND dimension_type='all' AND dimension='all' AND stat_date BETWEEN :start AND :end",
        {"item_id": item_id, "start": start, "end": end},
    )


def peers_stats(item_id: int, start: date, end: date, metric_names: list[str]) -> dict | None:
    """同类目排除自身后的同行汇总指标；无类目返回 None。"""
    cat = item_category_id(item_id)
    if cat is None:
        return None
    cat_row = category_total_raw(cat, start, end)
    own_row = item_summary_raw(item_id, start, end)
    peer_row = {c: cat_row.get(c, 0) - own_row.get(c, 0) for c in BASE_COLUMNS}
    return {"category_id": cat, "peers": compute_metrics(peer_row, metric_names)}


def peer_items(category_id: int, start: date, end: date, limit: int = 5) -> list[dict]:
    """同类目 UV TOP N 商品列表（对照样本）。"""
    rows = _rows(
        """SELECT s.item_id, SUM(s.uv) AS uv
           FROM daily_item_stat s
           JOIN item_category ic ON ic.item_id = s.item_id AND ic.category_id = :cat
           WHERE s.dimension_type='all' AND s.dimension='all'
             AND s.stat_date BETWEEN :start AND :end
           GROUP BY s.item_id ORDER BY uv DESC LIMIT :lim""",
        {"cat": category_id, "start": start, "end": end, "lim": int(limit)},
    )
    return [{"item_id": int(r["item_id"]), "uv": int(r["uv"] or 0)} for r in rows]
