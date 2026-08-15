"""日志轮转测试：JSONL 超过阈值自动滚动（防止 cli.jsonl 无限增长）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import tracing
from app.config import settings
from app.tracing import _append


def test_log_rotation_triggers(monkeypatch):
    """写入内容超过阈值后，下次写入触发滚动：旧文件留档、新文件重建。"""
    # 阈值调小便于测试（该值可在测试内 monkeypatch）
    monkeypatch.setattr(tracing, "MAX_LOG_BYTES", 60)
    run_id = "rot-test"
    base = Path(settings.LOG_DIR) / "sql_logs"
    base.mkdir(parents=True, exist_ok=True)
    for f in base.glob(f"{run_id}*.jsonl"):
        f.unlink()

    # 第一行写入（一行 JSON 通常 >60 字节，触发后续滚动）
    _append("sql_logs", run_id, {"statement": "SELECT x" * 30, "duration_ms": 1.5})
    # 第二行写入：检测到旧文件超限 → 滚动
    _append("sql_logs", run_id, {"statement": "SELECT y", "duration_ms": 2.5})

    files = sorted(base.glob(f"{run_id}*.jsonl"), key=lambda f: len(f.name))
    # 应有：原文件（新数据）+ 至少 1 个滚动留档；最短文件名 = 无时间戳后缀的最新文件
    assert len(files) >= 2
    new_file = files[0]
    assert new_file.name == f"{run_id}.jsonl"
    lines = [json.loads(l) for l in new_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["statement"] == "SELECT y"
    # 留档文件包含第一行
    archived = [json.loads(l) for f in files[1:] for l in f.read_text(encoding="utf-8").splitlines()]
    assert any(a["statement"].startswith("SELECT x") for a in archived)
    # 清理
    for f in files:
        f.unlink()


def test_log_no_rotation_when_small():
    """小日志不触发滚动。"""
    run_id = "rot-small"
    base = Path(settings.LOG_DIR) / "tool_calls"
    base.mkdir(parents=True, exist_ok=True)
    for f in base.glob(f"{run_id}*.jsonl"):
        f.unlink()
    _append("tool_calls", run_id, {"tool": "metric", "ok": True})
    files = list(base.glob(f"{run_id}*.jsonl"))
    assert len(files) == 1
    files[0].unlink()
