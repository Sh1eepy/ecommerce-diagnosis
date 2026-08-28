"""API 集成测试（SQLite + MockLLM 全离线）。"""
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.config import Settings, settings
from app.db import write_session
from app.models import DailyItemStat, DiagnosticReport

client = TestClient(app)
HEADERS = {"X-API-Key": "test-key"}


def test_lifespan_initializes_database_once_per_startup(monkeypatch):
    from app.api import main

    calls = []
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init"))
    assert calls == []
    with TestClient(app) as started_client:
        assert calls == ["init"]
        assert started_client.get("/healthz").status_code == 200
        assert started_client.get("/healthz").status_code == 200
        assert calls == ["init"]
    assert calls == ["init"]
    with TestClient(app):
        assert calls == ["init", "init"]


def test_lifespan_does_not_hide_database_initialization_failure(monkeypatch):
    from app.api import main

    def fail_init():
        raise RuntimeError("database initialization failed")

    monkeypatch.setattr(main, "init_db", fail_init)
    with pytest.raises(RuntimeError, match="database initialization failed"):
        with TestClient(app):
            pytest.fail("startup must fail before accepting requests")


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
    assert r2.json()["max_attempts"] == settings.TASK_MAX_RETRIES
    assert r2.json()["deadline_at"] is None  # 首次领取才开始诊断预算。
    assert "lease_token" not in r2.json()  # 领取凭证不是面向客户端的状态信息。


def test_diagnostics_idempotent():
    payload = {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}
    r1 = client.post("/api/v1/diagnostics", headers=HEADERS, json=payload)
    r2 = client.post("/api/v1/diagnostics", headers=HEADERS, json=payload)
    assert r1.status_code == 201
    assert r1.json()["task_id"] == r2.json()["task_id"]


def test_task_not_found():
    r = client.get("/api/v1/tasks/999999", headers=HEADERS)
    assert r.status_code == 404


def test_import_csv(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["data:import"]})
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


@pytest.fixture
def feedback_report(monkeypatch):
    run_id = uuid4().hex
    with write_session() as s:
        s.add(DiagnosticReport(run_id=run_id, item_id=1, content_json="{}", model="mock"))
        s.commit()
    monkeypatch.setattr(settings, "LOG_DIR", str(Path(settings.LOG_DIR).parent / run_id / "logs"))
    return run_id


def test_feedback(feedback_report, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["feedback:create"]})
    r = client.post("/api/v1/feedback", headers=HEADERS, json={
        "run_id": feedback_report, "rating": 2, "category": "analysis", "comment": "结论不准"})
    assert r.status_code == 200
    assert "path" not in r.json()
    feedback_id = r.json()["feedback_id"]
    base = Path(settings.LOG_DIR).parent / "feedback" / "agent_feedback"
    saved = json.loads((base / f"{feedback_id}.json").read_text(encoding="utf-8"))
    assert saved["records"][0]["run_id"] == feedback_report
    second = client.post("/api/v1/feedback", headers=HEADERS, json={
        "run_id": feedback_report, "rating": 4})
    assert second.status_code == 200
    assert second.json()["feedback_id"] != feedback_id
    from app.monitoring_history import collect_feedback

    assert collect_feedback()["total"] == 2


@pytest.mark.parametrize("change", [
    {"run_id": "../../escape"}, {"run_id": "..\\escape"},
    {"run_id": "C:\\escape"}, {"run_id": "x" * 33},
    {"category": '<img src=x onerror="alert(1)">'}, {"comment": "x" * 4001},
])
def test_feedback_rejects_unsafe_input(feedback_report, change):
    response = client.post("/api/v1/feedback", headers=HEADERS, json={
        "run_id": feedback_report, "rating": 3, **change})
    assert response.status_code == 422
    assert not (Path(settings.LOG_DIR).parent / "feedback").exists()


def test_feedback_requires_existing_report():
    response = client.post("/api/v1/feedback", headers=HEADERS, json={
        "run_id": "missing-report", "rating": 3})
    assert response.status_code == 404


@pytest.mark.parametrize("change", [
    {"start_date": "2015-07-01", "end_date": "2015-06-01"},
    {"start_date": "2015-01-01", "end_date": "2015-06-01"},
    {"item_id": True}, {"item_id": 1.5}, {"anomaly_id": -1},
    {"scopes": ["data:import"]},
])
def test_invalid_diagnostic_never_enqueues(monkeypatch, change):
    def must_not_enqueue(*args, **kwargs):
        raise AssertionError("invalid request reached the queue")

    monkeypatch.setattr("app.api.diagnostics.create_task", must_not_enqueue)
    response = client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14", **change})
    assert response.status_code == 422


def test_diagnostic_rejects_missing_or_mismatched_anomaly(monkeypatch):
    from types import SimpleNamespace
    from app.api import diagnostics

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, model, key):
            return None if key == 1 else SimpleNamespace(item_id=2)

    monkeypatch.setattr(diagnostics, "read_session", FakeSession)
    for anomaly_id, code in [(1, 404), (2, 422)]:
        response = client.post("/api/v1/diagnostics", headers=HEADERS, json={
            "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14", "anomaly_id": anomaly_id})
        assert response.status_code == code


def test_read_key_cannot_write_even_when_also_in_legacy_keys(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["report:read"]})
    assert client.get("/api/v1/monitoring", headers=HEADERS).status_code == 200
    assert client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}).status_code == 403
    assert client.post("/api/v1/feedback", headers=HEADERS, json={
        "run_id": "anything", "rating": 3}).status_code == 403
    assert client.post("/api/v1/import/daily-stat", headers=HEADERS,
                       files={"file": ("x.csv", b"a,b\n", "text/csv")}).status_code == 403


