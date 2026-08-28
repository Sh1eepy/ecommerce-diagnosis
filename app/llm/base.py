"""LLM 客户端抽象（Provider 层）。

DeepSeek 走 OpenAI 兼容协议，故只需一个真实实现 + 一个离线 Mock；
以后换 Qwen/OpenAI 只需改 base_url/api_key/model，不动 Agent 代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math


@dataclass
class LLMResponse:
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    attempts: int = 1  # Provider 底层调用次数；兼容原有 Mock 和调用方。


class LLMClient(ABC):
    """统一聊天接口。messages: [{"role": ..., "content": str}]"""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = True,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """返回模型文本输出（json_mode=True 时要求模型输出 JSON）。"""
    minimum_output_tokens = 512

    def close(self) -> None:
        """释放 Provider 资源；无外部连接的客户端无需操作。"""

    def output_token_reserve(self, limit: int) -> int:
        return limit

    def estimate_input_tokens(self, messages: list[dict]) -> int:
        """带余量的本地估计，不是供应商 tokenizer，也不是账单硬上限。"""
        content = "".join(str(m.get("content", "")) for m in messages)
        ascii_count = sum(ord(c) < 128 for c in content)
        return math.ceil(ascii_count / 3 + (len(content) - ascii_count) * 1.5) + 128 + 8 * len(messages)
