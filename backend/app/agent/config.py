"""D-owned settings, isolated from B/C's shared Settings to avoid config conflicts."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.errors import AgentError
from app.config import ROOT


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_prefix="AGENT_", extra="ignore", env_ignore_empty=True
    )

    base_url: str = ""
    model: str = ""
    api_key: SecretStr | None = None
    require_api_key: bool = True
    response_format: Literal["json_object", "json_schema"] = "json_object"
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    enable_thinking: bool | None = None
    model_timeout_seconds: float = Field(default=30, gt=0, le=30)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=30)
    total_timeout_seconds: float = Field(default=180, gt=0, le=180)
    max_attempts: int = Field(default=2, ge=1, le=2)
    retry_delay_seconds: float = Field(default=0.25, ge=0, le=2)
    max_requirements: int = Field(default=8, ge=1, le=8)
    max_tool_calls: int = Field(default=8, ge=1, le=8)
    top_k: int = Field(default=5, ge=1, le=10)
    max_output_tokens: int = Field(default=4096, ge=128, le=8192)
    max_evidence_chars: int = Field(default=80000, ge=1, le=200000)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_budget(self):
        if self.max_requirements > self.max_tool_calls:
            raise ValueError("Each requirement needs one retrieval within max_tool_calls")
        return self

    def check_provider(self) -> None:
        if not self.base_url.strip() or not self.model.strip():
            raise AgentError("MODEL_NOT_CONFIGURED", "请在本机配置 AGENT_BASE_URL 和 AGENT_MODEL。")
        url = urlsplit(self.base_url)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.scheme not in {"http", "https"}
            or (url.scheme == "http" and not local)
        ):
            raise AgentError("MODEL_NOT_CONFIGURED", "模型地址需使用 HTTPS，或本机 HTTP 地址。")
        if self.require_api_key and (
            self.api_key is None or not self.api_key.get_secret_value().strip()
        ):
            raise AgentError("MODEL_NOT_CONFIGURED", "尚未配置 AGENT_API_KEY；请勿将密钥写入代码。")


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
