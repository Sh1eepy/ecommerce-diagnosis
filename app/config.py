"""应用配置：从 .env 读取，pydantic-settings 统一管理。

设计要点：
- 所有可配置项集中于此，代码不出现硬编码连接串/密钥。
- 写连接（服务层/导入用）与只读连接（Agent Tool 用）分离。
- DB_DRIVER=sqlite 时走 SQLite，便于离线开发与单元测试。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 数据库 ----
    DB_DRIVER: str = "mysql"
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_NAME: str = "ecommerce_diagnosis"
    DB_WRITE_USER: str = "agent_app"
    DB_WRITE_PASSWORD: str = "change_me"
    DB_READ_USER: str = "agent_ro"
    DB_READ_PASSWORD: str = "change_me"
    SQLITE_PATH: str = "./dev.db"

    # ---- LLM (DeepSeek, OpenAI 兼容协议) ----
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = 3

    # ---- Agent 安全上限 ----
    AGENT_MAX_STEPS: int = 8
    AGENT_STEP_TIMEOUT_SECONDS: float = 90.0
    AGENT_TOKEN_BUDGET: int = 16000
    TOOL_RESULT_MAX_ROWS: int = 50
    TOOL_RESULT_MAX_CHARS: int = 4000
    LOG_DIR: str = "./logs"

    # ---- 告警 Webhook（留空 = 不发送）----
    ALERT_WEBHOOK_URL: str = ""
    ALERT_WEBHOOK_SECRET: str = ""

    # ---- API 安全 ----
    API_KEYS: str = "dev-key-123"
    API_RATE_LIMIT_PER_MINUTE: int = 60

    # ---- 任务系统 ----
    WORKER_CONCURRENCY: int = 4
    TASK_MAX_RETRIES: int = 3
    TASK_RETRY_BACKOFF_SECONDS: float = 5.0
    TASK_POLL_INTERVAL_SECONDS: float = 2.0

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
