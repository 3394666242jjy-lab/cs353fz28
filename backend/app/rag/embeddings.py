"""Embedding provider adapter for the RAG module (member C, issue #8).

M1 uses dense vectors only, as fixed by the architecture document; hybrid
retrieval and Rerank are later milestones. The provider is intentionally a
swappable adapter: this module is the only place that knows the provider wire
format, so another service can replace it without touching callers. Endpoint,
model and dimension stay configurable so every index build can record the
embedding_model / embedding_dimension / chunking_config_version versions the
development standards require.

The DashScope-compatible endpoint is called over the OpenAI embeddings schema.
DashScope additionally offers a native endpoint (text_type, output_type=
dense&sparse); those arrive with M2 hybrid retrieval and belong here too.
"""

import asyncio
from typing import Protocol

import httpx

from app.config import Settings, get_settings


class EmbeddingError(RuntimeError):
    """Provider call failed. ``retryable`` marks transient (429/5xx/network) failures."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmbeddingProvider(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input text, in input order."""
        ...

    @property
    def model_version(self) -> str:
        """Provider model identifier recorded with the index version."""
        ...

    @property
    def dimension(self) -> int:
        """Vector dimension recorded with the index version."""
        ...


class HttpEmbeddingProvider:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not settings.embedding_api_key:
            raise EmbeddingError("EMBEDDING_API_KEY is not configured", retryable=False)
        self._api_key = settings.embedding_api_key.get_secret_value()
        self._base_url = settings.embedding_base_url.rstrip("/")
        self._model = settings.embedding_model
        self._dimension = settings.embedding_dimension
        self._batch_size = settings.embedding_batch_size
        self._timeout = settings.embedding_timeout_seconds
        self._max_retries = settings.embedding_max_retries
        self._transport = transport

    @property
    def model_version(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("embedding input texts cannot be blank")
        # Provider constraint: one request carries at most embedding_batch_size
        # inputs, so larger chunk lists are split here instead of by callers.
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        last_error: EmbeddingError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._call(batch)
            except EmbeddingError as error:
                if not error.retryable or attempt == self._max_retries:
                    raise
                last_error = error
                await asyncio.sleep(0.5 * (2**attempt))
        raise last_error  # pragma: no cover - loop always returns or raises

    async def _call(self, texts: list[str]) -> list[list[float]]:
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
                f"embedding request timed out after {self._timeout}s", retryable=True
            ) from error
        except httpx.HTTPError as error:
            raise EmbeddingError(
                f"embedding transport failed: {type(error).__name__}", retryable=True
            ) from error
        if response.status_code == 429:
            raise EmbeddingError("embedding provider rate limited", retryable=True)
        if response.status_code >= 500:
            raise EmbeddingError(
                f"embedding provider error: HTTP {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise EmbeddingError(
                f"embedding request rejected: HTTP {response.status_code} "
                f"{_provider_message(response)}",
                retryable=False,
            )
        try:
            items = response.json()["data"]
            ordered = sorted(items, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in ordered]
        except (ValueError, KeyError, TypeError) as error:
            raise EmbeddingError("embedding response missing data", retryable=False) from error
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding response count {len(vectors)} != input count {len(texts)}",
                retryable=False,
            )
        if any(len(vector) != self._dimension for vector in vectors):
            actual = {len(vector) for vector in vectors}
            raise EmbeddingError(
                f"embedding dimension {actual} != configured {self._dimension}",
                retryable=False,
            )
        return vectors


def _provider_message(response: httpx.Response) -> str:
    """Return a short provider error excerpt; never echo our own credentials."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if not isinstance(body, dict):
        return response.text.strip()[:200]
    error = body.get("error")
    message = error.get("message", "") if isinstance(error, dict) else body.get("message", "")
    return str(message).strip()[:200]


def get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    return HttpEmbeddingProvider(settings or get_settings())
