"""Qdrant 向量库适配器（RAG 模块，成员 C，issue #8）。

Qdrant 是可重建索引：chunk 原文与 locator 的权威副本在 MongoDB，这里只存向量
加上数据库设计文档要求的 payload 过滤字段（corpus、user_id、document_id、
version、chunk_id、index_version、deleted）。用户与版本过滤永远写在查询条件里，
绝不"先全量取回再过滤"。

运行形态切换只改配置（VECTOR_STORE_URL / VECTOR_STORE_API_KEY）：在线集群
（Qdrant Cloud）填 HTTPS 地址与 API Key；本地开发用仓库 compose.yaml 的
Qdrant 容器（http://127.0.0.1:6333，Key 留空即不发鉴权头）。两种形态的 REST
行为一致，索引结构（具名 dense 向量 + payload 字段）与过滤语义完全相同，
索引数据可按文档重建，切换环境不影响已约定的字段。

适配器直接调用 Qdrant REST API：不引 SDK，也与架构文档提到的未来 Milvus 选项
保持可替换。point id 确定性生成；已写入的文档版本不可原地覆盖。
"""

import asyncio
import uuid
from typing import Literal, Protocol

import httpx
from pydantic import Field, model_validator

from app.config import Settings, get_settings
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel

# 稠密向量在 Qdrant 集合中的命名字段；M2 引入稀疏向量时另起名字（如 sparse），
# 数据库设计文档要求稠密与关键词稀疏表示使用不同命名字段。
DENSE_VECTOR_NAME = "dense"

# payload 索引清单：字段名 -> Qdrant 索引类型，来自数据库设计文档的最小集合。
PAYLOAD_INDEXES = {
    "corpus": "keyword",
    "user_id": "keyword",
    "document_id": "keyword",
    "version": "integer",
    "chunk_id": "keyword",
    "index_version": "keyword",
    "deleted": "bool",
}

# 错误信息中摘录 vector store 原因的最大字符数。
ERROR_EXCERPT_CHARS = 200


