from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = Field(default="dev-only-change-me", min_length=16)
    field_encryption_key: str = ""

    database_url: str = "postgresql+asyncpg://firefly:firefly@127.0.0.1:5432/firefly_gen"
    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    redis_url: str = "redis://127.0.0.1:6379/0"
    arp_internal_token: str = "dev-only-internal-token"
    arp_ttl_seconds: int = Field(default=120, ge=30, le=240)
    arp_max_uses: int = Field(default=20, ge=1, le=100)
    arp_key_prefix: str = "firefly:arp"
    submit_impersonate: str = "chrome146"
    poll_timeout_seconds: int = Field(default=120, ge=30, le=600)

    test_cookie_file: str | None = None
    log_level: str = "INFO"
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()