@pytest.mark.parametrize("path", [
    "/tasks/999999", "/monitoring", "/monitoring/history", "/monitoring/slow-queries",
    "/monitoring/cost", "/monitoring/feedback", "/monitoring/alerts",
    "/monitoring/anomalies", "/monitoring/reports/999999",
])
def test_import_key_cannot_read_reports(monkeypatch, path):
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["data:import"]})
    assert client.get("/api/v1" + path, headers=HEADERS).status_code == 403


def test_diagnosis_scope_allows_submission(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["diagnosis:create"]})
    response = client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"})
    assert response.status_code == 201


def test_mcp_scope_does_not_grant_http_business_access(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["tools:read"]})
    assert client.get("/api/v1/monitoring", headers=HEADERS).status_code == 403
    assert client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}).status_code == 403


def test_production_disables_legacy_keys_and_sync(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert client.get("/api/v1/monitoring", headers=HEADERS).status_code == 401
    monkeypatch.setattr(settings, "API_KEY_SCOPES", {"test-key": ["diagnosis:create"]})
    response = client.post("/api/v1/diagnostics", headers=HEADERS, json={
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14", "sync": True})
    assert response.status_code == 403


_CSV_HEADER = "item_id,stat_date,dimension_type,dimension,uv,view_count,click_count,addtocart_count,transaction_count,gmv\n"


@pytest.mark.parametrize("bad_values", [
    "-1,30,30,2,1,1", "1.5,30,30,2,1,1", "10,30,30,2,1,NaN",
    "10,30,30,2,1,inf", "10,30,30,2,1,-1", "10,30,30,2,1,1,extra",
])
def test_csv_invalid_row_rolls_back_whole_file(bad_values):
    with write_session() as s:
        before = s.query(DailyItemStat).count()
    csv_data = (_CSV_HEADER + "998,2015-07-01,all,all,10,30,30,2,1,1\n"
                + f"997,2015-07-01,all,all,{bad_values}\n")
    response = client.post("/api/v1/import/daily-stat", headers=HEADERS,
                           files={"file": ("x.csv", csv_data.encode(), "text/csv")})
    assert response.status_code == 400
    with write_session() as s:
        assert s.query(DailyItemStat).count() == before


def test_csv_limits_size_and_rows(monkeypatch):
    csv_data = _CSV_HEADER + "996,2015-07-01,all,all,10,30,30,2,1,1\n"
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", len(csv_data.encode()) - 1)
    assert client.post("/api/v1/import/daily-stat", headers=HEADERS,
                       files={"file": ("x.csv", csv_data.encode(), "text/csv")}).status_code == 413
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 100000)
    monkeypatch.setattr(settings, "MAX_UPLOAD_ROWS", 1000)
    with write_session() as s:
        before = s.query(DailyItemStat).count()
    # 第 1000 行已经 flush 到数据库；第 1001 行越界仍应回滚整份文件。
    csv_data = _CSV_HEADER + "995,2015-07-01,all,all,10,30,30,2,1,1\n" * 1001
    assert client.post("/api/v1/import/daily-stat", headers=HEADERS,
                       files={"file": ("x.csv", csv_data.encode(), "text/csv")}).status_code == 413
    with write_session() as s:
        assert s.query(DailyItemStat).count() == before


def test_request_body_limit_including_chunked_transfer():
    for content in [b" " * 70000, iter([b" " * 35000, b" " * 35000])]:
        response = client.post("/api/v1/feedback", headers={**HEADERS, "Content-Type": "application/json"},
                               content=content)
        assert response.status_code == 413


def test_rate_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "API_RATE_LIMIT_PER_MINUTE", 1)
    assert client.get("/api/v1/monitoring", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/monitoring", headers=HEADERS).status_code == 429


_PRODUCTION_SETTINGS = dict(
    APP_ENV="production", API_KEYS="", API_KEY_SCOPES={"k" * 32: ["report:read"]},
    LLM_API_KEY="test-provider-key", DB_DRIVER="mysql", DB_READ_USER="reader", DB_WRITE_USER="writer",
    DB_READ_PASSWORD="test-read-password", DB_WRITE_PASSWORD="test-write-password",
)


def test_valid_production_settings():
    configured = Settings(_env_file=None, **_PRODUCTION_SETTINGS)
    assert configured.APP_ENV == "production"


@pytest.mark.parametrize("change", [
    {"API_KEYS": "dev-key-123"}, {"API_KEY_SCOPES": {}},
    {"API_KEY_SCOPES": {"short": ["report:read"]}}, {"API_KEY_SCOPES": {"k" * 32: []}},
    {"API_KEY_SCOPES": {"k" * 32: ["typo:scope"]}}, {"LLM_API_KEY": ""},
    {"DB_DRIVER": "sqlite"}, {"DB_READ_USER": "writer"}, {"DB_READ_PASSWORD": "change_me"},
])
def test_unsafe_production_settings_fail_closed(change):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**_PRODUCTION_SETTINGS, **change})


def test_monitoring_endpoint():
    r = client.get("/api/v1/monitoring?window_hours=24", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "agent_runs" in body and "tool_calls" in body and "tasks" in body
    # 未带 Key 应 401
    r2 = client.get("/api/v1/monitoring")
    assert r2.status_code == 401
