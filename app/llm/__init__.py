"""LLM Provider 工厂：有 API Key → DeepSeek，否则 → Mock（离线可跑）。"""
from __future__ import annotations

from app.config import settings
from app.llm.base import LLMClient, LLMResponse


def get_llm() -> LLMClient:
    if settings.LLM_API_KEY:
        from app.llm.deepseek import DeepSeekClient

        return DeepSeekClient()
    from app.llm.mock import MockLLM

    return MockLLM()


__all__ = ["LLMClient", "LLMResponse", "get_llm"]
