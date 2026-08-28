"""Agent 与 Worker 之间的失败契约，以及可恢复存档格式。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.errors import LLMCallError


class FailureInfo(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra="forbid")

    kind: Literal["retryable", "permanent", "unknown"]
    retryable: bool = False
    reason: str = Field(max_length=80)
    attempts: int = Field(default=0, ge=0)
    status_code: int | None = None
    retry_after_seconds: float = Field(default=0.0, ge=0)

    @classmethod
    def from_exception(cls, error: Exception) -> "FailureInfo":
        if isinstance(error, LLMCallError):
            return cls(
                kind=error.kind, retryable=error.retryable, reason=error.stop_reason,
                attempts=error.attempts, status_code=error.status_code,
                retry_after_seconds=error.retry_after_seconds or 0.0,
            )
        # 不把原始异常正文（可能包含请求、凭据）传给队列/API。
        return cls(kind="unknown", reason="unexpected_error")

    def summary(self) -> str:
        return f"kind={self.kind}, reason={self.reason}, status_code={self.status_code}"


class RunBudget(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra="forbid")

    deadline_at: float  # UTC Unix 时间；包括任务重试等待，进程重启不重新计时。
    seconds_limit: float = Field(gt=0)
    max_steps: int = Field(ge=1)
    token_limit: int = Field(ge=1)
    elapsed_ms: float = Field(default=0.0, ge=0)  # 累计执行时间，不含排队等待。


class CheckpointState(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra="forbid")

    schema_version: Literal[1] = 1
    next_step: int = Field(ge=1)
    steps: int = Field(ge=0)
    messages: list[dict]
    investigation: dict
    workflow: dict
    tool_calls: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    llm_attempts: int = Field(ge=0)
    llm_duration_ms: int = Field(ge=0)
    nudges: int = Field(ge=0)
    tool_logs: list[dict]
    used_tools: list[str]
    budget: RunBudget
    retry_not_before: float = 0.0
    memory_refs: list[dict] = Field(default_factory=list)
