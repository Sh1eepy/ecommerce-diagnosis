"""应用配置：从 .env 读取，pydantic-settings 统一管理。

设计要点：
- 所有可配置项集中于此，代码不出现硬编码连接串/密钥。
- 写连接（服务层/导入用）与只读连接（Agent Tool 用）分离。
- DB_DRIVER=sqlite 时走 SQLite，便于离线开发与单元测试。
"""
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ApiScope = Literal["report:read", "diagnosis:create", "data:import", "feedback:create", "tools:read"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    APP_ENV: Literal["development", "test", "production"] = "development"

    # ---- 数据库 ----
    DB_DRIVER: str = "mysql"
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_NAME: str = "ecommerce_diagnosis"
    DB_WRITE_USER: str = "agent_app"
    DB_WRITE_PASSWORD: str = Field(default="change_me", repr=False)
    DB_READ_USER: str = "agent_ro"
    DB_READ_PASSWORD: str = Field(default="change_me", repr=False)
    SQLITE_PATH: str = "./dev.db"

    # ---- LLM (DeepSeek, OpenAI 兼容协议) ----
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = Field(default="", repr=False)
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # 额外重试次数：0 表示只发一次请求；3 表示最多 4 次尝试。
    LLM_MAX_RETRIES: int = Field(default=3, ge=0, le=5)

    # ---- Agent 安全上限 ----
    AGENT_MAX_STEPS: int = Field(default=8, ge=1)
    AGENT_STEP_TIMEOUT_SECONDS: float = Field(default=90.0, gt=0, allow_inf_nan=False)
    AGENT_TOTAL_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    AGENT_TOKEN_BUDGET: int = Field(default=30000, ge=1)
    AGENT_MAX_OUTPUT_TOKENS: int = Field(default=2048, ge=512, le=8192)
    FEEDBACK_LLM_TOKEN_BUDGET: int = Field(default=6000, ge=512, le=20000)
    FEEDBACK_LLM_MAX_OUTPUT_TOKENS: int = Field(default=1200, ge=512, le=2048)
    FEEDBACK_LLM_TIMEOUT_SECONDS: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    FEEDBACK_MAX_DRAFTS_PER_REPORT: int = Field(default=3, ge=1, le=10)
    TOOL_RESULT_MAX_ROWS: int = 50
    TOOL_RESULT_MAX_CHARS: int = 4000
    LOG_DIR: str = "./logs"

    # ---- 告警 Webhook（留空 = 不发送）----
    ALERT_WEBHOOK_URL: str = ""
    ALERT_WEBHOOK_SECRET: str = Field(default="", repr=False)

    # ---- API 安全 ----
    # 旧式全权限 Key 只允许开发/测试使用；生产环境必须显式配置各 Key 的权限。
    API_KEYS: str = Field(default="dev-key-123", repr=False)
    API_KEY_SCOPES: dict[str, list[ApiScope]] = Field(default_factory=dict, repr=False)
    API_RATE_LIMIT_PER_MINUTE: int = Field(default=60, gt=0)
    MAX_UPLOAD_BYTES: int = Field(default=50 * 1024 * 1024, gt=0)
    MAX_UPLOAD_ROWS: int = Field(default=100_000, gt=0)

    # ---- 本机 MCP（stdio；显式启用与授权，不继承旧式 API_KEYS）----
    MCP_ENABLED: bool = False
    MCP_ACCESS_KEY: str = Field(default="", repr=False)
    MCP_TOOL_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=300, allow_inf_nan=False)
    MCP_MAX_CONCURRENCY: int = Field(default=2, ge=1, le=8)
    MCP_MAX_RESULT_BYTES: int = Field(default=128 * 1024, ge=4096, le=1024 * 1024)
    MCP_CALLS_PER_MINUTE: int = Field(default=60, ge=1, le=600)

    # ---- 监控面板：token 成本估算单价（元/百万 tokens，按官方定价自行更新）----
    LLM_INPUT_PRICE_PER_M: float = 2.0
    LLM_OUTPUT_PRICE_PER_M: float = 8.0

    # ---- 任务系统 ----
    WORKER_CONCURRENCY: int = Field(default=4, ge=1, le=64)
    # 历史命名，保留总尝试次数语义（包含首次）；与 LLM_MAX_RETRIES 不同。
    TASK_MAX_RETRIES: int = Field(default=3, ge=1, le=10)
    TASK_RETRY_BACKOFF_SECONDS: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    TASK_POLL_INTERVAL_SECONDS: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    TASK_LEASE_SECONDS: float = Field(default=60.0, ge=3, allow_inf_nan=False)
    TASK_HEARTBEAT_SECONDS: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    TASK_RECOVERY_INTERVAL_SECONDS: float = Field(default=15.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_lease(self):
        if self.TASK_HEARTBEAT_SECONDS > self.TASK_LEASE_SECONDS / 3:
            raise ValueError("心跳间隔不得大于租约时长的三分之一")
        return self

    @model_validator(mode="after")
    def validate_production(self):
        if self.APP_ENV != "production":
            return self
        if self.api_key_list or not self.API_KEY_SCOPES:
            raise ValueError("生产环境必须清空 API_KEYS，并配置 API_KEY_SCOPES")
        if any(len(key) < 32 or key != key.strip() or not scopes
               for key, scopes in self.API_KEY_SCOPES.items()):
            raise ValueError("生产 API Key 至少 32 个字符、不得有首尾空白且必须分配权限")
        if not self.LLM_API_KEY.strip() or self.LLM_API_KEY == "sk-your-deepseek-key":
            raise ValueError("生产环境必须配置真实 LLM_API_KEY，禁止自动使用 Mock")
        if self.DB_DRIVER != "mysql":
            raise ValueError("生产环境必须使用支持独立只读账号的 MySQL")
        if self.DB_READ_USER == self.DB_WRITE_USER:
            raise ValueError("生产环境必须使用不同的数据库读写账号")
        if any(not password.strip() or password == "change_me" for password in
               (self.DB_READ_PASSWORD, self.DB_WRITE_PASSWORD)):
            raise ValueError("生产环境必须替换默认数据库密码")
        return self

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.API_KEYS.split(",") if k.strip()]

    def write_url(self) -> str:
        """写连接：仅供服务层/数据导入使用。"""
        if self.DB_DRIVER == "sqlite":
            return f"sqlite:///{self.SQLITE_PATH}"
        return (
            f"mysql+pymysql://{self.DB_WRITE_USER}:{self.DB_WRITE_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    def read_url(self) -> str:
        """只读连接：仅供 Agent Tool 使用（对应 agent_ro 只读账号）。"""
        if self.DB_DRIVER == "sqlite":
            return f"sqlite:///{self.SQLITE_PATH}"
        return (
            f"mysql+pymysql://{self.DB_READ_USER}:{self.DB_READ_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )


settings = Settings()
