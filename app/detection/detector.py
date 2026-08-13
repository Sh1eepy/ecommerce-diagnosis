"""异常检测器：扫描商品 → 跑规则 → 写入 anomaly_event（幂等）。"""
from __future__ import annotations

from datetime import date, timedelta

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


def _load_all_category_series(start: date, end: date) -> dict[int, list[tuple[date, dict]]]:
    """一次性加载窗口内全部类目切片（dimension_type='category'）的日聚合行。

    注意：category 维度按 (商品,日期) 存储，同一类目每天有多行 → 按 (类目,日期) SUM 聚合。
    排除 'unknown' 占位类目；dimension 存的是字符串类目 ID，转 int。
    """
    cols = ", ".join(f"SUM({c}) AS {c}" for c in BASE_COLUMNS)
    sql = f"""SELECT dimension, stat_date, {cols}
              FROM daily_item_stat
              WHERE dimension_type='category' AND dimension != 'unknown'
                AND stat_date BETWEEN :start AND :end
              GROUP BY dimension, stat_date
              ORDER BY dimension, stat_date"""
    groups: dict[int, list[tuple[date, dict]]] = {}
    with get_read_engine().connect() as conn:
        rows = conn.execute(text(sql), {"start": start, "end": end}).mappings()
        for r in rows:
            try:
                cat = int(r["dimension"])
            except (ValueError, TypeError):
                continue
            d = r["stat_date"]
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            groups.setdefault(cat, []).append((d, dict(r)))
    return groups


def _add_anomaly(session, item_id: int | None, category_id: int | None, rule, res: RuleResult) -> AnomalyEvent | None:
    """幂等插入一条异常事件；已存在同款 open 事件则返回 None，新插入返回该事件（含 id）。"""
    exists = session.query(AnomalyEvent).filter_by(
        item_id=item_id if item_id is not None else 0,
        category_id=category_id,
        metric=res.metric,
        rule_id=res.rule_id,
        date_end=res.date_end,
        status="open",
    ).first()
    if exists:
        return None
    anom = AnomalyEvent(
        item_id=item_id if item_id is not None else 0,
        category_id=category_id,
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
    )
    session.add(anom)
    session.flush()  # 立即取到自增 id（供自动诊断任务使用）
    return anom


def run_detection(start: date, end: date, rules=None, limit_items: int | None = None,
                  include_categories: bool = True, auto_diagnose: bool = True) -> int:
    """扫描所有商品（+ 类目切片）并落库新的异常事件。同款 open 事件跳过（幂等）。

    性能：一次性加载窗口内全部日序列（1 次查询），Python 内按商品/类目批量判定。
    类目级异常：item_id=0 + category_id=类目ID，表示"整个类目带崩"。
    auto_diagnose=True 时，为新产生的【商品级】异常自动创建诊断任务（类目级暂不诊断，
    因为 Agent 的工具是商品级）。幂等：重复检测不产生新异常，也就不产生新任务。
    """
    rules = list(rules or DEFAULT_RULES)
    groups = _load_all_series(start, end)
    items = sorted(groups)
    if limit_items:
        items = items[:int(limit_items)]
    cat_groups = _load_all_category_series(start, end) if include_categories else {}

    new_anomalies: list[AnomalyEvent] = []
    with write_session() as session:
        for item_id in items:
            raw_rows = groups[item_id]
            for rule in rules:
                series = [(d, compute_metrics(row, [rule.metric])[rule.metric]) for d, row in raw_rows]
                res = rule.evaluate(series)
                if res is not None:
                    anom = _add_anomaly(session, item_id, None, rule, res)
                    if anom:
                        new_anomalies.append(anom)

        for cat, raw_rows in cat_groups.items():
            for rule in rules:
                series = [(d, compute_metrics(row, [rule.metric])[rule.metric]) for d, row in raw_rows]
                res = rule.evaluate(series)
                if res is not None:
                    anom = _add_anomaly(session, None, cat, rule, res)
                    if anom:
                        new_anomalies.append(anom)

        session.commit()

    if auto_diagnose:
        _create_diagnosis_tasks(new_anomalies)
    return len(new_anomalies)


def _create_diagnosis_tasks(new_anomalies: list[AnomalyEvent]) -> int:
    """为新异常自动创建诊断任务（懒加载 create_task，避免检测器背上整条 Agent 依赖链）。

    类目级异常（item_id=0）跳过：Agent 的工具（metric/funnel/dimension/peer）都是商品级。
    """
    from app.tasks.queue import create_task  # 懒加载，避免循环/重依赖

    created = 0
    for anom in new_anomalies:
        if anom.item_id == 0:
            continue
        create_task(
            "diagnose",
            payload={
                "item_id": anom.item_id,
                "start_date": str(anom.date_start - timedelta(days=3)),
                "end_date": str(anom.date_end),
                "anomaly": anom.description,
            },
            anomaly_id=anom.id,
            idempotency_key=f"diag-anom:{anom.id}",
        )
        created += 1
    return created
