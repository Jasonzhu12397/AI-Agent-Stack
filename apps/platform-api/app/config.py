from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Agent Stack"
    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://agent:agent_dev_password@localhost:5432/agent_stack"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    worker_poll_seconds: float = Field(default=2.0, gt=0, le=300)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
