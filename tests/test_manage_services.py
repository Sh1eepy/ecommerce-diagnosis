"""服务管理失败不能冒充成功；只模拟进程，不启动或终止真实服务。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import manage_services as services


@pytest.fixture
def service_dir(monkeypatch):
    directory = Path(__file__).resolve().parent / ".pytest_runtime" / "service-manager"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(services, "SVC_DIR", directory)
    monkeypatch.setattr(services.time, "sleep", lambda _: None)
    return directory


def test_failed_stop_preserves_pid_for_recovery(service_dir, monkeypatch, capsys):
    pid = service_dir / "api.pid"
    pid.write_text("123", encoding="utf-8")
    monkeypatch.setattr(services, "_is_running", lambda _: True)
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="停止失败"):
        services.stop("api")
    assert pid.read_text(encoding="utf-8") == "123"
    assert "已停止" not in capsys.readouterr().out


def test_success_exit_code_does_not_hide_still_running_process(service_dir, monkeypatch):
    pid = service_dir / "api.pid"
    pid.write_text("123", encoding="utf-8")
    monkeypatch.setattr(services, "_is_running", lambda _: True)
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    clock = iter([0, 4])
    monkeypatch.setattr(services.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="进程仍存活"):
        services.stop("api")
    assert pid.exists()


def test_stop_removes_pid_only_after_confirmed_exit(service_dir, monkeypatch):
    pid = service_dir / "api.pid"
    pid.write_text("123", encoding="utf-8")
    states = iter([True, False, False])
    monkeypatch.setattr(services, "_is_running", lambda _: next(states))
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    services.stop("api")
    assert not pid.exists()


def test_immediate_start_failure_does_not_replace_previous_pid(service_dir, monkeypatch, capsys):
    pid = service_dir / "api.pid"
    pid.write_text("123", encoding="utf-8")
    monkeypatch.setattr(services, "_is_running", lambda _: False)
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=456, poll=lambda: 1))
    with pytest.raises(RuntimeError, match="启动进程已退出"):
        services.start("api")
    assert pid.read_text(encoding="utf-8") == "123"
    assert "进程已启动" not in capsys.readouterr().out
