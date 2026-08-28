"""真实 MCP Client/Server 协议测试；业务库与日志由 conftest 隔离。"""
import asyncio
import json
from pathlib import Path
from threading import Event

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from app.agent import default_registry
from app.config import settings
from app.mcp_server import EXPORTED_TOOLS, OUTPUT_SCHEMA, create_server
from app.tracing import current_run_id, set_run_id

KEY = "mcp-test-only-credential-32-characters"
ARGS = {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}


@pytest.fixture
def config():
    return settings.model_copy(update={
        "MCP_ENABLED": True, "MCP_ACCESS_KEY": KEY, "API_KEY_SCOPES": {KEY: ["tools:read"]},
        "MCP_TOOL_TIMEOUT_SECONDS": 2.0, "MCP_MAX_CONCURRENCY": 2,
        "MCP_MAX_RESULT_BYTES": 128 * 1024, "MCP_CALLS_PER_MINUTE": 60,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_protocol_walkthrough(config, mode, monkeypatch):
    registry = default_registry()
    server = create_server(config=config, registry=registry)
    # 构造 Server 和执行工具均不得触发建表/业务写库或模型调用。
    import app.db as db
    monkeypatch.setattr(db, "get_write_engine", lambda: pytest.fail("MCP 不得获取写连接"))
    async with Client(server, mode=mode, cache=None) as client:
        listing = await client.list_tools()
        assert {t.name for t in listing.tools} == set(EXPORTED_TOOLS)
        print(f"[{mode}] 1. MCP 发现工具：" + ", ".join(t.name for t in listing.tools))
        for tool in listing.tools:
            assert tool.input_schema == registry.get(tool.name).parameters
            assert tool.output_schema == OUTPUT_SCHEMA
            assert tool.annotations.read_only_hint is True
            args = {**ARGS, **({"dimension": "new_user"} if tool.name == "dimension" else {})}
            reply = await client.call_tool(tool.name, args)
            data = reply.structured_content
            assert reply.is_error is False
            assert json.loads(reply.content[0].text) == data
            direct = registry.execute(tool.name, args, run_id="mcp-direct-test", step=1)
            assert data["data"] == direct["data"]
            assert data["rows"] == direct["rows"]
            assert data["error"] is None
            assert "causal_unverified" in data["evidence_limits"]
            assert data["call_id"] == f"{tool.name}#{data['run_id']}"
            assert "_meta" not in data
            assert (Path(settings.LOG_DIR) / "sql_logs" / f"{data['run_id']}.jsonl").exists()
            print(f"[{mode}] 2. {tool.name}：ok={data['ok']}，rows={data['rows']}，与原工具 data 一致")
        bad = await client.call_tool("metric", {**ARGS, "run_id": "../../must-not-be-used"})
        assert bad.is_error is True
        assert bad.structured_content["error"]["code"] == "invalid_arguments"
        assert "must-not-be-used" not in bad.content[0].text
        print(f"[{mode}] 3. 客户端伪造 run_id：拒绝；成功证据包含服务端 evidence_limits")


@pytest.mark.parametrize("changes", [
    {"MCP_ENABLED": False}, {"MCP_ACCESS_KEY": ""}, {"MCP_ACCESS_KEY": "short"},
    {"API_KEY_SCOPES": {KEY: ["report:read"]}},
    {"API_KEY_SCOPES": {}, "API_KEYS": KEY},
])
def test_explicit_mcp_authorization_required(config, changes):
    with pytest.raises(PermissionError):
        create_server(config=config.model_copy(update=changes))


@pytest.mark.asyncio
async def test_scope_revocation_checked_on_each_request(config):
    async with Client(create_server(config=config), cache=None) as client:
        await client.list_tools()
        config.API_KEY_SCOPES = {KEY: ["report:read"]}
        with pytest.raises(MCPError, match="未获授权"):
            await client.call_tool("metric", ARGS)
        with pytest.raises(MCPError, match="未获授权"):
            await client.list_tools()


@pytest.mark.asyncio
async def test_unknown_and_future_tools_are_not_exported(config):
    registry = default_registry()
    extra = type("Extra", (), {"name": "write_inventory", "description": "never export", "parameters": {}})()
    registry.register(extra)
    async with Client(create_server(config=config, registry=registry), cache=None) as client:
        assert "write_inventory" not in {t.name for t in (await client.list_tools()).tools}
        for name in ("shell", "write_inventory"):
            with pytest.raises(MCPError, match="未知或未导出") as error:
                await client.call_tool(name, {})
            assert error.value.code == -32602


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"item_id": True}, {"item_id": 1.5}, {"item_id": "1"}, {"item_id": -1},
    {"start_date": "20150601"}, {"start_date": "2015-02-30"},
    {"start_date": "2015-06-15"}, {"start_date": "2015-01-01"},
    {"metrics": []}, {"metrics": None}, {"metrics": ["uv", "uv"]},
    {"metrics": ["not-a-metric"]}, {"metrics": [True]},
    {"api_key": "never-echo-this"}, {"item_id": None},
])
async def test_invalid_arguments_never_query(config, changes, monkeypatch):
    import app.metrics.compute as compute
    monkeypatch.setattr(compute, "_rows", lambda *a, **k: pytest.fail("非法参数不能执行 SQL"))
    async with Client(create_server(config=config), cache=None) as client:
        result = await client.call_tool("metric", {**ARGS, **changes})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_arguments"
    assert result.structured_content["data"] is None
    assert "never-echo-this" not in result.content[0].text


