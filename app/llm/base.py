"""LLM 客户端抽象（Provider 层）。

DeepSeek 走 OpenAI 兼容协议，故只需一个真实实现 + 一个离线 Mock；
以后换 Qwen/OpenAI 只需改 base_url/api_key/model，不动 Agent 代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


class LLMClient(ABC):
    """统一聊天接口。messages: [{"role": ..., "content": str}]"""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = True,
        timeout: float | None = None,
    ) -> LLMResponse:
        """返回模型文本输出（json_mode=True 时要求模型输出 JSON）。"""
