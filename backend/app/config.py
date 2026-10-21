from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置：从根目录 .env 读取，密钥字段不回显。

    分组：应用与 scaffold 基础设施 / 向量库（Qdrant）/ Embedding 服务 /
    文档切分 / 证据检索。新增可调参数一律在此登记，并在 .env.example 补充说明。
    """

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)

    app_env: Literal["local", "test", "staging", "production"] = "local"
    mongodb_uri: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    mongodb_database: str = "careerpilot_dev"
    celery_broker_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    dependency_timeout_seconds: float = Field(default=1.5, ge=0.1, le=10)
    cors_allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    local_storage_dir: Path = ROOT / "storage"

    # 向量库（Qdrant，成员 C）：地址、凭据、集合名与调用参数。
    # 在线集群填 HTTPS 地址与 API Key；本地 compose 用 http://127.0.0.1:6333
    # 且 Key 留空（不发鉴权头），两种形态切换只改这两项。
    # 集合为可重建索引；稠密向量维度必须与 EMBEDDING_DIMENSION 一致。
    vector_store_url: str = "http://127.0.0.1:6333"
    vector_store_api_key: SecretStr | None = None
    vector_store_collection: str = "careerpilot_chunks"
    vector_store_timeout_seconds: float = Field(default=10.0, ge=0.1, le=60.0)
    vector_store_max_retries: int = Field(default=2, ge=0, le=5)
    # 重试退避基数（秒）：第 n 次重试等待 backoff * 2^(n-1)。
    vector_store_retry_backoff_seconds: float = Field(default=0.5, ge=0.1, le=10.0)

    # Embedding 服务（成员 C）：OpenAI 兼容端点（默认指向阿里云百炼），M1 只用
    # 稠密向量。换模型/服务商只改下面四项：在线服务（DashScope/OpenAI 等）必须
    # 填 API Key；本地免鉴权推理服务（Ollama/vLLM/Xinference）Key 留空。
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: SecretStr | None = None
    embedding_model: str = "text-embedding-v4"
    # 向量维度：创建集合时冻结，变更须走数据库设计文档的迁移流程。
    embedding_dimension: int = Field(default=1024, ge=64, le=2560)
    # 单请求最多输入的文本条数（provider 上限为 10）。
    embedding_batch_size: int = Field(default=10, ge=1, le=10)
    embedding_timeout_seconds: float = Field(default=10.0, ge=0.1, le=60.0)
    embedding_max_retries: int = Field(default=2, ge=0, le=5)
    embedding_retry_backoff_seconds: float = Field(default=0.5, ge=0.1, le=10.0)

    # 文档切分（成员 C）：单 chunk 目标字符数；参数进入 chunking_config_version。
    chunk_max_chars: int = Field(default=500, ge=100, le=8000)

    # 证据检索（成员 C）：默认返回的候选片段数。
    retrieval_top_k: int = Field(default=5, ge=1, le=100)


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置单例。"""
    return Settings()
