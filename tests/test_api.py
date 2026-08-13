"""API 集成测试（SQLite + MockLLM 全离线）。"""
import io

from fastapi.testclient import TestClient

from app.api.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-key"}


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_security_headers():
    r = client.get("/healthz")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("content-security-policy") == "default-src 'none'"


def test_auth_required():
    r = client.post("/api/v1/diagnostics", json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"})
    assert r.status_code == 401


def test_diagnostics_sync():
    r = client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14", "sync": True})
    assert r.status_code == 201
    body = r.json()
    assert body["sync"] is True
    assert body["run_id"]
    assert body["report"]["conclusion"]


def test_diagnostics_async_creates_task():
    r = client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"})
    assert r.status_code == 201
    body = r.json()
    assert body["sync"] is False
    assert body["status"] == "pending"
    r2 = client.get(f"/api/v1/tasks/{body['task_id']}", headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["status"] == "pending"


def test_diagnostics_idempotent():
    payload = {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}
    r1 = client.post("/api/v1/diagnostics", headers=HEADERS, json=payload)
    r2 = client.post("/api/v1/diagnostics", headers=HEADERS, json=payload)
    assert r1.status_code == 201
    assert r1.json()["task_id"] == r2.json()["task_id"]


def test_task_not_found():
    r = client.get("/api/v1/tasks/999999", headers=HEADERS)
    assert r.status_code == 404


def test_import_csv():
    csv_data = (
        "item_id,stat_date,dimension_type,dimension,uv,view_count,click_count,"
        "addtocart_count,transaction_count,gmv\n"
        "999,2015-07-01,all,all,10,30,30,2,1,1\n"
    )
    r = client.post(
        "/api/v1/import/daily-stat", headers=HEADERS,
        files={"file": ("stats.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")},
    )
    assert r.status_code == 200
    assert r.json()["imported_rows"] == 1


def test_import_csv_bad_header():
    r = client.post(
        "/api/v1/import/daily-stat", headers=HEADERS,
        files={"file": ("bad.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 400


def test_feedback():
    r = client.post("/api/v1/feedback", headers=HEADERS, json={
        "run_id": "abc123", "rating": 2, "category": "analysis", "comment": "结论不准"})
    assert r.status_code == 200


def test_monitoring_endpoint():
    r = client.get("/api/v1/monitoring?window_hours=24", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "agent_runs" in body and "tool_calls" in body and "tasks" in body
    # 未带 Key 应 401
    r2 = client.get("/api/v1/monitoring")
    assert r2.status_code == 401
