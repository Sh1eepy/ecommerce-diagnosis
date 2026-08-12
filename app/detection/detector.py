"""异常检测器：扫描商品 → 跑规则 → 写入 anomaly_event（幂等）。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text

from app.db import get_read_engine, write_session
from app.detection.rules import DEFAULT_RULES, RuleResult
from app.metrics.registry import BASE_COLUMNS, compute_metrics
from app.models import AnomalyEvent


def data_date_range() -> tuple[date, date]:
    """日表数据覆盖范围。"""
    with get_read_engine().connect() as conn:
        row = conn.execute(
            text("SELECT MIN(stat_date) AS mn, MAX(stat_date) AS mx FROM daily_item_stat")
        ).one()
    return row.mn, row.mx


def _fetch_series(item_id: int, metric: str, start: date, end: date) -> list[tuple[date, float]]:
    sql = f"""SELECT stat_date, {', '.join(BASE_COLUMNS)}
              FROM daily_item_stat
              WHERE item_id=:item_id AND dimension_type='all' AND dimension='all'
                AND stat_date BETWEEN :start AND :end ORDER BY stat_date"""
    with get_read_engine().connect() as conn:
        rows = conn.execute(text(sql), {"item_id": item_id, "start": start, "end": end}).mappings()
        series = []
        for r in rows:
            row = compute_metrics(dict(r), [metric])
            # 兼容不同方言：raw text 查询在 SQLite 下日期为 str，MySQL 为 date
            d = r["stat_date"]
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            series.append((d, row[metric]))
        return series


def _load_all_series(start: date, end: date) -> dict[int, list[tuple[date, dict]]]:
    """一次性加载窗口内全部商品的日聚合行，按 item_id 分组（批量扫描，避免逐商品逐规则查询）。"""
    sql = f"""SELECT item_id, stat_date, {', '.join(BASE_COLUMNS)}
              FROM daily_item_stat
              WHERE dimension_type='all' AND dimension='all'
                AND stat_date BETWEEN :start AND :end
              ORDER BY item_id, stat_date"""
    groups: dict[int, list[tuple[date, dict]]] = {}
    with get_read_engine().connect() as conn:
        rows = conn.execute(text(sql), {"start": start, "end": end}).mappings()
        for r in rows:
            d = r["stat_date"]
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            groups.setdefault(r["item_id"], []).append((d, dict(r)))
    return groups


def detect_for_item(item_id: int, rules, start: date, end: date) -> list[RuleResult]:
    results: list[RuleResult] = []
    for rule in rules:
        series = _fetch_series(item_id, rule.metric, start, end)
        res = rule.evaluate(series)
        if res:
            results.append(res)
    return results


def _severity(change_pct: float) -> str:
    if change_pct >= 0.5:
        return "high"
    if change_pct >= 0.30:
        return "medium"
    return "low"


def run_detection(start: date, end: date, rules=None, limit_items: int | None = None) -> int:
    """扫描所有商品，落库新的异常事件。同款 open 事件跳过（幂等）。

    性能：一次性加载窗口内全部日序列（1 次查询），Python 内按商品批量判定，
    避免逐商品逐规则查询（V2 优化，解决全量扫描过慢）。
    """
    rules = list(rules or DEFAULT_RULES)
    groups = _load_all_series(start, end)
    items = sorted(groups)
    if limit_items:
        items = items[:int(limit_items)]

    created = 0
    with write_session() as session:
        for item_id in items:
            raw_rows = groups[item_id]
            for rule in rules:
                series = [(d, compute_metrics(row, [rule.metric])[rule.metric]) for d, row in raw_rows]
                res = rule.evaluate(series)
                if res is None:
                    continue
                exists = session.query(AnomalyEvent).filter_by(
                    item_id=item_id,
                    metric=res.metric,
                    rule_id=res.rule_id,
                    date_end=res.date_end,
                    status="open",
                ).first()
                if exists:
                    continue
                session.add(AnomalyEvent(
                    item_id=item_id,
                    metric=res.metric,
                    rule_id=res.rule_id,
                    rule_name=res.rule_name,
                    date_start=res.date_start,
                    date_end=res.date_end,
                    baseline_value=res.baseline_value,
                    current_value=res.current_value,
                    change_pct=res.change_pct,
                    severity=_severity(res.change_pct),
                    description=res.description,
                ))
                created += 1
        session.commit()
    return created
