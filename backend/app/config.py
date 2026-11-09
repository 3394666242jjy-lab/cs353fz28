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
    seed_email: str = "demo@example.invalid"
    seed_display_name: str = "Local demo"
    seed_password: SecretStr | None = None
    access_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    execution_lease_seconds: int = Field(default=300, ge=60, le=86400)
    execution_queue_timeout_seconds: int = Field(default=3600, ge=60, le=86400)

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

    # 检索策略（成员 C，M2，issue #25）：三档可复现对比的开关。dense 是 M1 基线，
    # hybrid 叠加关键词稀疏召回，hybrid_rerank 再对候选重排。策略名连同模型版本
    # 进入 retrieval_config_version，任何一项变化都产生新版本号，不覆盖旧结果。
    retrieval_strategy: Literal["dense", "hybrid", "hybrid_rerank"] = "dense"
    # 混合检索融合权重：稀疏通道占比 w，稠密通道占 1-w（各档必须在同一 w 下比较）。
    hybrid_sparse_weight: float = Field(default=0.5, gt=0.0, lt=1.0)
    # 每个通道在融合前各自召回的候选数；应大于等于 retrieval_top_k 才谈得上重排空间。
    hybrid_candidates_per_channel: int = Field(default=20, ge=1, le=200)

    # Rerank 服务（成员 C，M2）：只服务于 hybrid_rerank 档位。未配置时必须显式报错，
    # 不得静默降级——把未重排的结果记成重排结果会污染评测结论。
    # 使用阿里云百炼的**原生** rerank 端点（兼容端点没有 /rerank 路径）；
    # 密钥留空时回退到 EMBEDDING_API_KEY，因为两者本就共用同一个百炼 Key。
    rerank_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    )
    rerank_api_key: SecretStr | None = None
    # 账号可用的重排模型；qwen-rerank 在部分账号上返回 403（未开通），需按账号实测。
    rerank_model: str = "gte-rerank-v2"
    rerank_timeout_seconds: float = Field(default=15.0, ge=0.1, le=60.0)
    rerank_max_retries: int = Field(default=2, ge=0, le=5)
    rerank_retry_backoff_seconds: float = Field(default=0.5, ge=0.1, le=10.0)


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置单例。"""
    return Settings()