@pytest.mark.asyncio
async def test_peer_checks_window_before_missing_category(config, monkeypatch):
    from app.metrics import compute
    monkeypatch.setattr(compute, "item_category_id", lambda *a: pytest.fail("应在查询类目前拒绝窗口"))
    async with Client(create_server(config=config), cache=None) as client:
        result = await client.call_tool("peer", {**ARGS, "start_date": "2015-06-15"})
    assert result.structured_content["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [("metric", {}), ("dimension", ARGS),
                                      ("dimension", {**ARGS, "dimension": "arbitrary-column"})])
async def test_missing_and_dimension_arguments_rejected(config, name, args):
    async with Client(create_server(config=config), cache=None) as client:
        result = await client.call_tool(name, args)
    assert result.structured_content["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [True, False])
async def test_failure_is_not_evidence_and_exception_is_redacted(config, monkeypatch, raises):
    registry = default_registry()
    secret = "mysql://secret-user:secret-password@host/private-table"

    def fail(**kwargs):
        if raises:
            raise RuntimeError(secret)
        return {"ok": False, "text": secret, "rows": 9, "data": {"private": secret}}

    monkeypatch.setattr(registry.get("metric"), "run", fail)
    async with Client(create_server(config=config, registry=registry), cache=None) as client:
        result = await client.call_tool("metric", ARGS)
    assert result.is_error is True
    assert result.structured_content["data"] is None
    assert result.structured_content["rows"] == 0
    assert result.structured_content["error"]["code"] == "execution_error"
    assert secret not in result.model_dump_json()


@pytest.mark.asyncio
async def test_large_result_rejected_without_partial_evidence(config, monkeypatch):
    config.MCP_MAX_RESULT_BYTES = 4096
    registry = default_registry()
    monkeypatch.setattr(registry.get("metric"), "run", lambda **kw: {
        "ok": True, "text": "内容" * 3000, "rows": 1, "data": {"full": "not-partial"},
    })
    async with Client(create_server(config=config, registry=registry), cache=None) as client:
        result = await client.call_tool("metric", ARGS)
    assert result.structured_content["error"]["code"] == "result_too_large"
    assert result.structured_content["data"] is None
    assert len(result.model_dump_json(by_alias=True).encode("utf-8")) <= 4096


@pytest.mark.asyncio
async def test_audit_failure_does_not_deliver_data(config, monkeypatch):
    import app.mcp_server as module

    def fail(*args, **kwargs):
        raise OSError("private-log-path")

    monkeypatch.setattr(module, "log_agent_step", fail)
    async with Client(create_server(config=config), cache=None) as client:
        result = await client.call_tool("metric", ARGS)
    assert result.structured_content["error"]["code"] == "audit_unavailable"
    assert result.structured_content["data"] is None
    assert "private-log-path" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"rows": -1}, {"data": []}, {"text": None}, {"data": {"uv": float("nan")}}])
