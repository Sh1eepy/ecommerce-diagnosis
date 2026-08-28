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

from jsonschema import Draft202012Validator, FormatChecker

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
            status, err = ("ok" if result["ok"] else "error"), ""
            error_code = None if result["ok"] else "execution_error"
        except Exception as e:  # noqa: BLE001
            result = {
                "ok": False,
                "text": f"工具执行失败: {type(e).__name__}: {e}",
                "rows": 0,
                "data": None,
            }
            status, err = "error", str(e)
            error_code = "invalid_arguments" if isinstance(e, ValueError) else "execution_error"
        latency = (time.perf_counter() - t0) * 1000.0
        log_tool_call(
            run_id, step, self.name, args,
            result.get("text", ""), result.get("rows", 0),
            round(latency, 2), status,
        )
        result["_meta"] = {"latency_ms": round(latency, 2), "error": err, "error_code": error_code}
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
        error = self._validate_args(tool, args)
        if error:
            return {"ok": False, "text": f"工具参数不合法: {error}", "rows": 0, "data": None,
                    "_meta": {"error_code": "invalid_arguments"}}
        return tool.execute(args, run_id=run_id, step=step)

    @staticmethod
    def _validate_args(tool: Tool, args: dict) -> str:
        """共用 Schema 的完整校验；错误不回显输入值，业务关系由 Tool 校验。"""
        if not isinstance(args, dict):
            return "args 必须是对象"
        schema = tool.parameters or {}
        props = schema.get("properties") or {}
        missing = [k for k in schema.get("required") or [] if k not in args]
        if missing:
            return "缺少必填参数: " + ", ".join(missing)
        unknown = sorted(set(args) - set(props))
        if unknown:
            return "未知参数: " + ", ".join(unknown)
        types = {"integer": int, "string": str, "array": list, "object": dict, "boolean": bool}
        for key, value in args.items():
            expected = types.get((props.get(key) or {}).get("type"))
            if expected and (not isinstance(value, expected) or expected is int and isinstance(value, bool)):
                return f"参数 {key} 类型错误"
        error = next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(args), None)
        if error is not None:
            # 不使用 error.message，它可能包含用户输入或整个 payload。
            path = ".".join(str(part) for part in error.absolute_path) or "args"
            return f"参数 {path} 不符合 {error.validator} 约束"
        return ""
