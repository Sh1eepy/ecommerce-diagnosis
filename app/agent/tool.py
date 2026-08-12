"""Tool 基类 + 白名单注册表。

安全设计：
- 只能执行注册表（白名单）内的 Tool
- execute() 统一做：参数执行 → 异常兜底 → 追踪日志（tool_calls/{run_id}.jsonl）
- 所有 Tool 均为只读查询，数据库侧用 agent_ro 账号兜底
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from app.tracing import log_tool_call


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {}

    @abstractmethod
    def run(self, **kwargs) -> dict:
        """返回 {"ok": bool, "text": str, "rows": int, "data": Any}"""

    def execute(self, args: dict, *, run_id: str, step: int) -> dict:
        t0 = time.perf_counter()
        try:
            result = self.run(**args)
            result.setdefault("ok", True)
            status, err = "ok", ""
        except Exception as e:  # noqa: BLE001
            result = {
                "ok": False,
                "text": f"工具执行失败: {type(e).__name__}: {e}",
                "rows": 0,
                "data": None,
            }
            status, err = "error", str(e)
        latency = (time.perf_counter() - t0) * 1000.0
        log_tool_call(
            run_id, step, self.name, args,
            result.get("text", ""), result.get("rows", 0),
            round(latency, 2), status,
        )
        result["_meta"] = {"latency_ms": round(latency, 2), "error": err}
        return result


class ToolRegistry:
    """工具白名单注册表：只能执行已注册的 Tool。"""

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def describe(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict, *, run_id: str, step: int) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "text": f"未知工具: {name}（白名单: {', '.join(self.names())}）",
                "rows": 0,
                "data": None,
            }
        return tool.execute(args, run_id=run_id, step=step)
