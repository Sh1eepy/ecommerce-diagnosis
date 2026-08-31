"""真实 SDK + MockTransport + 假时钟：验证请求次数与故障处理，无外部网络。"""
import json
import ssl
from email.utils import formatdate
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings, settings
from app.llm import deepseek
from app.llm.errors import LLMCallError, classify_http_error, classify_llm_exception


@pytest.mark.parametrize(("status_code", "expected"), [
    (408, "retryable"),
    (429, "retryable"),
    (500, "retryable"),
    (502, "retryable"),
    (503, "retryable"),
    (504, "retryable"),
    (400, "permanent"),
    (401, "permanent"),
    (402, "permanent"),
    (403, "permanent"),
    (404, "permanent"),
    (405, "permanent"),
    (499, "permanent"),
    (None, "unknown"),
    (200, "unknown"),
    (399, "unknown"),
    (501, "unknown"),
    (505, "unknown"),
    (599, "unknown"),
])
def test_classify_http_error(status_code, expected):
    assert classify_http_error(status_code) == expected


@pytest.fixture
def provider(monkeypatch):
    """只替换 HTTP 传输，保留真实 SDK 重试代码，能发现嵌套重试回归。"""
    clock = SimpleNamespace(now=0.0, sleeps=[])
    outcomes, requests, logs = [], [], []

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    def handler(request):
        requests.append(request)
        if not outcomes:
            raise AssertionError("unexpected extra HTTP request")
        outcome = outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(request)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        status, headers, body = outcome
        if body is None:
            body = ({
                "id": "test-completion", "object": "chat.completion", "created": 0,
                "model": "test-model", "choices": [{
                    "index": 0, "message": {"role": "assistant", "content": '{"type":"final"}'},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            } if status == 200 else {"error": {"message": "sensitive-provider-detail", "code": "server_error"}})
        return httpx.Response(status, headers=headers, json=body)

    real_openai = deepseek.OpenAI
    transport_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(deepseek, "OpenAI", lambda **kwargs: real_openai(**kwargs, http_client=transport_client))
    monkeypatch.setattr(deepseek, "monotonic", lambda: clock.now)
    monkeypatch.setattr(deepseek, "sleep", sleep)
    monkeypatch.setattr(deepseek, "uniform", lambda low, high: 1.0)
    monkeypatch.setattr(deepseek, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(deepseek, "log_agent_step", lambda *args: logs.append(args))
    monkeypatch.setattr(settings, "LLM_API_KEY", "unit-test-key")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://llm.invalid")
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 2)
    client = deepseek.DeepSeekClient()
    yield SimpleNamespace(client=client, clock=clock, outcomes=outcomes, requests=requests, logs=logs)
    client._client.close()


def test_provider_success_preserves_response_contract(provider):
    provider.outcomes.append((200, {}, None))
    result = provider.client.chat([{"role": "user", "content": "test"}])
    assert (result.content, result.tokens_in, result.tokens_out, result.attempts) == ('{"type":"final"}', 10, 3, 1)
    assert len(provider.requests) == 1
    assert provider.clock.sleeps == []
    payload = json.loads(provider.requests[0].content)
    assert payload["response_format"] == {"type": "json_object"}


def test_langchain_adapter_preserves_wire_messages_usage_and_retry_limit(provider):
    from app.llm.langchain_adapter import invoke_chat

    messages = [{"role": "system", "content": "原始规则"}, {"role": "user", "content": "任务"},
                {"role": "assistant", "content": '{"type":"tool_call"}'},
                {"role": "user", "content": "结构化证据"}]
    provider.outcomes.extend([(503, {}, None), (200, {}, None)])
    result = invoke_chat(provider.client, messages, timeout=5, max_tokens=600)
    assert (result.content, result.tokens_in, result.tokens_out, result.attempts) == ('{"type":"final"}', 10, 3, 2)
    assert len(provider.requests) == 2
    for request in provider.requests:
        payload = json.loads(request.content)
        assert payload["messages"] == messages
        assert payload["max_tokens"] == 600
        assert payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload and "stream" not in payload


def test_langchain_adapter_preserves_permanent_failure_without_extra_attempt(provider):
    from app.llm.langchain_adapter import invoke_chat

    provider.outcomes.append((401, {}, None))
    with pytest.raises(LLMCallError) as exc:
        invoke_chat(provider.client, [{"role": "user", "content": "task"}])
    assert exc.value.kind == "permanent" and exc.value.attempts == 1
    assert len(provider.requests) == 1 and provider.clock.sleeps == []


def test_langchain_adapter_does_not_use_global_cache(provider):
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import get_llm_cache, set_llm_cache
    from app.llm.langchain_adapter import invoke_chat

    old = get_llm_cache()
    try:
        set_llm_cache(InMemoryCache())
        provider.outcomes.extend([(200, {}, None), (200, {}, None)])
        for _ in range(2):
            invoke_chat(provider.client, [{"role": "user", "content": "same input"}], json_mode=False)
        assert len(provider.requests) == 2
        assert all("response_format" not in json.loads(r.content) for r in provider.requests)
    finally:
        set_llm_cache(old)


def test_provider_output_limit_is_sent_on_every_retry(provider):
    provider.outcomes.extend([(503, {}, None), (200, {}, None)])
    provider.client.chat([{"role": "user", "content": "JSON"}], max_tokens=600)
    assert [json.loads(r.content)["max_tokens"] for r in provider.requests] == [600, 600]


def test_feedback_provider_can_disable_retries_without_changing_global_config(provider):
    client = deepseek.DeepSeekClient(max_retries=0)
    assert client.max_retries == 0 and provider.client.max_retries == 2
    provider.outcomes.append((503, {}, None))
    with pytest.raises(LLMCallError) as exc:
        client.chat([], max_tokens=512)
    assert exc.value.attempts == 1 and len(provider.requests) == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_provider_rejects_invalid_output_limit_without_request(provider, limit):
    with pytest.raises(ValueError):
        provider.client.chat([], max_tokens=limit)
    assert provider.requests == []


def test_plain_text_mode_and_missing_usage_remain_supported(provider):
    provider.outcomes.append((200, {}, {"choices": [{"message": {"content": "hello"}}]}))
    result = provider.client.chat([], json_mode=False)
    assert (result.content, result.tokens_in, result.tokens_out) == ("hello", 0, 0)
    assert "response_format" not in json.loads(provider.requests[0].content)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_failure_retries_once_then_succeeds(provider, status):
    provider.outcomes.extend([(status, {}, None), (200, {}, None)])
    result = provider.client.chat([])
    assert result.attempts == 2
    assert len(provider.requests) == 2
    assert provider.clock.sleeps == [1.0]
    assert provider.requests[0].extensions["timeout"]["read"] == 10.0
    assert provider.requests[1].extensions["timeout"]["read"] == 9.0


@pytest.mark.parametrize("status,headers,body,kind", [
    (400, {}, None, "permanent"), (401, {}, None, "permanent"),
    (402, {}, None, "permanent"), (403, {}, None, "permanent"),
    (404, {}, None, "permanent"), (409, {}, None, "permanent"),
    (422, {}, None, "permanent"), (501, {}, None, "unknown"),
    (505, {}, None, "unknown"),
    (401, {"x-should-retry": "true"}, None, "permanent"),
    (501, {"x-should-retry": "true"}, None, "unknown"),
    (503, {"x-should-retry": "false"}, None, "permanent"),
    (429, {}, {"error": {"code": "insufficient_quota"}}, "permanent"),
    (429, {}, {"error": {"type": "insufficient_quota"}}, "permanent"),
    (429, {}, {"error": {"code": "billing_hard_limit_reached"}}, "permanent"),
    (429, {}, {"error": {"code": "insufficient_balance"}}, "permanent"),
    (429, {}, {"error": {"code": "credit_balance_exhausted"}}, "permanent"),
    (429, {}, {"error": {"code": "organization_spend_limit_exceeded"}}, "permanent"),
    (429, {}, {"error": {"code": "project_spend_limit_exceeded"}}, "permanent"),
    (429, {}, {"error": {"code": "organization_usage_limit_exceeded"}}, "permanent"),
])
def test_nonretryable_failure_stops_without_sleep(provider, status, headers, body, kind):
    provider.outcomes.append((status, headers, body))
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    error = caught.value
    assert (error.kind, error.attempts, error.status_code) == (kind, 1, status)
    assert error.retryable is False
    assert error.stop_reason == "not_retryable"
    assert len(provider.requests) == 1
    assert provider.clock.sleeps == []
    assert "sensitive-provider-detail" not in str(error)
    assert "sensitive-provider-detail" not in str(provider.logs)


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_actual_transport_attempts_are_bounded_without_sdk_retries(provider, retries):
    provider.client.max_retries = retries
    provider.outcomes.extend([(503, {}, None)] * (retries + 1))
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([], timeout=60)
    assert caught.value.stop_reason == "attempts_exhausted"
    assert caught.value.attempts == retries + 1
    assert len(provider.requests) == retries + 1
    assert len(provider.clock.sleeps) == retries


@pytest.mark.parametrize("headers,delay", [
    ({"retry-after": "2.5"}, 3.5),
    ({"retry-after-ms": "2500"}, 3.5),
    ({"retry-after-ms": "2500", "retry-after": "4"}, 3.5),
    ({"retry-after-ms": "bad", "retry-after": "3"}, 4.0),
    ({"retry-after": formatdate(1_700_000_003, usegmt=True)}, 4.0),
    ({"retry-after": "0"}, 1.0),
])
def test_retry_after_is_respected(provider, headers, delay):
    provider.outcomes.extend([(429, headers, None), (200, {}, None)])
    assert provider.client.chat([]).attempts == 2
    assert provider.clock.sleeps == [delay]
    assert provider.requests[1].extensions["timeout"]["read"] == pytest.approx(10 - delay)


@pytest.mark.parametrize("value", ["bad", "-1", "NaN", "Infinity", "1e999", formatdate(1_699_999_990, usegmt=True)])
def test_invalid_retry_after_falls_back_to_backoff(provider, value):
    provider.outcomes.extend([(503, {"retry-after": value}, None), (200, {}, None)])
    provider.client.chat([])
    assert provider.clock.sleeps == [1.0]


@pytest.mark.parametrize("delay,reason", [(10, "deadline_exhausted"), (15, "deadline_exhausted"), (61, "retry_after_too_long")])
def test_server_delay_is_not_shortened_to_force_a_retry(provider, delay, reason):
    provider.outcomes.append((429, {"retry-after": str(delay)}, None))
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.stop_reason == reason
    assert caught.value.retry_after_seconds == delay
    assert len(provider.requests) == 1
    assert provider.clock.sleeps == []


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_errors_without_status_can_retry(provider, error_type):
    provider.outcomes.extend([error_type("network fault"), (200, {}, None)])
    assert provider.client.chat([]).attempts == 2
    assert len(provider.requests) == 2


def test_tls_verification_failure_does_not_retry(provider):
    failure = httpx.ConnectError("TLS failure")
    failure.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    provider.outcomes.append(failure)
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.kind == "permanent"
    assert len(provider.requests) == 1


def test_wrapped_programming_error_is_not_treated_as_network_retry(provider):
    provider.outcomes.append(ValueError("bad local configuration"))
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.kind == "unknown"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("failure,kind", [
    (httpx.InvalidURL("invalid URL"), "permanent"),
    (httpx.UnsupportedProtocol("unsupported scheme"), "permanent"),
    (httpx.LocalProtocolError("invalid local headers"), "permanent"),
    (httpx.TooManyRedirects("redirect loop"), "unknown"),
    (FileNotFoundError("local file missing"), "unknown"),
])
def test_wrapped_local_errors_do_not_retry(provider, failure, kind):
    provider.outcomes.append(failure)
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.kind == kind
    assert len(provider.requests) == 1
    assert provider.clock.sleeps == []


def test_sdk3_default_transport_exception_classification():
    httpx2 = pytest.importorskip("httpx2")
    from openai import APIConnectionError

    for cause, kind in [
        (httpx2.ReadTimeout("timeout"), "retryable"),
        (httpx2.ConnectError("network"), "retryable"),
        (httpx2.LocalProtocolError("headers"), "permanent"),
    ]:
        error = APIConnectionError(request=httpx2.Request("POST", "https://llm.invalid"))
        error.__cause__ = cause
        assert classify_llm_exception(error) == kind


def test_network_time_and_backoff_share_budget(provider):
    def slow_failure(request):
        provider.clock.now += 4
        return (503, {}, None)

    provider.outcomes.extend([slow_failure, (200, {}, None)])
    assert provider.client.chat([]).attempts == 2
    assert provider.requests[1].extensions["timeout"]["read"] == 5


def test_exponential_backoff_is_capped_and_jittered(provider, monkeypatch):
    provider.client.max_retries = 5
    provider.outcomes.extend([(503, {}, None)] * 5 + [(200, {}, None)])
    monkeypatch.setattr(deepseek, "uniform", lambda low, high: low)
    assert provider.client.chat([], timeout=60).attempts == 6
    assert provider.clock.sleeps == [0.75, 1.5, 3.0, 6.0, 6.0]


def test_deadline_exhausted_during_request_does_not_retry(provider):
    def slow_failure(request):
        provider.clock.now += 10
        raise httpx.ReadTimeout("slow response", request=request)

    provider.outcomes.append(slow_failure)
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.stop_reason == "deadline_exhausted"
    assert caught.value.retryable is False
    assert len(provider.requests) == 1
    assert provider.clock.sleeps == []


def test_oversleep_does_not_start_request_after_deadline(provider, monkeypatch):
    provider.outcomes.append((503, {}, None))
    monkeypatch.setattr(deepseek, "sleep", lambda seconds: setattr(provider.clock, "now", 11.0))
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.stop_reason == "deadline_exhausted"
    assert caught.value.attempts == 1
    assert len(provider.requests) == 1


def test_response_received_after_deadline_is_not_accepted_or_retried(provider):
    def late_success(request):
        provider.clock.now += 11
        return (200, {}, None)

    provider.outcomes.append(late_success)
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.stop_reason == "deadline_exhausted"
    assert len(provider.requests) == 1


def test_invalid_response_envelope_is_not_retried(provider):
    provider.outcomes.append((200, {}, {"choices": []}))
    with pytest.raises(LLMCallError) as caught:
        provider.client.chat([])
    assert caught.value.stop_reason == "invalid_response"
    assert len(provider.requests) == 1


def test_log_write_failure_does_not_repeat_successful_request(provider, monkeypatch):
    def failed_log(*args):
        raise OSError("disk full")

    monkeypatch.setattr(deepseek, "log_agent_step", failed_log)
    provider.outcomes.append((200, {}, None))
    assert provider.client.chat([]).attempts == 1
    assert len(provider.requests) == 1


def test_cancellation_is_not_swallowed(provider):
    provider.outcomes.append(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        provider.client.chat([])
    assert len(provider.requests) == 1


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_never_sends_request(provider, budget):
    with pytest.raises(ValueError):
        provider.client.chat([], timeout=budget)
    assert provider.requests == []


@pytest.mark.parametrize("values", [
    {"LLM_MAX_RETRIES": -1}, {"LLM_MAX_RETRIES": 6},
    {"LLM_TIMEOUT_SECONDS": 0}, {"LLM_TIMEOUT_SECONDS": float("nan")},
    {"LLM_TIMEOUT_SECONDS": float("inf")},
])
def test_invalid_retry_settings_rejected(values):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_provider_failure_is_reported_by_agent_without_executing_tools(provider):
    from datetime import date

    from app.agent.agent import Agent

    provider.outcomes.append((401, {}, None))
    result = Agent(llm=provider.client).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "error"
    assert result["stop_reason"] == "llm_error"
    assert result["tool_calls"] == 0
    assert len(provider.requests) == 1
