import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rag.embeddings import (
    EmbeddingError,
    HttpEmbeddingProvider,
    get_embedding_provider,
)

DIMENSION = 64  # smallest dimension the provider schema allows
API_KEY = "sk-unit-test-key"
TEXTS = ["数据库设计", "Redis 缓存"]


def settings(**overrides) -> Settings:
    values = {
        "embedding_api_key": API_KEY,
        "embedding_base_url": "https://embedding.test/v1",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": DIMENSION,
        "embedding_batch_size": 10,
        "embedding_max_retries": 2,
        "embedding_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def provider(handler, **overrides) -> HttpEmbeddingProvider:
    return HttpEmbeddingProvider(settings(**overrides), transport=httpx.MockTransport(handler))


def embedding_response(texts: list[str], *, reverse: bool = False) -> httpx.Response:
    items = [
        {"index": i, "embedding": [float(i + 1)] * DIMENSION, "object": "embedding"}
        for i in range(len(texts))
    ]
    if reverse:
        items.reverse()
    return httpx.Response(200, json={"data": items, "model": "text-embedding-v4"})


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"provider must not be called: {request.url}")


def patch_sleep(monkeypatch, delays: list[float]) -> None:
    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def test_request_shape_and_vectors_follow_input_order():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Provider returns items out of order; adapter must still answer in input order.
        return embedding_response(["数据库设计", "Redis 缓存"], reverse=True)

    adapter = provider(handler)
    vectors = asyncio.run(adapter.embed_texts(TEXTS))

    assert vectors == [[1.0] * DIMENSION, [2.0] * DIMENSION]
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://embedding.test/v1/embeddings"
    assert requests[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(requests[0].content) == {
        "model": "text-embedding-v4",
        "input": TEXTS,
        "dimensions": DIMENSION,
    }


def test_more_texts_than_batch_size_are_split_in_order():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return embedding_response(body["input"])

    texts = [f"片段 {index}" for index in range(7)]
    vectors = asyncio.run(provider(handler, embedding_batch_size=3).embed_texts(texts))

    assert len(vectors) == 7
    assert all(len(vector) == DIMENSION for vector in vectors)
    assert [len(body["input"]) for body in bodies] == [3, 3, 1]
    assert [text for body in bodies for text in body["input"]] == texts


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    delays: list[float] = []
    patch_sleep(monkeypatch, delays)
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return embedding_response(TEXTS)

    vectors = asyncio.run(provider(handler).embed_texts(TEXTS))

    assert vectors == [[1.0] * DIMENSION, [2.0] * DIMENSION]
    assert state["calls"] == 2
    assert delays == [0.5]


def test_timeout_is_retried_then_succeeds(monkeypatch):
    delays: list[float] = []
    patch_sleep(monkeypatch, delays)
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ReadTimeout("simulated timeout")
        return embedding_response(TEXTS)

    vectors = asyncio.run(provider(handler).embed_texts(TEXTS))

    assert len(vectors) == 2
    assert state["calls"] == 2
    assert delays == [0.5]


def test_server_error_retries_are_exhausted_then_raised(monkeypatch):
    delays: list[float] = []
    patch_sleep(monkeypatch, delays)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(TEXTS))

    assert excinfo.value.retryable is True
    assert "503" in str(excinfo.value)
    assert delays == [0.5, 1.0]


def test_authentication_failure_fails_fast_without_retry_or_credential_leak():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(401, json={"error": {"message": "Invalid API-key provided."}})

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(TEXTS))

    assert state["calls"] == 1
    assert excinfo.value.retryable is False
    assert "401" in str(excinfo.value)
    assert "Invalid API-key" in str(excinfo.value)
    assert API_KEY not in str(excinfo.value)


def test_dimension_mismatch_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler, embedding_dimension=128).embed_texts(["文本"]))

    assert "dimension" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_response_count_mismatch_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return embedding_response(TEXTS[:1])

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(TEXTS))

    assert "count" in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {"result": []},
        {"data": "not-a-list"},
        {"data": [{"embedding": [0.1] * DIMENSION}]},
        {"data": [{"index": 0}]},
    ],
)
def test_malformed_responses_are_rejected(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(["文本"]))

    assert excinfo.value.retryable is False


def test_non_json_error_response_is_handled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>bad request</html>")

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(["文本"]))

    assert "400" in str(excinfo.value)
    assert "bad request" in str(excinfo.value)


def test_missing_api_key_is_rejected_at_construction():
    with pytest.raises(EmbeddingError):
        HttpEmbeddingProvider(settings(embedding_api_key=None))


def test_blank_and_empty_inputs_never_call_the_provider():
    adapter = provider(never_called)
    assert asyncio.run(adapter.embed_texts([])) == []
    with pytest.raises(ValueError):
        asyncio.run(adapter.embed_texts(["有效文本", "   "]))


def test_versioning_metadata_is_exposed_for_index_records():
    adapter = provider(never_called, embedding_model="text-embedding-v3", embedding_dimension=768)
    assert adapter.model_version == "text-embedding-v3"
    assert adapter.dimension == 768


def test_factory_builds_provider_from_explicit_settings():
    adapter = get_embedding_provider(settings())
    assert adapter.dimension == DIMENSION
    assert adapter.model_version == "text-embedding-v4"


def test_embedding_settings_reject_out_of_range_values():
    with pytest.raises(ValidationError):
        settings(embedding_dimension=32)
    with pytest.raises(ValidationError):
        settings(embedding_batch_size=11)
    with pytest.raises(ValidationError):
        settings(embedding_max_retries=9)
