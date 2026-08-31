"""原生扩展接口验收：不接触真实供应商/数据库服务。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import ToolException, tool
from langsmith import tracing_context
from langsmith.run_helpers import get_tracing_context
from openai import APIStatusError
from pydantic import Field

from app.agent.agent import Agent, _normalize_report, _parse_decision, _partial_report
from app.agent.context import build_initial_messages
from app.agent.graph import InvestigationContext, InvestigationGraph, LoopState
from app.agent.state import RunBudget
from app.agent.tool import ToolRegistry
from app.llm.errors import LLMCallError
from app.llm.langchain_adapter import NativeChatClient, ProviderChatModel, invoke_chat
from app.llm.mock import MockLLM


class ScriptChatModel(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    calls: list[dict] = Field(default_factory=list)
    error: Any = None
    max_retries: int = 0
    model_name: str = "native-test"

    @property
    def _llm_type(self):
        return "native-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"messages": messages, "options": kwargs,
                           "tracing": get_tracing_context()["enabled"]})
        if self.error is not None:
            raise self.error
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def response(content):
    return AIMessage(content=json.dumps(content), usage_metadata={
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def report(value):
    return {"facts": [{"point": f"已记录访客数为{value}", "metric": "uv", "value": value,
                       "evidence_ref": {"call_id": "metric#1", "path": "summary.current.uv"}}],
            "hypotheses": [], "analysis": {"attribution_status": "uncertain", "primary_hypothesis_id": None,
                                           "limitations": ["尚无因果验证"]},
            "conclusion": "原因待核查", "suggestions": [{"action": "核对业务记录", "rationale": "补足原因证据",
                "owner": "运营", "priority": "P1", "success_metric": "取得业务核查记录"}]}


def test_standard_model_runs_agent_without_legacy_provider(monkeypatch):
    monkeypatch.setattr(ProviderChatModel, "_generate", lambda *a, **k: pytest.fail("不应再包装原生模型"))
    model = ScriptChatModel(responses=[response({"type": "tool_call", "tool": "metric", "args": {
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}}),
        response({"type": "final", "report": report(14000)})])
    result = Agent(llm=model).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok" and result["quality"]["passed"]
    assert result["model"] == "native-test" and result["llm_attempts"] == 2
    assert len(model.calls) == 2 and result["tool_calls"] == 1
    assert all(call["options"]["max_tokens"] > 0 and call["options"]["timeout"] > 0 for call in model.calls)


def test_native_model_options_usage_and_no_extra_call():
    model = ScriptChatModel(responses=[response({"ok": True})])
    client = NativeChatClient(model, max_tokens_parameter="max_output_tokens", timeout_parameter="request_timeout",
                              json_mode_options={"response_format": {"type": "json_object"}})
    result = invoke_chat(client, [{"role": "user", "content": "task"}], timeout=3, max_tokens=600)
    assert (result.tokens_in, result.tokens_out, result.attempts) == (10, 5, 1)
    assert model.calls[0]["options"] == {"max_output_tokens": 600, "request_timeout": 3,
                                         "response_format": {"type": "json_object"}}
    direct = ScriptChatModel(responses=[response({"direct": True})])
    assert invoke_chat(direct, [{"role": "user", "content": "task"}]).tokens_in == 10
    assert len(direct.calls) == 1


@pytest.mark.parametrize("options", [{"timeout": 100}, {"max_tokens": 10000}, {"callbacks": []}, {"config": {}}])
def test_native_json_options_cannot_override_execution_controls(options):
    with pytest.raises(ValueError):
        NativeChatClient(ScriptChatModel(), json_mode_options=options)


def test_native_retries_must_be_disabled_at_model_construction():
    with pytest.raises(ValueError, match="max_retries=0"):
        Agent(llm=ScriptChatModel(max_retries=2))


@pytest.mark.parametrize("status,code,kind", [(429, "rate_limit", "retryable"),
    (429, "insufficient_quota", "permanent"), (401, "invalid_key", "permanent")])
def test_native_failures_preserve_classification_without_hidden_retry(status, code, kind):
    error = APIStatusError("secret-body", response=httpx.Response(status, headers={"retry-after": "3"},
        request=httpx.Request("POST", "https://model.invalid")), body={"error": {"code": code}})
    model = ScriptChatModel(error=error)
    with pytest.raises(LLMCallError) as caught:
        invoke_chat(NativeChatClient(model), [{"role": "user", "content": "task"}])
    assert caught.value.kind == kind and caught.value.attempts == 1
    assert caught.value.retry_after_seconds == 3
    assert len(model.calls) == 1 and "secret-body" not in str(caught.value)


@pytest.mark.parametrize("reply,reason", [(AIMessage(content="{}"), "usage_missing"),
    (AIMessage(content="", tool_calls=[{"name": "metric", "args": {}, "id": "1"}]), "invalid_response")])
def test_native_unsupported_responses_stop_instead_of_free_or_implicit_tool_calls(reply, reason):
    model = ScriptChatModel(responses=[reply])
    with pytest.raises(LLMCallError) as caught:
        invoke_chat(NativeChatClient(model), [{"role": "user", "content": "task"}])
    assert caught.value.stop_reason == reason and not caught.value.retryable
    assert len(model.calls) == 1


def test_native_model_does_not_bypass_budget_preflight(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 100)
    model = ScriptChatModel()
    result = Agent(llm=model).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["stop_reason"] == "token_budget" and not model.calls


def test_native_model_recovery_keeps_saved_usage_and_does_not_repeat_tool():
    from app.agent.checkpoint import load_checkpoint, decode_state

    class CrashAfterResponse(ScriptChatModel):
        def _generate(self, *args, **kwargs):
            if not self.responses:
                raise KeyboardInterrupt("simulated process interruption")
            return super()._generate(*args, **kwargs)

    rid = "native-model-recovery"
    model = CrashAfterResponse(responses=[response({"type": "tool_call", "tool": "metric", "args": {
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}})])
    with pytest.raises(KeyboardInterrupt):
        Agent(llm=model).run(1, date(2015, 6, 1), date(2015, 6, 14), run_id=rid)
    saved = decode_state(load_checkpoint(rid))
    deadline = saved["budget"]["deadline_at"]
    assert saved["tokens_in"] + saved["tokens_out"] == 15
    resumed = ScriptChatModel(responses=[response({"type": "final", "report": report(14000)})])
    result = Agent(llm=resumed).run(1, date(2015, 6, 1), date(2015, 6, 14), run_id=rid)
    assert result["status"] == "ok" and result["tool_calls"] == 1 and len(resumed.calls) == 1
    saved = decode_state(load_checkpoint(rid))
    assert saved["tokens_in"] + saved["tokens_out"] == 30
    assert saved["budget"]["deadline_at"] == deadline


def test_native_cache_and_callbacks_do_not_change_execution(monkeypatch):
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import get_llm_cache, set_llm_cache

    class ForbiddenCallback(BaseCallbackHandler):
        raise_error = True

        def on_chat_model_start(self, *args, **kwargs):
            pytest.fail("实例 callback 不得外发")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr("langchain_core.tracers.langchain.LangChainTracer.__init__",
                        lambda *a, **k: pytest.fail("不应创建远程 tracer"))
    model = ScriptChatModel(responses=[response({"n": 1}), response({"n": 2})],
                            callbacks=[ForbiddenCallback()], cache=True)
    client = NativeChatClient(model)
    previous = get_llm_cache()
    try:
        set_llm_cache(InMemoryCache())
        with tracing_context(enabled=True):
            outputs = [invoke_chat(client, [{"role": "user", "content": "same"}]).content for _ in range(2)]
            assert get_tracing_context()["enabled"] is True
    finally:
        set_llm_cache(previous)
    assert outputs == ['{"n": 1}', '{"n": 2}']
    assert len(model.calls) == 2 and all(c["tracing"] is False for c in model.calls)
    assert model.cache is True and model.callbacks  # 不改写调用方实例


def test_decorated_tool_registers_without_project_base_class_and_uses_defaults():
    @tool
    def count_records(item_id: int, multiplier: int = 2) -> dict:
        """只读示例。"""
        return {"count": item_id * multiplier}

    registry = ToolRegistry([count_records])
    result = registry.execute("count_records", {"item_id": 3}, run_id="native-tool", step=1)
    assert result["ok"] and result["data"] == {"count": 6}
    assert registry.describe()[0]["parameters"] == registry.input_schema("count_records")
    for bad in ({"item_id": True}, {"item_id": "3"}, {"item_id": 3, "run_id": "forged"}):
        assert registry.execute("count_records", bad, run_id="native-tool", step=2)["ok"] is False


def test_registry_refuses_duplicate_or_non_native_tools():
    @tool
    def lookup() -> str:
        """示例。"""
        return "value"

    registry = ToolRegistry([lookup])
    with pytest.raises(ValueError):
        registry.register(lookup)
    with pytest.raises(TypeError):
        registry.register(object())


def test_native_tool_receives_execution_identity_through_framework_config():
    from langchain_core.runnables import RunnableConfig

    @tool
    def lookup(item_id: int, config: RunnableConfig) -> dict:
        """从框架上下文读取身份，不让模型填写身份。"""
        return {"item_id": item_id, "run": config["configurable"]["diagnosis_run_id"],
                "step": config["configurable"]["diagnosis_step"]}

    registry = ToolRegistry([lookup])
    assert set(registry.input_schema("lookup")["properties"]) == {"item_id"}
    result = registry.execute("lookup", {"item_id": 1}, run_id="trusted-run", step=2)
    assert result["data"] == {"item_id": 1, "run": "trusted-run", "step": 2}
    assert not registry.execute("lookup", {"item_id": 1, "config": {}}, run_id="trusted-run", step=2)["ok"]


def test_native_tool_callbacks_are_not_inherited():
    class ForbiddenCallback(BaseCallbackHandler):
        raise_error = True

        def on_tool_start(self, *args, **kwargs):
            pytest.fail("工具实例 callback 不得外发")

    @tool
    def lookup() -> str:
        """普通工具。"""
        return "ok"

    lookup.callbacks = [ForbiddenCallback()]
    with tracing_context(enabled=True):
        result = ToolRegistry([lookup]).execute("lookup", {}, run_id="callback-tool", step=1)
    assert result["ok"] and result["data"] == "ok" and lookup.callbacks


def test_native_handled_error_is_not_successful_evidence():
    @tool
    def broken() -> str:
        """失败工具。"""
        raise ToolException("query failed")

    broken.handle_tool_error = True
    result = ToolRegistry([broken]).execute("broken", {}, run_id="native-error", step=1)
    assert result["ok"] is False and result["data"] is None
    assert broken.handle_tool_error is True


def test_native_artifact_is_preserved_as_structured_evidence():
    @tool(response_format="content_and_artifact")
    def lookup() -> tuple:
        """结构化取证示例。"""
        return "two records", {"count": 2}

    result = ToolRegistry([lookup]).execute("lookup", {}, run_id="native-artifact", step=1)
    assert result["ok"] and result["text"] == "two records" and result["data"] == {"count": 2}


def test_graph_runtime_isolates_concurrent_invocations():
    graph, barrier = InvestigationGraph(), Barrier(2)

    def execute(item_id):
        @tool("metric")
        def query(item_id: int) -> dict:
            """并发只读示例。"""
            barrier.wait(timeout=5)
            return {"summary": {"current": {"uv": item_id}}}

        registry, saves, logs = ToolRegistry([query]), [], []
        llm = MockLLM(plan=[{"type": "tool_call", "tool": "metric", "args": {"item_id": item_id}},
                            {"type": "final", "report": report(item_id)}])
        ctx = InvestigationContext(llm=llm, registry=registry,
            budget=RunBudget(deadline_at=time.time() + 30, seconds_limit=30, max_steps=4, token_limit=10000),
            run_id=f"context-{item_id}", remaining_seconds=lambda: 30, check_owner=lambda: None,
            save_step=lambda state: saves.append(state.investigation.to_dict()),
            parse_decision=_parse_decision, normalize_report=_normalize_report, partial_report=_partial_report,
            log_step=lambda rid, *args: logs.append(rid), clock=time, max_output_tokens=1000, step_timeout_seconds=10)
        state = LoopState(messages=build_initial_messages(registry.describe(), item_id, "2015-06-01", "2015-06-14"))
        result = graph.invoke(state, context=ctx)
        return result, saves, logs

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, (1, 2)))
    for item_id, (state, saves, logs) in enumerate(results, 1):
        assert state.stop_reason == "final" and state.report["facts"][0]["value"] == item_id
        assert len(saves) == 1 and saves[0]["evidence"]["metric#1"]["args"] == {"item_id": item_id}
        assert set(logs) == {f"context-{item_id}"}
    assert "llm" not in graph.compiled.get_input_jsonschema().get("properties", {})


def test_inspection_does_not_connect_to_models_or_databases(monkeypatch):
    from app import db, llm
    from scripts.inspect_agent import inspect_agent

    def forbidden(*args, **kwargs):
        pytest.fail("结构检查不得执行模型或数据库操作")

    monkeypatch.setattr(db, "get_read_engine", forbidden)
    monkeypatch.setattr(db, "get_write_engine", forbidden)
    monkeypatch.setattr(llm, "get_llm", forbidden)
    snapshot = json.loads(inspect_agent())
    assert {t["name"] for t in snapshot["tools"]} == {"metric", "funnel", "peer", "dimension"}
    assert {n["id"] for n in snapshot["graph"]["nodes"]} >= {"prepare", "model", "tools", "review", "checkpoint"}
    assert "graph TD" in inspect_agent("mermaid")
