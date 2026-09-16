"""Application configuration.

All configuration comes from environment variables (12-factor). Nothing environment-specific
is hard-coded: the same Docker image runs on a laptop, in CI and on EC2 — only the
environment differs. Validation happens at startup, so a missing secret crashes the
process immediately instead of failing on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------------------
    app_name: str = "task-platform"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    api_prefix: str = "/api/v1"
    # Identifies the process in logs/metrics (e.g. "api-1", "worker-2").
    instance_id: str = "local"

    # --- Security ----------------------------------------------------------------------
    jwt_secret_key: SecretStr = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    cors_allowed_origins: list[str] = Field(default_factory=list)

    # --- PostgreSQL --------------------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "taskplatform"
    postgres_user: str = "taskplatform"
    postgres_password: SecretStr = SecretStr("")
    # Pool sizing: (pool_size + max_overflow) * number_of_processes must stay below
    # PostgreSQL max_connections. 3 APIs * (5+5) + 2 workers * (5+5) = 50 < 100.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_seconds: float = 5.0
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 5000

    # --- Redis -------------------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: SecretStr = SecretStr("")
    redis_db: int = 0
    redis_socket_timeout_seconds: float = 0.5
    cache_task_ttl_seconds: int = 60
    cache_ttl_jitter_seconds: int = 10

    # --- Rate limiting -----------------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    # --- RabbitMQ ----------------------------------------------------------------------
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "taskplatform"
    rabbitmq_password: SecretStr = SecretStr("")
    rabbitmq_vhost: str = "/"
    rabbitmq_publish_timeout_seconds: float = 5.0

    # --- Worker ------------------------------------------------------------------------
    worker_concurrency: int = 4  # == RabbitMQ prefetch count
    worker_metrics_port: int = 9101
    worker_processing_lease_seconds: int = 60
    worker_cpu_pool_size: int = 2

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        # Allow CORS_ALLOWED_ORIGINS="https://a.com,https://b.com" as well as JSON lists.
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rabbitmq_url(self) -> str:
        password = self.rabbitmq_password.get_secret_value()
        vhost = "%2F" if self.rabbitmq_vhost == "/" else self.rabbitmq_vhost
        return (
            f"amqp://{self.rabbitmq_user}:{password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/{vhost}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
