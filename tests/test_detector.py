"""检测器集成测试（基于 seeded 的 SQLite 库）。"""
import json
from datetime import date

from app.db import read_session, write_session
from app.detection.detector import detect_for_item, run_detection
from app.detection.rules import ConsecutiveDeclineRule
from app.models import AnomalyEvent, Task


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


def _clear_anomalies_and_tasks():
    with write_session() as s:
        s.query(AnomalyEvent).delete()
        s.query(Task).delete()
        s.commit()


def test_detection_auto_creates_diagnosis_task():
    """新商品级异常应自动创建诊断任务；重复检测幂等不产生新任务。"""
    _clear_anomalies_and_tasks()
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30)]
    n = run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    assert n >= 1
    with read_session() as s:
        tasks = s.query(Task).filter(Task.task_type == "diagnose").all()
        assert len(tasks) == 1  # item1 商品级 cvr 异常 → 1 个任务
        t = tasks[0]
        assert t.idempotency_key.startswith("diag-anom:")
        payload = json.loads(t.payload_json)
        assert payload["item_id"] == 1
        assert payload["anomaly"] and payload["start_date"] and payload["end_date"]
        assert t.anomaly_id is not None

    # 幂等：再跑一遍不产生新异常，也就不产生新任务
    n2 = run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    assert n2 == 0
    with read_session() as s:
        assert s.query(Task).filter(Task.task_type == "diagnose").count() == 1


def test_category_anomaly_not_auto_diagnosed():
    """类目级异常（item_id=0）不自动建任务：Agent 工具是商品级。"""
    _clear_anomalies_and_tasks()
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.15)]
    n = run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    assert n >= 2  # item1 商品级 + 类目100 类目级都触发
    with read_session() as s:
        assert s.query(AnomalyEvent).filter_by(item_id=0, category_id=100).count() >= 1
        # 商品级 1 个任务，类目级跳过
        assert s.query(Task).filter(Task.task_type == "diagnose").count() == 1
