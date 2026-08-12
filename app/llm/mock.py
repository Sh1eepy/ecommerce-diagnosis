"""MockLLM：离线开发/测试用，按脚本化决策序列回复。

示例：
    plan = [
        {"type": "tool_call", "tool": "metric", "args": {...}},
        {"type": "tool_call", "tool": "funnel", "args": {...}},
        {"type": "final", "report": {"conclusion": "..."}},
    ]
"""
from __future__ import annotations

import json

from app.llm.base import LLMClient, LLMResponse


class MockLLM(LLMClient):
    def __init__(self, plan: list[dict] | None = None, fallback: dict | None = None):
        self.plan = list(plan or [])
        self.idx = 0
        self.model = "mock"
        self.fallback = fallback or {
            "type": "final",
            "report": {
                "facts": [{"point": "Mock 兜底", "evidence": "无"}],
                "analysis": {"key_finding": "无", "impact": "无"},
                "conclusion": "Mock 兜底结论",
                "suggestions": [],
            },
        }

    def chat(self, messages, *, json_mode=True, timeout=None):
        if self.idx < len(self.plan):
            step = self.plan[self.idx]
            self.idx += 1
        else:
            step = self.fallback
        return LLMResponse(
            content=json.dumps(step, ensure_ascii=False),
            tokens_in=10,
            tokens_out=10,
            model=self.model,
        )
