"""Qdrant vector store adapter for the RAG module (member C, issue #8).

Qdrant is a rebuildable index: authoritative chunk text and locators stay in
MongoDB, this store only keeps vectors plus the metadata the database design
document requires for payload filtering (corpus, user_id, document_id,
version, chunk_id, index_version, deleted). Ownership and version filters are
always part of the query, never applied after fetching everything.

The adapter speaks the Qdrant REST API so it stays swappable (Milvus is a
future option named in the architecture document) and needs no extra SDK.
Point IDs are derived deterministically so re-indexing a document version is
idempotent: retries upsert the same points instead of duplicating them.
"""

import asyncio
import uuid
from typing import Literal, Protocol

import httpx
from pydantic import Field, model_validator

from app.config import Settings, get_settings
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel

DENSE_VECTOR_NAME = "dense"
PAYLOAD_INDEXES = {
    "corpus": "keyword",
    "user_id": "keyword",
    "document_id": "keyword",
    "version": "integer",
    "chunk_id": "keyword",
    "index_version": "keyword",
    "deleted": "bool",
}


class VectorStoreError(RuntimeError):
    """Store operation failed. ``retryable`` marks transient (429/5xx/network) failures."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ChunkPoint(ContractModel):
    """One indexed chunk. ``vector`` must match the configured embedding dimension."""

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
        if self.corpus == "personal" and not self.user_id:
            raise ValueError("personal chunks must carry user_id")
        return self

    def payload(self) -> dict:
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
    chunk_id: str
    document_id: str
    version: int = Field(ge=1)
    corpus: Literal["personal", "public"]
    score: float


def point_id(point: ChunkPoint) -> str:
    """Deterministic UUID so repeated upserts of the same chunk are idempotent."""
    key = (
        f"{point.corpus}:{point.user_id or ''}:{point.document_id}:{point.version}:{point.chunk_id}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class VectorStore(Protocol):
    async def ensure_collection(self) -> None:
        """Create the collection and payload indexes when absent."""
        ...

    async def upsert_chunks(self, points: list[ChunkPoint]) -> None:
        """Index chunks for a document version; repeats are idempotent."""
        ...

    async def search(
        self,
        *,
        user_id: str,
        vector: list[float],
        document_versions: list[DocumentVersion] | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """Recall candidates for one user, optionally limited to document versions."""
        ...


class QdrantVectorStore:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
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
        self._transport = transport

    @property
    def collection_name(self) -> str:
        return self._collection

    async def ensure_collection(self) -> None:
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
        # Index creation is idempotent and always re-asserted so a partial
        # earlier failure (collection without indexes) repairs on retry.
        for field_name, schema in PAYLOAD_INDEXES.items():
            index_body = {"field_name": field_name, "field_schema": schema}
            response = await self._send(
                "PUT", f"/collections/{self._collection}/index", json_body=index_body
            )
            if response.status_code != 200:
                raise self._status_error(response)

    def _verify_existing_dimension(self, response: httpx.Response) -> None:
        params = (
            response.json().get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
        )
        size = params.get(DENSE_VECTOR_NAME, {}).get("size")
        if size is not None and size != self._dimension:
            raise VectorStoreError(
                f"collection {self._collection} stores {size}-dim vectors; adapter is "
                f"configured for {self._dimension}. Dimension changes need a re-index "
                "(see database design change rules)",
                retryable=False,
            )

    async def upsert_chunks(self, points: list[ChunkPoint]) -> None:
        if not points:
            return
        for point in points:
            if len(point.vector) != self._dimension:
                raise VectorStoreError(
                    f"chunk {point.chunk_id} vector is {len(point.vector)}-dim, "
                    f"expected {self._dimension}",
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
        response = await self._send(
            "PUT",
            f"/collections/{self._collection}/points",
            json_body=body,
            params={"wait": "true"},
        )
        if response.status_code != 200:
            raise self._status_error(response)

    async def search(
        self,
        *,
        user_id: str,
        vector: list[float],
        document_versions: list[DocumentVersion] | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        if len(vector) != self._dimension:
            raise VectorStoreError(
                f"query vector is {len(vector)}-dim, expected {self._dimension}", retryable=False
            )
        must: list[dict] = [
            {"key": "user_id", "match": {"value": user_id}},
            {"key": "deleted", "match": {"value": False}},
        ]
        if document_versions:
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
                f"collection {self._collection} is missing; run ensure_collection first",
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
                last_error = VectorStoreError(
                    f"vector store request timed out after {self._timeout}s", retryable=True
                )
                last_error.__cause__ = error
            except httpx.HTTPError as error:
                last_error = VectorStoreError(
                    f"vector store transport failed: {type(error).__name__}", retryable=True
                )
                last_error.__cause__ = error
            else:
                if response.status_code < 400:
                    return response
                error = self._status_error(response)
                if not error.retryable:
                    # Callers interpret 4xx themselves: for ensure_collection a
                    # 404 is the expected "collection absent" signal, and search
                    # turns its own 404 into a setup hint.
                    return response
                if attempt == self._max_retries:
                    raise error
                last_error = error
            if attempt < self._max_retries:
                await asyncio.sleep(0.5 * (2**attempt))
        raise last_error  # pragma: no cover - loop always returns or raises

    @staticmethod
    def _status_error(response: httpx.Response) -> VectorStoreError:
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
            f"vector store rejected {response.request.method} {response.request.url.path}: "
            f"HTTP {response.status_code} {detail}".strip(),
            retryable=retryable,
        )


def get_vector_store(settings: Settings | None = None) -> VectorStore:
    return QdrantVectorStore(settings or get_settings())
