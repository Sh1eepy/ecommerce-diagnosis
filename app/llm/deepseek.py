"""DeepSeek 官方 API 客户端（OpenAI 兼容协议）。

要点：
- base_url / api_key / model 全部来自配置（.env）
- 单次请求超时 + 429/5xx 指数退避重试（防"突然变慢"被限流打死）
- 支持 JSON 输出模式（response_format）
"""
from __future__ import annotations

import time

from openai import OpenAI

from app.config import settings
from app.llm.base import LLMClient, LLMResponse


class DeepSeekClient(LLMClient):
    def __init__(self):
        if not settings.LLM_API_KEY:
            raise ValueError("LLM_API_KEY 未配置；请填入 .env 或改用 MockLLM")
        self._client = OpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )
        self.model = settings.LLM_MODEL
        self.timeout = settings.LLM_TIMEOUT_SECONDS
        self.max_retries = settings.LLM_MAX_RETRIES

    def chat(self, messages, *, json_mode=True, timeout=None):
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(
                    **kwargs, timeout=timeout or self.timeout
                )
                content = resp.choices[0].message.content or ""
                usage = resp.usage
                return LLMResponse(
                    content=content,
                    tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                    tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                    model=self.model,
                )
            except Exception as e:  # noqa: BLE001  限流/超时/5xx → 退避重试
                last_err = e
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
        raise last_err
