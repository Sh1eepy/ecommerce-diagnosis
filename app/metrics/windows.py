"""双窗口的纯计算契约：日期、覆盖率、变化量共用，不访问数据库。"""
from __future__ import annotations

from datetime import date, timedelta

from app.metrics.registry import BASE_COLUMNS, RATIO_COMPONENTS, compute_metrics

DATE_RANGE_MAX_DAYS = 90
RATE_METRICS = frozenset(name for name, (_, _, scale) in RATIO_COMPONENTS.items() if scale == 100)


def check_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("start 不能晚于 end")
    if (end - start).days > DATE_RANGE_MAX_DAYS:
        raise ValueError(f"查询窗口超过 {DATE_RANGE_MAX_DAYS} 天上限")


def paired_windows(start: date, end: date) -> dict[str, tuple[date, date]]:
    check_range(start, end)
    try:
        previous_end = start - timedelta(days=1)
        previous_start = start - timedelta(days=(end - start).days + 1)
    except OverflowError as error:
        raise ValueError("日期过早，无法构造上一等长窗口") from error
    return {"current": (start, end), "previous": (previous_start, previous_end)}


def coverage(rows: list[dict], start: date, end: date) -> dict:
    expected = {str(start + timedelta(days=i)) for i in range((end - start).days + 1)}
    observed = {str(row["date"]) for row in rows} & expected
    return {"expected_days": len(expected), "observed_days": len(observed),
            "dates_without_rows": sorted(expected - observed), "missing_days_are_zero": False}


def has_full_observations(info: dict | None) -> bool:
    """记录齐全≠业务数据已完整审计；缺任一方时不输出可解释的变化量。"""
    return bool(isinstance(info, dict) and info.get("expected_days", 0) > 0
                and info.get("observed_days") == info["expected_days"]
                and info.get("dates_without_rows") == [])


def compare_windows(rows: list[dict], start: date, end: date, names: list[str]) -> dict:
    windows = paired_windows(start, end)
    values, covered, totals = {}, {}, {}
    for label, (left, right) in windows.items():
        selected = [row for row in rows if str(left) <= str(row["date"]) <= str(right)]
        raw = {column: sum((row.get(column) or 0) for row in selected) for column in BASE_COLUMNS}
        totals[label] = raw
        values[label] = compute_metrics(raw, names)
        covered[label] = coverage(selected, left, right)
    comparable = all(has_full_observations(info) for info in covered.values())
    changes = {}
    for name in names:
        current, previous = float(values["current"][name]), float(values["previous"][name])
        denominator_valid = name not in RATIO_COMPONENTS or all(
            raw[RATIO_COMPONENTS[name][1]] != 0 for raw in totals.values()
        )
        usable = comparable and denominator_valid
        changes[name] = {
            "delta": round(current - previous, 4) if usable else None,
            "delta_unit": "percentage_points" if name in RATE_METRICS else "metric_units",
            "relative_change_pct": round((current - previous) / previous * 100, 4)
            if usable and previous != 0 else None,
            "status": "insufficient_coverage" if not comparable else
                      ("undefined_denominator" if not denominator_valid else
                       ("zero_baseline" if previous == 0 else "ok")),
        }
    return {"window": [str(windows["previous"][0]), str(end)],
            "windows": {key: [str(left), str(right)] for key, (left, right) in windows.items()},
            **values, "coverage": covered, "changes": changes,
            "sample_counts": {label: compute_metrics(raw, ["uv", "transaction_count"]) for label, raw in totals.items()},
            "comparison_status": "observed_days_covered" if comparable else "insufficient_coverage"}
