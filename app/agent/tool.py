"""LangChain 原生工具的业务执行边界。

安全设计：
- 只能执行注册表（白名单）内的 Tool
- BaseTool 负责标准调用；注册表只负责白名单、严格校验、错误信封和本地审计
- 内置四工具均为只读查询，数据库侧用 agent_ro 账号兜底；新增工具需单独审查
"""
from __future__ import annotations

import json
import time
from copy import deepcopy

from jsonschema import Draft202012Validator, FormatChecker
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langsmith import tracing_context

from app.tracing import log_tool_call


class ToolRegistry:
    """接收 @tool / StructuredTool / BaseTool；不再要求继承项目专用基类。

    注册表示调用方允许此工具在当前业务中执行，不自动证明第三方工具只读。
    MCP 仍使用自己的显式导出名单，不自动导出新增工具。
    """

    def __init__(self, tools: list[BaseTool] | None = None):
        self._tools: dict[str, BaseTool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: BaseTool) -> None:
        if not isinstance(tool, BaseTool):
            raise TypeError("工具必须实现 LangChain BaseTool 接口")
        if not tool.name or tool.name in self._tools:
            raise ValueError("工具名为空或重复；禁止静默覆盖白名单")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def input_schema(self, name: str) -> dict:
        tool = self._tools[name]
        schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.tool_call_schema
        return deepcopy(schema if isinstance(schema, dict) else schema.model_json_schema())

    def describe(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "parameters": self.input_schema(t.name)}
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
        error = self.validate_args(name, args)
        if error:
            return {"ok": False, "text": f"工具参数不合法: {error}", "rows": 0, "data": None,
                    "_meta": {"error_code": "invalid_arguments"}}
        t0 = time.perf_counter()
        try:
            # 不继承第三方工具实例上的 callbacks；只输出本地审计。
            local_tool = tool.model_copy(update={
                "callbacks": [], "verbose": False,
                "handle_tool_error": False, "handle_validation_error": False,
            })
            payload = args
            if tool.response_format == "content_and_artifact":
                payload = {"type": "tool_call", "name": name, "id": f"{run_id}:{step}", "args": args}
            with tracing_context(enabled=False):
                raw = local_tool.invoke(payload, config={"callbacks": [], "configurable": {
                    "diagnosis_run_id": run_id, "diagnosis_step": step,
                }})
            if isinstance(raw, ToolMessage):
                result = {"ok": raw.status != "error", "text": raw.text, "rows": 0,
                          "data": raw.artifact if raw.artifact is not None else raw.content}
                if not result["ok"]:
                    result["data"] = None
                raw = result
            if isinstance(raw, dict) and {"ok", "text", "rows", "data"} <= raw.keys():
                result = dict(raw)
                if (type(result["ok"]) is not bool or not isinstance(result["text"], str)
                        or type(result["rows"]) is not int or result["rows"] < 0):
                    raise ValueError("工具信封的 ok/text/rows 类型不合法")
            elif isinstance(raw, dict) and "ok" in raw:
                raise ValueError("工具返回了不完整的业务信封")
            else:
                # 通用 LangChain 工具可返回普通字符串/JSON，无需学习项目的信封格式。
                result = {"ok": True, "text": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str),
                          "rows": len(raw) if isinstance(raw, list) else 1, "data": raw}
            status, err = ("ok" if result["ok"] else "error"), ""
            error_code = None if result["ok"] else "execution_error"
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "text": f"工具执行失败: {type(e).__name__}: {e}", "rows": 0, "data": None}
            status, err = "error", str(e)
            error_code = "invalid_arguments" if isinstance(e, ValueError) else "execution_error"
        latency = round((time.perf_counter() - t0) * 1000.0, 2)
        log_tool_call(run_id, step, name, args, result["text"], result["rows"], latency, status)
        result["_meta"] = {"latency_ms": latency, "error": err, "error_code": error_code}
        return result

    def validate_args(self, name: str, args: dict) -> str:
        """共用 Schema 的完整校验；错误不回显输入值，业务关系由 Tool 校验。"""
        if not isinstance(args, dict):
            return "args 必须是对象"
        schema = self.input_schema(name)
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
