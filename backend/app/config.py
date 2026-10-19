from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)

    app_env: Literal["local", "test", "staging", "production"] = "local"
    mongodb_uri: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    mongodb_database: str = "careerpilot_dev"
    celery_broker_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    vector_store_url: str = "http://127.0.0.1:6333"
    vector_store_api_key: SecretStr | None = None
    dependency_timeout_seconds: float = Field(default=1.5, ge=0.1, le=10)
    cors_allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    local_storage_dir: Path = ROOT / "storage"


@lru_cache
def get_settings() -> Settings:
    return Settings()
