"""兼容原 Provider，并开放 BaseChatModel 直接接入；预算和失败契约保持统一。"""
from __future__ import annotations

import math
from time import monotonic
from typing import Any

from langchain.messages import AIMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, convert_to_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langsmith import tracing_context
from pydantic import Field

from app.llm.base import LLMClient, LLMResponse
from app.llm.errors import LLMCallError, classify_llm_exception, retry_after_seconds


class ProviderChatModel(BaseChatModel):
    """保留原 messages/JSON mode，不引入原生 tool_calls、缓存或第二层重试。"""

    client: Any = Field(exclude=True, repr=False)

    @property
    def _llm_type(self) -> str:
        return "ecommerce-provider"

    @property
    def _identifying_params(self) -> dict:
        return {"model": self.client.model}

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        if stop is not None:
            raise ValueError("当前 Provider 不支持 stop 参数")
        roles = {"human": "user", "ai": "assistant", "system": "system"}
        payload = []
        for message in messages:
            if message.type not in roles or not isinstance(message.content, str):
                raise ValueError("诊断仅支持 system/user/assistant 文本消息")
            payload.append({"role": roles[message.type], "content": message.content})
        response = self.client.chat(payload, **kwargs)
        message = AIMessage(
            content=response.content,
            response_metadata={"model": response.model, "attempts": response.attempts},
            usage_metadata={"input_tokens": response.tokens_in, "output_tokens": response.tokens_out,
                            "total_tokens": response.tokens_in + response.tokens_out},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class NativeChatClient(LLMClient):
    """把标准 BaseChatModel 接到现有预算/结果契约，无需另写 Provider。

    原生模型直接 invoke，不再经过 ProviderChatModel 的来回转换。
    供应商必须支持可配置的输出/请求超时参数并返回 usage_metadata；
    有 max_retries 配置的模型须在构造时设为 0，避免隐藏重试与尝试计数失真。
    不接管调用方所持模型/网络客户端的关闭生命周期。
    """

    def __init__(self, chat_model: BaseChatModel, *, model_name: str | None = None,
                 max_tokens_parameter: str = "max_tokens", timeout_parameter: str = "timeout",
                 json_mode_options: dict | None = None):
        if not isinstance(chat_model, BaseChatModel):
            raise TypeError("chat_model 必须实现 BaseChatModel")
        if getattr(chat_model, "max_retries", 0) != 0:
            raise ValueError("请在原生模型构造时设置 max_retries=0；重试仍由任务层控制")
        if not max_tokens_parameter or not timeout_parameter or max_tokens_parameter == timeout_parameter:
            raise ValueError("必须分别指定输出额度和请求超时参数")
        self.chat_model = chat_model.model_copy(update={"cache": False, "callbacks": [], "verbose": False})
        self.model = str(model_name or getattr(chat_model, "model_name", None) or getattr(chat_model, "model", None) or chat_model._llm_type)
        self.max_tokens_parameter, self.timeout_parameter = max_tokens_parameter, timeout_parameter
        self.json_mode_options = dict(json_mode_options or {})
        if {max_tokens_parameter, timeout_parameter, "config", "callbacks"} & self.json_mode_options.keys():
            raise ValueError("JSON 模式选项不能覆盖预算、超时或回调配置")

    def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout 必须为有限正数")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
            raise ValueError("max_tokens 必须为正整数")
        options = dict(self.json_mode_options) if json_mode else {}
        if timeout is not None:
            options[self.timeout_parameter] = timeout
        if max_tokens is not None:
            options[self.max_tokens_parameter] = max_tokens
        started = monotonic()
        try:
            with tracing_context(enabled=False):
                response = self.chat_model.invoke(convert_to_messages(messages), config={"callbacks": []}, **options)
        except LLMCallError:
            raise
        except Exception as error:
            # 已知 OpenAI 兼容错误保留分类/服务端等待，未知供应商错误默认停止。
            raise LLMCallError(kind=classify_llm_exception(error), attempts=1,
                               stop_reason="native_model_error", status_code=getattr(error, "status_code", None),
                               retry_after_seconds=retry_after_seconds(error)) from error
        if timeout is not None and monotonic() - started >= timeout:
            raise LLMCallError(kind="unknown", attempts=1, stop_reason="deadline_exhausted")
        if not isinstance(response, AIMessage) or not isinstance(response.content, str) or not response.content.strip() or response.tool_calls:
            raise LLMCallError(kind="unknown", attempts=1, stop_reason="invalid_response")
        usage = response.usage_metadata
        if usage is None:
            # 不把未知计费当 0，避免引入一个可绕过累计预算的接入路径。
            raise LLMCallError(kind="unknown", attempts=1, stop_reason="usage_missing")
        return LLMResponse(content=response.content, tokens_in=usage["input_tokens"],
                           tokens_out=usage["output_tokens"], model=str(self.model), attempts=1)


def as_llm_client(client: LLMClient | BaseChatModel) -> LLMClient:
    return NativeChatClient(client) if isinstance(client, BaseChatModel) else client


def invoke_chat(client: LLMClient | BaseChatModel, messages: list[dict], *, json_mode=True,
                timeout=None, max_tokens=None) -> LLMResponse:
    """诊断与反馈共用入口。强制本地执行，不继承环境中的 LangSmith 自动外发。"""
    with tracing_context(enabled=False):
        client = as_llm_client(client)
        if isinstance(client, NativeChatClient):
            return client.chat(messages, json_mode=json_mode, timeout=timeout, max_tokens=max_tokens)
        response = ProviderChatModel(client=client, cache=False).invoke(
            convert_to_messages(messages), config={"callbacks": []},
            json_mode=json_mode, timeout=timeout, max_tokens=max_tokens,
        )
    usage = response.usage_metadata or {}
    return LLMResponse(
        content=response.content, tokens_in=usage.get("input_tokens", 0),
        tokens_out=usage.get("output_tokens", 0),
        model=response.response_metadata.get("model", ""),
        attempts=response.response_metadata.get("attempts", 1),
    )