class VectorStoreError(RuntimeError):
    """向量库操作失败。``retryable`` 标记可重试的瞬时错误（限流/服务端/网络）。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ChunkPoint(ContractModel):
    """一个待入库的 chunk 点。``vector`` 长度必须等于配置的 embedding 维度。"""

    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    corpus: Literal["personal", "public"]
    user_id: str | None = None
    index_version: str = Field(min_length=1)
    vector: list[float] = Field(min_length=1)
    deleted: bool = False

    @model_validator(mode="after")
    def personal_requires_owner(self):
        """个人语料必须携带 user_id，否则拒绝构造（公共语料允许无属主）。"""
        if self.corpus == "personal" and not self.user_id:
            raise ValueError("personal 片段必须携带 user_id")
        return self

    def payload(self) -> dict:
        """组装写入 Qdrant 的 payload，字段与 PAYLOAD_INDEXES 一一对应。"""
        return {
            "corpus": self.corpus,
            "user_id": self.user_id,
            "document_id": self.document_id,
            "version": self.version,
            "chunk_id": self.chunk_id,
            "index_version": self.index_version,
            "deleted": self.deleted,
        }


class SearchHit(ContractModel):
    """一次检索命中的最小信息：定位标识 + 相关度分数（分数只用于排序）。"""

    chunk_id: str
    document_id: str
    version: int = Field(ge=1)
    corpus: Literal["personal", "public"]
    score: float


def point_id(point: ChunkPoint) -> str:
    """由 chunk 身份字段派生确定性 UUID，保证重复 upsert 同一 chunk 是幂等的。"""
    key = (
        f"{point.corpus}:{point.user_id or ''}:{point.document_id}:{point.version}:{point.chunk_id}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class VectorStore(Protocol):
    """向量库能力协议：调用方只依赖接口，不绑定 Qdrant 具体实现。"""

    async def ensure_collection(self) -> None:
        """集合不存在时创建，并（重）建立全部 payload 索引。"""
        ...

    async def upsert_chunks(self, points: list[ChunkPoint]) -> None:
        """写入一个文档版本的若干 chunk 点；重复写入幂等。"""
        ...

    async def replace_document_points(
        self,
        *,
        user_id: str,
        document_id: str,
        version: int,
        points: list[ChunkPoint],
    ) -> None:
        """写入尚不存在的文档版本；已有向量时拒绝覆盖。"""
        ...

    async def search(
        self,
        *,
        user_id: str,
        vector: list[float],
        document_versions: list[DocumentVersion] | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """为一个用户召回候选 chunk，可限定文档版本；结果保持 store 返回顺序。"""
        ...


class QdrantVectorStore:
    """基于 Qdrant REST API 的实现。``transport`` 仅供测试注入 MockTransport。"""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # 本地 Compose 无鉴权时 key 为空，属正常配置；header 仅在配置了 key 时附加。
        self._api_key = (
            settings.vector_store_api_key.get_secret_value()
            if settings.vector_store_api_key
            else None
        )
        self._base_url = settings.vector_store_url.rstrip("/")
        self._collection = settings.vector_store_collection
        self._dimension = settings.embedding_dimension
        self._timeout = settings.vector_store_timeout_seconds
        self._max_retries = settings.vector_store_max_retries
        self._backoff = settings.vector_store_retry_backoff_seconds
        self._transport = transport

    @property
    def collection_name(self) -> str:
        """返回当前配置的集合名（运维与诊断用）。"""
        return self._collection

    async def ensure_collection(self) -> None:
        """确保集合与 payload 索引就绪。

        集合已存在时跳过创建（并校验维度一致）；索引始终重新断言——建索引是幂等
        操作，因此"集合建成但索引失败"的半完成状态会在重试时自动修复。
        """
        response = await self._send("GET", f"/collections/{self._collection}")
        if response.status_code == 404:
            body = {"vectors": {DENSE_VECTOR_NAME: {"size": self._dimension, "distance": "Cosine"}}}
            response = await self._send("PUT", f"/collections/{self._collection}", json_body=body)
            if response.status_code != 200:
                raise self._status_error(response)
        elif response.status_code != 200:
            raise self._status_error(response)
        else:
            self._verify_existing_dimension(response)
        for field_name, schema in PAYLOAD_INDEXES.items():
            index_body = {"field_name": field_name, "field_schema": schema}
            response = await self._send(
                "PUT", f"/collections/{self._collection}/index", json_body=index_body
            )
            if response.status_code != 200:
                raise self._status_error(response)

    def _verify_existing_dimension(self, response: httpx.Response) -> None:
        """校验已存在集合的稠密向量维度与配置一致，不一致按迁移流程拒绝继续。"""
        params = (
            response.json().get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
        )
        size = params.get(DENSE_VECTOR_NAME, {}).get("size")
        if size is not None and size != self._dimension:
            raise VectorStoreError(
                f"集合 {self._collection} 现有 {size} 维向量，适配器配置为 {self._dimension} 维；"
                "变更向量维度须按数据库设计文档的变更规则重建索引",
                retryable=False,
            )

    async def upsert_chunks(self, points: list[ChunkPoint]) -> None:
        """写入 chunk 点：发送前逐条校验维度，point id 由 point_id 确定性派生。"""
        if not points:
            return
        for point in points:
            if len(point.vector) != self._dimension:
                raise VectorStoreError(
                    f"chunk {point.chunk_id} 向量为 {len(point.vector)} 维，"
                    f"应为 {self._dimension} 维",
                    retryable=False,
                )
        body = {
            "points": [
                {
                    "id": point_id(point),
                    "vector": {DENSE_VECTOR_NAME: point.vector},
                    "payload": point.payload(),
                }
                for point in points
            ]
        }
        # wait=true：等 Qdrant 确认落库后再返回，避免"上报成功但尚未可检索"。
        response = await self._send(
            "PUT",
            f"/collections/{self._collection}/points",
            json_body=body,
            params={"wait": "true"},
        )
        if response.status_code != 200:
            raise self._status_error(response)

    async def replace_document_points(
        self,
        *,
        user_id: str,
        document_id: str,
        version: int,
        points: list[ChunkPoint],
    ) -> None:
        """Write an immutable document version; never delete published points.

        If any points already exist, require a new document version. A partial
        failed build must be cleaned up before retrying that unpublished version.
        """
        if not points:
            raise VectorStoreError("不能为空文档发布向量", retryable=False)
        for point in points:
            if (point.user_id, point.document_id, point.version) != (user_id, document_id, version):
                raise VectorStoreError(
                    f"chunk {point.chunk_id} 不属于 {user_id}/{document_id}/v{version}",
                    retryable=False,
                )
            if len(point.vector) != self._dimension:
                raise VectorStoreError(
                    f"chunk {point.chunk_id} 向量为 {len(point.vector)} 维，"
                    f"应为 {self._dimension} 维",
                    retryable=False,
                )
        filter_body = {
            "filter": {
                "must": [
                    {"key": "user_id", "match": {"value": user_id}},
                    {"key": "document_id", "match": {"value": document_id}},
                    {"key": "version", "match": {"value": version}},
                ]
            },
            "limit": 1,
            "with_payload": False,
            "with_vector": False,
        }
        response = await self._send(
            "POST", f"/collections/{self._collection}/points/scroll", json_body=filter_body
        )
        if response.status_code != 200:
            raise self._status_error(response)
        existing = response.json().get("result", {}).get("points")
        if not isinstance(existing, list):
            raise VectorStoreError("向量库返回了无效的版本检查结果", retryable=False)
        if existing:
            raise VectorStoreError("文档版本已有向量；请创建新版本，不能原地重建", retryable=False)
        await self.upsert_chunks(points)

    async def search(
        self,
        *,
        user_id: str,
        vector: list[float],
        document_versions: list[DocumentVersion] | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """按用户隔离执行向量召回，可选限定文档版本，返回 payload 组装的命中列表。"""
        if document_versions == []:
            return []
        if len(vector) != self._dimension:
            raise VectorStoreError(
                f"查询向量为 {len(vector)} 维，应为 {self._dimension} 维", retryable=False
            )
        # 所有权过滤永远内置：user_id 精确匹配 + 排除已删除点。
        must: list[dict] = [
            {"key": "user_id", "match": {"value": user_id}},
            {"key": "deleted", "match": {"value": False}},
        ]
        if document_versions:
            # 每个 (document_id, version) 是一组并列条件，用 should-of-must 精确表达。
            must.append(
                {
                    "should": [
                        {
                            "must": [
                                {"key": "document_id", "match": {"value": item.document_id}},
                                {"key": "version", "match": {"value": item.version}},
                            ]
                        }
                        for item in document_versions
                    ]
                }
            )
        body = {
            "vector": {"name": DENSE_VECTOR_NAME, "vector": vector},
            "filter": {"must": must},
            "limit": top_k,
            "with_payload": True,
        }
        response = await self._send(
            "POST", f"/collections/{self._collection}/points/search", json_body=body
        )
        if response.status_code == 404:
            raise VectorStoreError(
                f"集合 {self._collection} 不存在；请先执行 ensure_collection",
                retryable=False,
            )
        if response.status_code != 200:
            raise self._status_error(response)
        hits = []
        for item in response.json().get("result", []):
            payload = item.get("payload", {})
            hits.append(
                SearchHit(
                    chunk_id=payload["chunk_id"],
                    document_id=payload["document_id"],
                    version=payload["version"],
                    corpus=payload["corpus"],
                    score=item["score"],
                )
            )
        return hits

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """发送一次请求并按策略处理失败。

        429/5xx/超时/传输错误：按指数退避重试，次数用尽后抛出；其余 4xx 原样返回
        给调用方解释（例如集合不存在是 ensure_collection 的预期信号，检索的 404
        会转成"请先初始化"的提示）。
        """
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        url = f"{self._base_url}{path}"
        last_error: VectorStoreError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    response = await client.request(
                        method, url, headers=headers, json=json_body, params=params
                    )
            except httpx.TimeoutException as error:
                last_error = VectorStoreError(f"向量库请求超时（{self._timeout}s）", retryable=True)
                last_error.__cause__ = error
            except httpx.HTTPError as error:
                last_error = VectorStoreError(
                    f"向量库传输失败：{type(error).__name__}", retryable=True
                )
                last_error.__cause__ = error
            else:
                if response.status_code < 400:
                    return response
                error = self._status_error(response)
                if not error.retryable:
                    return response
                if attempt == self._max_retries:
                    raise error
                last_error = error
            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff * (2**attempt))
        raise last_error  # pragma: no cover - 循环要么返回要么抛出

    @staticmethod
    def _status_error(response: httpx.Response) -> VectorStoreError:
        """把非 2xx 响应转成 VectorStoreError；429/5xx 记为可重试，原因截断展示。"""
        retryable = response.status_code == 429 or response.status_code >= 500
        detail = ""
        try:
            body = response.json()
            error = body.get("status", {}).get("error") or body.get("error", "")
        except ValueError:
            error = response.text
        if isinstance(error, str):
            detail = error.strip()[:ERROR_EXCERPT_CHARS]
        return VectorStoreError(
            f"向量库拒绝 {response.request.method} {response.request.url.path}："
            f"HTTP {response.status_code} {detail}".strip(),
            retryable=retryable,
        )


def get_vector_store(settings: Settings | None = None) -> VectorStore:
    """工厂：默认从全局配置构建向量库适配器，测试可显式传入配置。"""
    return QdrantVectorStore(settings or get_settings())
