"""本机 stdio MCP 适配：复用白名单工具，不运行 Agent/LLM，不创建数据库表。

SDK 负责协议；本模块负责授权、容量、结果边界。超时不杀线程，因此直到
底层查询真正结束才释放槽位，避免客户端超时重试产生无界后台查询。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from app.agent import default_registry
from app.agent.quality import evidence_limits
from app.agent.tool import ToolRegistry
from app.config import Settings, settings
from app.tracing import log_agent_step, set_run_id

# 显式导出，未来给 Agent 增加写工具不会自动暴露给 MCP。
EXPORTED_TOOLS = ("metric", "funnel", "dimension", "peer")
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "tool", "run_id", "call_id", "text", "rows", "data", "evidence_limits", "error"],
    "properties": {
        "ok": {"type": "boolean"}, "tool": {"type": "string"},
        "run_id": {"type": "string"}, "call_id": {"type": "string"},
        "text": {"type": "string"}, "rows": {"type": "integer", "minimum": 0},
        "data": {"type": ["object", "null"]},
        "evidence_limits": {"type": "object", "additionalProperties": {"type": "string"}},
        "error": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "required": ["code", "message", "retryable"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"},
                           "retryable": {"type": "boolean"}},
        }]},
    },
}
ERROR_MESSAGES = {
    "invalid_arguments": "参数不符合工具契约，请检查类型、指标、维度及日期窗口。",
    "execution_error": "工具查询失败；请由管理员按 run_id 检查受限日志。",
    "timeout": "等待工具结果超时；查询可能仍在执行，不要立即重试。",
    "busy": "工具执行槽位已满，请稍后再试。",
    "rate_limited": "工具调用过于频繁，请稍后再试。",
    "result_too_large": "结果超过返回上限；请缩小日期窗口或减少指标。",
    "audit_unavailable": "审计记录不可用，本次不交付业务结果。",
}
_OUTPUT_VALIDATOR = Draft202012Validator(OUTPUT_SCHEMA)


def require_mcp_access(config: Settings) -> None:
    """启动者授权；不是远程 OAuth，也不接受模型传入 Key 或权限。"""
    if not config.MCP_ENABLED:
        raise PermissionError("MCP 未启用：需要明确设置 MCP_ENABLED=true。")
    key = config.MCP_ACCESS_KEY
    if len(key) < 32 or key != key.strip():
        raise PermissionError("MCP_ACCESS_KEY 必须是至少 32 字符的独立凭证。")
    for configured, scopes in config.API_KEY_SCOPES.items():
        if secrets.compare_digest(key.encode(), configured.encode()) and "tools:read" in scopes:
            return
    raise PermissionError("MCP 凭证未被显式授予 tools:read；旧式 API_KEYS 不适用。")


def _envelope(name: str, run_id: str, *, result: dict | None = None, code: str | None = None) -> dict:
    if code:
        message = ERROR_MESSAGES[code]
        return {"ok": False, "tool": name, "run_id": run_id, "call_id": f"{name}#{run_id}",
                "text": message, "rows": 0, "data": None, "evidence_limits": {},
                "error": {"code": code, "message": message,
                          "retryable": code in {"busy", "rate_limited"}}}
    assert result is not None
    return {"ok": True, "tool": name, "run_id": run_id, "call_id": f"{name}#{run_id}",
            "text": result["text"], "rows": result["rows"], "data": result["data"],
            "evidence_limits": evidence_limits({"tool": {"tool": name, "data": result["data"]}}),
            "error": None}


def _wire_result(payload: dict) -> types.CallToolResult:
    # 文本与 structuredContent 表达同一个信封，旧客户端也不会丢失证据限制。
    if not _OUTPUT_VALIDATOR.is_valid(payload):
        raise ValueError("工具输出不符合 MCP 结果契约")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, allow_nan=False))],
        structured_content=payload, is_error=not payload["ok"],
    )


class _Bridge:
    def __init__(self, registry: ToolRegistry, config: Settings):
        self.registry = registry
        self.config = config
        self.executor: ThreadPoolExecutor | None = None
        self.inflight: set[asyncio.Future] = set()
        self.calls: deque[float] = deque()

    @asynccontextmanager
    async def lifespan(self, server):
        if self.executor is not None:
            raise RuntimeError("同一个 stdio Server 实例不能同时启动多个生命周期")
        executor = ThreadPoolExecutor(max_workers=self.config.MCP_MAX_CONCURRENCY,
                                      thread_name_prefix="mcp-read")
        self.executor = executor
        try:
            yield {}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None

    def authorize(self):
        try:
            require_mcp_access(self.config)
        except PermissionError:
            raise MCPError(-32001, "MCP 工具访问未获授权。") from None

    async def list_tools(self, ctx, params) -> types.ListToolsResult:
        self.authorize()
        return types.ListToolsResult(tools=[types.Tool(
            name=name, description=self.registry.get(name).description,
            input_schema=self.registry.input_schema(name),
            output_schema=deepcopy(OUTPUT_SCHEMA),
            annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                              idempotent_hint=True, open_world_hint=False),
        ) for name in EXPORTED_TOOLS])

    def _invoke(self, name: str, args: dict, run_id: str) -> dict:
        set_run_id(run_id)  # 在独立 copy_context 中设置，线程复用不串日志。
        return self.registry.execute(name, args, run_id=run_id, step=1)

    def _completed(self, future: asyncio.Future):
        self.inflight.discard(future)
        if not future.cancelled():
            future.exception()  # 超时/取消后的异常也被取走，避免未检索异常告警。

    def _finish(self, name: str, run_id: str, started: float,
                *, result: dict | None = None, code: str | None = None) -> types.CallToolResult:
        try:
            reply = _wire_result(_envelope(name, run_id, result=result, code=code))
            if len(reply.model_dump_json(by_alias=True).encode("utf-8")) > self.config.MCP_MAX_RESULT_BYTES:
                code = "result_too_large"
                reply = _wire_result(_envelope(name, run_id, code=code))
        except (TypeError, ValueError, KeyError):
            code = "execution_error"
            reply = _wire_result(_envelope(name, run_id, code=code))
        try:
            log_agent_step(run_id, 1, "mcp_tool_call", json.dumps({"tool": name, "code": code or "ok"}),
                           duration_ms=round((time.monotonic() - started) * 1000, 2))
        except Exception:
            # 不把路径、SQL、凭证或原始异常带到 MCP 客户端。
            return _wire_result(_envelope(name, run_id, code="audit_unavailable"))
        return reply

    async def call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        self.authorize()
        name = params.name
        if name not in EXPORTED_TOOLS:
            raise MCPError(types.INVALID_PARAMS, "未知或未导出的工具。")
        started, run_id = time.monotonic(), uuid4().hex
        while self.calls and started - self.calls[0] >= 60:
            self.calls.popleft()
        if len(self.calls) >= self.config.MCP_CALLS_PER_MINUTE:
            return self._finish(name, run_id, started, code="rate_limited")
        self.calls.append(started)
        args = params.arguments if params.arguments is not None else {}
        if self.registry.validate_args(name, args):
            return self._finish(name, run_id, started, code="invalid_arguments")
        if self.executor is None or len(self.inflight) >= self.config.MCP_MAX_CONCURRENCY:
            return self._finish(name, run_id, started, code="busy")
        future = asyncio.wrap_future(self.executor.submit(
            contextvars.copy_context().run, self._invoke, name, args, run_id,
        ))
        self.inflight.add(future)
        future.add_done_callback(self._completed)
        try:
            result = await asyncio.wait_for(asyncio.shield(future), self.config.MCP_TOOL_TIMEOUT_SECONDS)
        except TimeoutError:
            return self._finish(name, run_id, started, code="timeout")
        except asyncio.CancelledError:
            self._finish(name, run_id, started, code="timeout")
            raise
        except Exception:
            return self._finish(name, run_id, started, code="execution_error")
        if not isinstance(result, dict) or result.get("ok") is not True:
            meta = result.get("_meta") if isinstance(result, dict) else None
            code = meta.get("error_code") if isinstance(meta, dict) else None
            return self._finish(name, run_id, started,
                                code="invalid_arguments" if code == "invalid_arguments" else "execution_error")
        return self._finish(name, run_id, started, result=result)


def create_server(*, config: Settings | None = None, registry: ToolRegistry | None = None) -> Server:
    """构造无网络副作用的 Server；调用时才查询，只提供显式授权的四个工具。"""
    config = config if config is not None else settings
    require_mcp_access(config)
    registry = registry if registry is not None else default_registry()
    if any(registry.get(name) is None for name in EXPORTED_TOOLS):
        raise ValueError("MCP 注册表缺少必需的只读工具。")
    bridge = _Bridge(registry, config)
    return Server(
        "ecommerce-read-tools", version="1.0.0",
        instructions="只读取证，不生成诊断或执行写操作。必须保留 evidence_limits；错误不是成功证据。",
        lifespan=bridge.lifespan, on_list_tools=bridge.list_tools, on_call_tool=bridge.call_tool,
    )
