"""Embedding 适配器（RAG 模块，成员 C，issue #8）。

按架构文档，M1 只用稠密向量，混合检索与 Rerank 是后续里程碑。本模块是全项目
唯一知道 Embedding provider 线上协议的地方，因此换服务商只改这里，调用方无感；
端点、模型、维度、批量、超时、重试全部来自配置，且模型与维度会随每次索引构建
留档（开发规范要求的 embedding_model / embedding_dimension 记录）。

调用 DashScope 的 OpenAI 兼容 /embeddings 端点。DashScope 另有支持 text_type
与 output_type=dense&sparse 的原生端点，属于 M2 混合检索，届时在本模块扩展，
调用方接口不变。
"""

import asyncio
from typing import Protocol

import httpx

from app.config import Settings, get_settings

# 错误信息中摘录 provider 原因的最大字符数，防止把整页响应写进日志或异常。
ERROR_EXCERPT_CHARS = 200


class EmbeddingError(RuntimeError):
    """Embedding 调用失败。``retryable`` 标记可重试的瞬时错误（限流/服务端/网络）。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmbeddingProvider(Protocol):
    """Embedding 能力协议：调用方只依赖这个接口，不关心具体服务商。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把一批文本逐条转成稠密向量，返回值与输入一一对应、顺序一致。"""
        ...

    @property
    def model_version(self) -> str:
        """provider 模型标识，随索引版本一起留档。"""
        ...

    @property
    def dimension(self) -> int:
        """向量维度，随索引版本一起留档；变更须走索引迁移流程。"""
        ...


class HttpEmbeddingProvider:
    """基于 HTTP 的 Embedding 适配器（DashScope OpenAI 兼容端点）。

    ``transport`` 仅供测试注入 MockTransport，生产路径传空走默认传输。
    """

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not settings.embedding_api_key:
            raise EmbeddingError("EMBEDDING_API_KEY 未配置", retryable=False)
        self._api_key = settings.embedding_api_key.get_secret_value()
        self._base_url = settings.embedding_base_url.rstrip("/")
        self._model = settings.embedding_model
        self._dimension = settings.embedding_dimension
        self._batch_size = settings.embedding_batch_size
        self._timeout = settings.embedding_timeout_seconds
        self._max_retries = settings.embedding_max_retries
        self._backoff = settings.embedding_retry_backoff_seconds
        self._transport = transport

    @property
    def model_version(self) -> str:
        """返回当前配置的模型标识（如 text-embedding-v4）。"""
        return self._model

    @property
    def dimension(self) -> int:
        """返回当前配置的向量维度（如 1024）。"""
        return self._dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """对外入口：校验输入、按批量上限分批、按输入顺序拼接结果。

        provider 约束单请求最多 embedding_batch_size 条输入，因此分批在适配器
        内完成，调用方无需知道；空白文本直接拒绝（输入错误不重试）。
        """
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("待向量化的文本不能为空白")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """单批调用：仅对可恢复错误按指数退避重试，次数用尽或鉴权/参数错误立即抛出。"""
        last_error: EmbeddingError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._call(batch)
            except EmbeddingError as error:
                if not error.retryable or attempt == self._max_retries:
                    raise
                last_error = error
                await asyncio.sleep(self._backoff * (2**attempt))
        raise last_error  # pragma: no cover - 循环要么返回要么抛出

    async def _call(self, texts: list[str]) -> list[list[float]]:
        """执行一次 HTTP 调用并把响应转换为按输入顺序排列的向量列表。

        错误分类：超时/传输失败/429/5xx 记为可重试；4xx（鉴权、参数）立即失败。
        响应解析做三重校验：按 index 排序保证顺序、数量一致、维度一致，
        防止 provider 静默改维度导致脏索引。
        """
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {"model": self._model, "input": texts, "dimensions": self._dimension}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings", headers=headers, json=payload
                )
        except httpx.TimeoutException as error:
            raise EmbeddingError(
                f"embedding 请求超时（{self._timeout}s）", retryable=True
            ) from error
        except httpx.HTTPError as error:
            raise EmbeddingError(
                f"embedding 传输失败：{type(error).__name__}", retryable=True
            ) from error
        if response.status_code == 429:
            raise EmbeddingError("embedding provider 限流", retryable=True)
        if response.status_code >= 500:
            raise EmbeddingError(
                f"embedding provider 错误：HTTP {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise EmbeddingError(
                f"embedding 请求被拒绝：HTTP {response.status_code} {_provider_message(response)}",
                retryable=False,
            )
        try:
            items = response.json()["data"]
            ordered = sorted(items, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in ordered]
        except (ValueError, KeyError, TypeError) as error:
            raise EmbeddingError("embedding 响应缺少 data 字段", retryable=False) from error
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding 响应数量 {len(vectors)} 与输入数量 {len(texts)} 不一致",
                retryable=False,
            )
        if any(len(vector) != self._dimension for vector in vectors):
            actual = {len(vector) for vector in vectors}
            raise EmbeddingError(
                f"embedding 维度 {actual} 与配置维度 {self._dimension} 不一致",
                retryable=False,
            )
        return vectors


def _provider_message(response: httpx.Response) -> str:
    """从错误响应中摘录一段 provider 原因；任何分支都不会带上本方 API Key。"""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:ERROR_EXCERPT_CHARS]
    if not isinstance(body, dict):
        return response.text.strip()[:ERROR_EXCERPT_CHARS]
    error = body.get("error")
    message = error.get("message", "") if isinstance(error, dict) else body.get("message", "")
    return str(message).strip()[:ERROR_EXCERPT_CHARS]


def get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """工厂：默认从全局配置构建 provider，测试可显式传入配置。"""
    return HttpEmbeddingProvider(settings or get_settings())
