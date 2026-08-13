"""检测器集成测试（基于 seeded 的 SQLite 库）。"""
from datetime import date

from app.db import read_session
from app.detection.detector import detect_for_item, run_detection
from app.detection.rules import ConsecutiveDeclineRule
from app.models import AnomalyEvent


def test_detect_item1_cvr_drop():
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)]
    res = detect_for_item(1, rules, date(2015, 6, 1), date(2015, 6, 14))
    assert len(res) == 1
    assert res[0].metric == "cvr"


def test_detect_item2_clean():
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)]
    assert detect_for_item(2, rules, date(2015, 6, 1), date(2015, 6, 14)) == []


def test_run_detection_idempotent():
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)]
    n1 = run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    n2 = run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    assert n1 >= 1
    assert n2 == 0  # 幂等：二次运行不再产生重复事件


def test_category_detection_creates_category_event():
    # 阈值放宽到 0.15：类目 100（item1+item2 合并）cvr 降幅 ~20% 也触发
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.15)]
    run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    with read_session() as s:
        cat_anoms = s.query(AnomalyEvent).filter_by(item_id=0, category_id=100).all()
        assert len(cat_anoms) >= 1
        assert cat_anoms[0].metric == "cvr"
