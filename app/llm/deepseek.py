"""DeepSeek 官方 API 客户端（OpenAI 兼容协议）。

要点：
- base_url / api_key / model 全部来自配置（.env）
- 禁用 SDK 自动重试，由 Provider 统一分类、计数、退避和检查剩余预算
- 支持 JSON 输出模式（response_format）
"""
from __future__ import annotations

import json
import logging
import math
from random import uniform
from time import monotonic, sleep, time

from openai import APIStatusError, OpenAI

from app.config import settings
from app.llm.base import LLMClient, LLMResponse
from app.llm.errors import LLMCallError, classify_llm_exception, retry_after_seconds
from app.tracing import current_run_id, log_agent_step

logger = logging.getLogger(__name__)
MAX_RETRY_AFTER_SECONDS = 60.0


def _retry_after(error: Exception) -> float | None:
    return retry_after_seconds(error, now=time())


def _record_attempt(attempt: int, started: float, outcome: str,
                    *, kind: str | None = None, status_code: int | None = None) -> None:
    try:
        log_agent_step(current_run_id() or "cli", 0, "llm_attempt", json.dumps({
            "attempt": attempt, "outcome": outcome, "kind": kind, "status_code": status_code,
        }), round((monotonic() - started) * 1000, 2))
    except OSError:
        # 日志磁盘异常不得触发第二次模型请求。
        logger.warning("LLM attempt log could not be written")


class DeepSeekClient(LLMClient):
    def __init__(self, *, max_retries: int | None = None):
        if not settings.LLM_API_KEY:
            raise ValueError("LLM_API_KEY 未配置；请填入 .env 或改用 MockLLM")
        self._client = OpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            max_retries=0,
        )
        self.model = settings.LLM_MODEL
        self.timeout = settings.LLM_TIMEOUT_SECONDS
        self.max_retries = settings.LLM_MAX_RETRIES if max_retries is None else max_retries
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries 必须为 0~5 的整数")

    def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
        """重试共享调度预算；SDK 网络阶段超时不能提供在途请求的硬墙钟取消。"""
        budget = self.timeout if timeout is None else float(timeout)
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("timeout 必须是有限正数")
        deadline = monotonic() + budget
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
                raise ValueError("max_tokens 必须为正整数")
            kwargs["max_tokens"] = max_tokens

        last_err: Exception | None = None
        for index in range(self.max_retries + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise LLMCallError(kind="unknown", attempts=index,
                                   stop_reason="deadline_exhausted") from last_err
            attempt = index + 1
            started = monotonic()
            try:
                resp = self._client.chat.completions.create(
                    **kwargs, timeout=min(self.timeout, remaining)
                )
            except Exception as e:  # noqa: BLE001  未知异常只封装，不盲目重试
                last_err = e
                kind = classify_llm_exception(e)
                status = e.status_code if isinstance(e, APIStatusError) else None
                _record_attempt(attempt, started, "error", kind=kind, status_code=status)
                retry_after = _retry_after(e)
                remaining = deadline - monotonic()
                stop = None
                if kind != "retryable":
                    stop = "not_retryable"
                elif remaining <= 0:
                    stop = "deadline_exhausted"
                elif index == self.max_retries:
                    stop = "attempts_exhausted"
                elif retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
                    stop = "retry_after_too_long"
                # 在服务端要求的最短等待后叠加随机退避，避免同批客户端同时醒来。
                delay = (retry_after or 0.0) + min(2 ** index, 8.0) * uniform(0.75, 1.0)
                if stop is None and delay >= remaining:
                    stop = "deadline_exhausted"
                if stop:
                    raise LLMCallError(
                        kind=kind, attempts=attempt, stop_reason=stop,
                        status_code=status, retry_after_seconds=retry_after,
                    ) from e
                sleep(delay)
                continue

            if monotonic() >= deadline:
                _record_attempt(attempt, started, "late_response")
                raise LLMCallError(kind="unknown", attempts=attempt, stop_reason="deadline_exhausted")
            # 协议/格式异常不得作为网络故障重新计费请求；JSON 正文校验仍由 Agent 负责。
            choices = getattr(resp, "choices", None)
            message = getattr(choices[0], "message", None) if isinstance(choices, list) and choices else None
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.strip():
                _record_attempt(attempt, started, "invalid_response")
                raise LLMCallError(kind="unknown", attempts=attempt, stop_reason="invalid_response")
            usage = getattr(resp, "usage", None)
            _record_attempt(attempt, started, "ok")
            return LLMResponse(
                content=content,
                tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                model=self.model, attempts=attempt,
            )
        raise AssertionError("unreachable retry loop")

    def close(self):
        self._client.close()
