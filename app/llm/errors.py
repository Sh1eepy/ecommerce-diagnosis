"""错误分类和安全的失败元数据；不在此处执行重试。"""
import ssl
from typing import Literal

import httpx
from openai import APIConnectionError, APIStatusError

# SDK 1/2 使用 httpx；SDK 3 默认使用 httpx2，同时兼容旧传输。
try:
    import httpx2
except ImportError:
    _transports = (httpx,)
else:
    _transports = (httpx, httpx2)

_CONFIG_ERRORS = tuple(cls for transport in _transports for cls in (
    transport.InvalidURL, transport.UnsupportedProtocol, transport.LocalProtocolError,
))
_TRANSIENT_ERRORS = tuple(cls for transport in _transports for cls in (
    transport.TimeoutException, transport.NetworkError, transport.RemoteProtocolError,
))

ErrorKind = Literal["retryable", "permanent", "unknown"]


def classify_http_error(
    status_code: int | None,
) -> ErrorKind:
    """可重试不代表必须重试；预算、异常类型及服务方错误码由调用层决定。

    无状态码的网络异常不能仅凭 None 判断是否临时故障。
    未识别的状态保持 unknown，调用方默认不自动重试。
    """
    if status_code in {408, 429, 500, 502, 503, 504}:
        return "retryable"
    if status_code is not None and 400 <= status_code <= 499:
        return "permanent"
    return "unknown"


def classify_llm_exception(error: Exception) -> ErrorKind:
    """状态码只是一层信号；额度、服务端禁止重试和 TLS 错误优先。"""
    if isinstance(error, APIStatusError):
        body = error.body if isinstance(error.body, dict) else {}
        nested = body.get("error")
        if isinstance(nested, dict):
            body = nested
        codes = (getattr(error, "code", None), body.get("code"), body.get("type"))
        if any(isinstance(code, str) and code in {
            "insufficient_quota", "billing_hard_limit_reached", "insufficient_balance",
            "credit_balance_exhausted", "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
        } for code in codes):
            return "permanent"
        if error.response.headers.get("x-should-retry", "").lower() == "false":
            return "permanent"
        # x-should-retry:true 不得把认证失败或未知状态升级为可重试。
        return classify_http_error(error.status_code)
    if isinstance(error, APIConnectionError):
        cause: BaseException | None = error
        seen: set[int] = set()
        transient = error.__cause__ is None and error.__context__ is None
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, (ssl.SSLError, *_CONFIG_ERRORS)):
                return "permanent"  # 不重试证书/本地协议配置故障，更不关闭 TLS 校验。
            if isinstance(cause, (ValueError, TypeError, RuntimeError, AssertionError, AttributeError, KeyError)):
                return "unknown"  # SDK 可能把本地配置/程序错误包装为连接异常。
            if isinstance(cause, _TRANSIENT_ERRORS):
                transient = True
            cause = cause.__cause__ or cause.__context__
        return "retryable" if transient else "unknown"
    return "unknown"


class LLMCallError(RuntimeError):
    """供后续 Agent/Worker 使用；str 不包含原始响应体、请求或凭据。"""

    def __init__(self, *, kind: ErrorKind, attempts: int, stop_reason: str,
                 status_code: int | None = None, retry_after_seconds: float | None = None):
        self.kind = kind
        self.attempts = attempts
        self.stop_reason = stop_reason
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = kind == "retryable" and stop_reason != "deadline_exhausted"
        super().__init__(
            f"LLM 调用失败: kind={kind}, stop_reason={stop_reason}, "
            f"attempts={attempts}, status_code={status_code}"
        )
