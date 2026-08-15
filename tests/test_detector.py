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


def test_category_anomaly_auto_diagnosed_with_category_id():
    """类目级异常（item_id=0）也会自动建任务：payload 附带 category_id，Worker 选锚点。"""
    _clear_anomalies_and_tasks()
    rules = [ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.15)]
    n = run_detection(date(2015, 6, 1), date(2015, 6, 14), rules=rules)
    assert n >= 2  # item1 商品级 + 类目100 类目级都触发
    with read_session() as s:
        assert s.query(AnomalyEvent).filter_by(item_id=0, category_id=100).count() >= 1
        tasks = s.query(Task).filter(Task.task_type == "diagnose").all()
        assert len(tasks) == 2  # 商品级 + 类目级各 1 个任务
        cat_task = next(t for t in tasks if json.loads(t.payload_json)["item_id"] == 0)
        payload = json.loads(cat_task.payload_json)
        assert payload["category_id"] == 100
        assert payload["item_id"] == 0
        assert cat_task.idempotency_key.startswith("diag-anom:")
        assert cat_task.anomaly_id is not None


def test_find_anchor_item():
    """类目 100 的锚点商品：窗口内 UV 最高的商品（item1/item2 UV 相同，取其一）。"""
    from app.detection.detector import find_anchor_item

    anchor = find_anchor_item(100, date(2015, 6, 1), date(2015, 6, 14))
    assert anchor in (1, 2)
    # 不存在的类目 → None
    assert find_anchor_item(999999, date(2015, 6, 1), date(2015, 6, 14)) is None


def test_worker_diagnose_category_task_uses_anchor():
    """类目级任务：Worker 取锚点商品跑诊断（MockLLM 离线），返回 run_id。"""
    from app.tasks.queue import get_task
    from app.tasks.worker import _run_diagnose

    _clear_anomalies_and_tasks()
    with write_session() as s:
        t = Task(
            task_type="diagnose",
            anomaly_id=1,
            idempotency_key="t-cat-anchor-test",
            payload_json=json.dumps({
                "item_id": 0,
                "category_id": 100,
                "start_date": "2015-06-01",
                "end_date": "2015-06-14",
                "anomaly": "[类目级] 测试异常",
            }),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        tid = t.id

    result = _run_diagnose(get_task(tid))
    assert result.get("run_id")
    assert result.get("status") == "ok"  # MockLLM 离线跑通