async def test_invalid_tool_output_cannot_be_success(config, monkeypatch, change):
    registry = default_registry()
    monkeypatch.setattr(registry, "execute", lambda *a, **kw: {
        "ok": True, "text": "done", "rows": 0, "data": {}, **change,
    })
    async with Client(create_server(config=config, registry=registry), cache=None) as client:
        result = await client.call_tool("metric", ARGS)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "execution_error"


@pytest.mark.asyncio
async def test_rate_limit_does_not_retry(config):
    config.MCP_CALLS_PER_MINUTE = 1
    async with Client(create_server(config=config), cache=None) as client:
        first = await client.call_tool("funnel", ARGS)
        second = await client.call_tool("funnel", ARGS)
    assert first.is_error is False
    assert second.structured_content["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_timeout_keeps_capacity_until_actual_completion(config, monkeypatch):
    config.MCP_TOOL_TIMEOUT_SECONDS = 0.05
    config.MCP_MAX_CONCURRENCY = 1
    registry = default_registry()
    release = Event()
    finished = Event()
    calls = []

    def block(**kwargs):
        calls.append(1)
        try:
            assert release.wait(3), "测试未释放线程"
            return {"ok": True, "text": "done", "rows": 0, "data": {}}
        finally:
            finished.set()

    monkeypatch.setattr(registry.get("metric"), "run", block)
    try:
        async with Client(create_server(config=config, registry=registry), cache=None) as client:
            timed_out = await client.call_tool("metric", ARGS)
            assert timed_out.structured_content["error"]["code"] == "timeout"
            assert timed_out.structured_content["error"]["retryable"] is False
            busy = await client.call_tool("funnel", ARGS)
            assert busy.structured_content["error"]["code"] == "busy"
            assert calls == [1]
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            for _ in range(100):
                await asyncio.sleep(0.01)
                available = await client.call_tool("funnel", ARGS)
                if not available.is_error:
                    break
            assert available.is_error is False
    finally:
        release.set()


@pytest.mark.asyncio
async def test_concurrent_context_and_ids_are_isolated(config):
    parent_id = set_run_id("parent-mcp-context")
    async with Client(create_server(config=config), cache=None) as client:
        results = await asyncio.gather(
            client.call_tool("metric", ARGS),
            client.call_tool("metric", {**ARGS, "item_id": 2}),
        )
    assert current_run_id() == parent_id
    ids = [r.structured_content["run_id"] for r in results]
    assert len(set(ids)) == 2
    assert len({r.structured_content["call_id"] for r in results}) == 2
    for result in results:
        assert result.is_error is False
        run_id = result.structured_content["run_id"]
        lines = (Path(settings.LOG_DIR) / "sql_logs" / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        assert lines and all(json.loads(line)["run_id"] == run_id for line in lines)


@pytest.mark.asyncio
async def test_client_cancellation_does_not_release_running_query(config, monkeypatch):
    config.MCP_MAX_CONCURRENCY = 1
    registry = default_registry()
    entered, release, finished = Event(), Event(), Event()

    def block(**kwargs):
        entered.set()
        try:
            assert release.wait(3)
            return {"ok": True, "text": "late", "rows": 0, "data": {}}
        finally:
            finished.set()

    monkeypatch.setattr(registry.get("metric"), "run", block)
    try:
        async with Client(create_server(config=config, registry=registry), cache=None) as client:
            pending = asyncio.create_task(client.call_tool("metric", ARGS))
            assert await asyncio.to_thread(entered.wait, 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            busy = await client.call_tool("funnel", ARGS)
            assert busy.structured_content["error"]["code"] == "busy"
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            # 给工具日志写完和 done callback 释放槽位留出明确的可观察终点。
            for _ in range(100):
                await asyncio.sleep(0.01)
                result = await client.call_tool("funnel", ARGS)
                if not result.is_error:
                    break
            assert result.is_error is False
    finally:
        release.set()


def test_entrypoint_refuses_disabled_config_without_stdout(monkeypatch, capsys):
    import app.mcp_server as module
    from scripts import run_mcp

    monkeypatch.setattr(module.settings, "MCP_ENABLED", False)
    assert run_mcp.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "启动被拒" in captured.err
