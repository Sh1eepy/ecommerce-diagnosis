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
from app.metrics.registry import ALLOWED_DIMENSIONS, BASE_COLUMNS, compute_metrics
from app.metrics.windows import DATE_RANGE_MAX_DAYS, check_range as _check_range, compare_windows, paired_windows


def _sum_columns() -> str:
    # 列名只来自代码常量，不接收模型输入。
    return ", ".join(f"COALESCE(SUM({column}),0) AS {column}" for column in BASE_COLUMNS)


def _date_to_ms(d: date) -> int:
    return int(datetime.combine(d, time.min).timestamp() * 1000)


def _rows(sql: str, params: dict) -> list[dict]:
    # bindparams 从值推导 SQL 类型，让方言处理 date，不依赖 sqlite3 的弃用适配器。
    statement = text(sql).bindparams(**params)
    with get_read_engine().connect() as conn:
        return [dict(r) for r in conn.execute(statement).mappings()]


def _daily_totals(item_id: int, start: date, end: date,
                  dimension_type: str = "all", dimension: str = "all") -> list[dict]:
    """调用方先校验单窗口；此处也服务于已校验的相邻双窗口查询。"""
    return _rows(
        f"""SELECT stat_date, {_sum_columns()} FROM daily_item_stat
            WHERE item_id=:item_id AND stat_date BETWEEN :start AND :end
              AND dimension_type=:dt AND dimension=:d
            GROUP BY stat_date ORDER BY stat_date""",
        {"item_id": item_id, "start": start, "end": end, "dt": dimension_type, "d": dimension},
    )


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
    rows = _daily_totals(item_id, start, end, dimension_type, dimension)
    out = []
    for r in rows:
        out.append({"date": str(r["stat_date"]), **compute_metrics(r, metric_names)})
    return out


def item_summary(item_id: int, start: date, end: date, metric_names: list[str]) -> dict:
    """窗口汇总 + 与上一等长窗口对比（用于"历史趋势/环比"）。"""
    return item_comparison(item_id, start, end, metric_names)["summary"]


def item_comparison(item_id: int, start: date, end: date, metric_names: list[str]) -> dict:
    """一次取双窗口原始日汇总，同时供日序列、汇总、覆盖和变化量使用。"""
    previous_start = paired_windows(start, end)["previous"][0]
    rows = _daily_totals(item_id, previous_start, end)
    daily = [{"date": str(row["stat_date"]), **{c: row[c] for c in BASE_COLUMNS}} for row in rows]
    return {"series": [{"date": row["date"], **compute_metrics(row, metric_names)} for row in daily
                       if str(start) <= row["date"] <= str(end)],
            "summary": compare_windows(daily, start, end, metric_names)}


def funnel(item_id: int, start: date, end: date) -> dict:
    """商品窗口漏斗：view → addtocart → transaction。"""
    _check_range(start, end)
    r = item_summary_raw(item_id, start, end)
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

    这是状态观察点，不是整个窗口不可售或成交下降因果关系的证明。
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
    if dimension_type not in ALLOWED_DIMENSIONS:
        raise ValueError(f"不支持的维度类型: {dimension_type}")
    cols = _sum_columns()
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
    _check_range(start, end)
    cols = _sum_columns()
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
    """商品自身在窗口内的原始聚合行，供漏斗等单窗口查询复用。"""
    _check_range(start, end)
    cols = _sum_columns()
    return _agg_over_window(
        cols,
        "item_id=:item_id AND dimension_type='all' AND dimension='all' AND stat_date BETWEEN :start AND :end",
        {"item_id": item_id, "start": start, "end": end},
    )


def peers_stats(item_id: int, start: date, end: date, metric_names: list[str]) -> dict | None:
    """同类目排除自身后的单窗口同行指标；保留既有类目切片减自身口径。"""
    _check_range(start, end)
    cat = item_category_id(item_id)
    if cat is None:
        return None
    category = category_total_raw(cat, start, end)
    own = item_summary_raw(item_id, start, end)
    return {"category_id": cat, "peers": peers_from_totals(category, own, metric_names)}


def peers_from_totals(category: dict, own: dict, metric_names: list[str]) -> dict:
    """复用已读取的单窗口原始汇总，避免为派生指标重复查库。"""
    peer_row = {c: category.get(c, 0) - own.get(c, 0) for c in BASE_COLUMNS}
    return compute_metrics(peer_row, metric_names)


def peer_items(category_id: int, start: date, end: date, limit: int = 5) -> list[dict]:
    """同类目 UV TOP N 商品列表（对照样本）。"""
    _check_range(start, end)
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
