"""公共知识向量适配器（M3.2，issue #41）。

公共参考知识使用**独立集合**（默认 ``careerpilot_knowledge``），与个人语料的
``careerpilot_chunks`` 完全隔离：不依赖 corpus 参数防漏传，撤回与重建都不会
触碰个人索引。集合只有稠密向量（沿用应用统一的 embedding 维度），payload 为
来源与版本过滤所需的最小字段（source_id/source_version/chunk_id/index_version/
deleted）。chunk 原文与 span 的权威副本在 MongoDB（见 dev/03），这里只存向量。

point id 由 ``知识:{source_id}:{source_version}:{chunk_id}`` 确定性派生，重复
写入幂等；撤回按 (source_id, source_version) 过滤物理删除，重复调用幂等。
REST 调用、重试与错误语义复用 ``vector_store`` 的既有约定，保持两种运行形态
（本地 compose / 在线集群）一致。
"""

import asyncio
import uuid

import httpx
from pydantic import Field

from app.config import Settings, get_settings
from app.rag.vector_store import DENSE_VECTOR_NAME, VectorStoreError
from app.schemas.common import ContractModel

# 公共知识集合的 payload 索引清单：字段名 -> Qdrant 索引类型。
KNOWLEDGE_PAYLOAD_INDEXES = {
    "source_id": "keyword",
    "source_version": "keyword",
    "chunk_id": "keyword",
    "index_version": "keyword",
    "deleted": "bool",
}


class KnowledgePoint(ContractModel):
    """一个待入库的公共知识 chunk 点。``vector`` 长度必须等于 embedding 维度。"""

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    index_version: str = Field(min_length=1)
    vector: list[float] = Field(min_length=1)
    deleted: bool = False

    def payload(self) -> dict:
        """组装写入 Qdrant 的 payload，字段与 KNOWLEDGE_PAYLOAD_INDEXES 一一对应。"""
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "chunk_id": self.chunk_id,
            "index_version": self.index_version,
            "deleted": self.deleted,
        }


class KnowledgeHit(ContractModel):
    """一次公共知识召回的最小信息：定位标识 + 相关度分数（分数只用于排序）。"""

    chunk_id: str
    source_id: str
    source_version: str
    index_version: str
    score: float


def knowledge_point_id(point: KnowledgePoint) -> str:
    """由来源、版本与 chunk 身份派生确定性 UUID，重复 upsert 同一 chunk 幂等。"""
    key = f"知识:{point.source_id}:{point.source_version}:{point.chunk_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class QdrantKnowledgeStore:
    """公共知识集合的 Qdrant REST 实现。``transport`` 仅供测试注入 MockTransport。"""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = (
            settings.vector_store_api_key.get_secret_value()
            if settings.vector_store_api_key
            else None
        )
        self._base_url = settings.vector_store_url.rstrip("/")
        self._collection = settings.knowledge_vector_collection
        self._dimension = settings.embedding_dimension
        self._timeout = settings.vector_store_timeout_seconds
        self._max_retries = settings.vector_store_max_retries
        self._backoff = settings.vector_store_retry_backoff_seconds
        self._transport = transport

    @property
    def collection_name(self) -> str:
        """返回公共知识集合名（运维与诊断用）。"""
        return self._collection

    async def ensure_collection(self) -> None:
        """确保公共知识集合与 payload 索引就绪；幂等，可重复调用。"""
        response = await self._send("GET", f"/collections/{self._collection}")
        if response.status_code == 404:
            body = {
                "vectors": {DENSE_VECTOR_NAME: {"size": self._dimension, "distance": "Cosine"}},
            }
            response = await self._send("PUT", f"/collections/{self._collection}", json_body=body)
            if response.status_code != 200:
                raise self._status_error(response)
        elif response.status_code != 200:
            raise self._status_error(response)
        else:
            self._verify_existing_schema(response)
        for field_name, schema in KNOWLEDGE_PAYLOAD_INDEXES.items():
            index_body = {"field_name": field_name, "field_schema": schema}
            response = await self._send(
                "PUT", f"/collections/{self._collection}/index", json_body=index_body
            )
            if response.status_code != 200:
                raise self._status_error(response)

    def _verify_existing_schema(self, response: httpx.Response) -> None:
        """校验既有集合的向量维度一致；不一致按迁移流程拒绝继续。"""
        params = response.json().get("result", {}).get("config", {}).get("params", {})
        size = params.get("vectors", {}).get(DENSE_VECTOR_NAME, {}).get("size")
        if size != self._dimension:
            raise VectorStoreError(
                f"集合 {self._collection} 现有 {size} 维向量，适配器配置为 "
                f"{self._dimension} 维；变更向量维度须按数据库设计文档重建索引",
                retryable=False,
            )

    async def upsert_chunks(self, points: list[KnowledgePoint]) -> None:
        """写入一个来源版本的若干 chunk 点；发送前逐条校验维度，重复写入幂等。"""
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
                    "id": knowledge_point_id(point),
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

    async def delete_source_points(self, *, source_id: str, source_version: str) -> None:
        """物理删除一个来源版本的全部向量点；过滤删除幂等，清理失败可安全重试。"""
        body = {
            "filter": {
                "must": [
                    {"key": "source_id", "match": {"value": source_id}},
                    {"key": "source_version", "match": {"value": source_version}},
                ]
            },
            "wait": True,
        }
        response = await self._send(
            "POST", f"/collections/{self._collection}/points/delete", json_body=body
        )
        if response.status_code == 404:
            # 集合尚不存在时没有任何可删除的向量，视为已清理。
            return
        if response.status_code != 200:
            raise self._status_error(response)

    async def search(self, *, vector: list[float], top_k: int) -> list[KnowledgeHit]:
        """公共知识稠密召回；只排除已标记删除的点，发布状态由服务层复核。"""
        if len(vector) != self._dimension:
            raise VectorStoreError(
                f"查询向量为 {len(vector)} 维，应为 {self._dimension} 维", retryable=False
            )
        if not 1 <= top_k <= 100:
            raise VectorStoreError("top_k 必须在 1..100 之间", retryable=False)
        body = {
            "vector": {"name": DENSE_VECTOR_NAME, "vector": vector},
            "filter": {"must": [{"key": "deleted", "match": {"value": False}}]},
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
                KnowledgeHit(
                    chunk_id=payload["chunk_id"],
                    source_id=payload["source_id"],
                    source_version=payload["source_version"],
                    index_version=payload["index_version"],
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
        """发送一次请求并按策略处理失败（与 vector_store 同一套重试语义）。"""
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
        """把非 2xx 响应转成 VectorStoreError；429/5xx 记为可重试。"""
        retryable = response.status_code == 429 or response.status_code >= 500
        detail = ""
        try:
            body = response.json()
            error = body.get("status", {}).get("error") or body.get("error", "")
        except ValueError:
            error = response.text
        if isinstance(error, str):
            detail = error.strip()[:200]
        return VectorStoreError(
            f"向量库拒绝 {response.request.method} {response.request.url.path}："
            f"HTTP {response.status_code} {detail}".strip(),
            retryable=retryable,
        )


def get_knowledge_store(settings: Settings | None = None) -> QdrantKnowledgeStore:
    """工厂：默认从全局配置构建公共知识向量适配器，测试可显式传入配置。"""
    return QdrantKnowledgeStore(settings or get_settings())
