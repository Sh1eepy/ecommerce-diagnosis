"""规则引擎单元测试。"""
from datetime import date, timedelta

from app.detection.rules import ConsecutiveDeclineRule, PeriodDropRule


def _series(values, start=date(2015, 6, 1)):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_consecutive_decline_triggers():
    rule = ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)
    res = rule.evaluate(_series([8.0, 8.0, 8.0, 7.0, 6.0, 5.0, 4.0]))
    assert res is not None
    assert res.metric == "cvr"
    assert res.baseline_value == 7.0
    assert res.current_value == 4.0
    assert res.change_pct >= 0.30
    assert res.date_start == date(2015, 6, 4)
    assert res.date_end == date(2015, 6, 7)


def test_consecutive_decline_insufficient_data():
    rule = ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)
    assert rule.evaluate(_series([8.0, 7.0, 6.0])) is None


def test_consecutive_decline_flat_no_trigger():
    rule = ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)
    assert rule.evaluate(_series([8.0] * 10)) is None


def test_consecutive_decline_uptick_breaks_monotone():
    # 6,8,7,6,5 最后4点: 8,7,6,5 连续下降 → 仍触发（基准 8）
    rule = ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)
    res = rule.evaluate(_series([8.0, 6.0, 8.0, 7.0, 6.0, 5.0]))
    assert res is not None
    assert res.baseline_value == 8.0


def test_small_drop_not_enough():
    rule = ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)
    # 8,7.5,7,6.8 → 累计降幅 15% < 30%
    res = rule.evaluate(_series([8.0, 7.5, 7.0, 6.8]))
    assert res is None


def test_period_drop_triggers():
    rule = PeriodDropRule("cvr", days=3, drop_pct=0.30)
    res = rule.evaluate(_series([8.0, 8.0, 8.0, 4.0, 4.0, 4.0]))
    assert res is not None
    assert res.change_pct >= 0.49


def test_period_drop_flat_no_trigger():
    rule = PeriodDropRule("cvr", days=3, drop_pct=0.30)
    assert rule.evaluate(_series([8.0, 8.0, 8.0, 8.0, 8.0, 8.0])) is None
