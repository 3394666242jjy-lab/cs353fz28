"""embeddings 适配器单元测试（成员 C，issue #8）。

全部用例经 httpx.MockTransport 本地运行，不触达真实 Embedding 服务；
覆盖请求形状、乱序响应、批量分批、重试策略、鉴权快失败与维度校验。
"""

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

DIMENSION = 64  # provider schema 允许的最小向量维度
API_KEY = "sk-unit-test-key"
TEXTS = ["数据库设计", "Redis 缓存"]


def settings(**overrides) -> Settings:
    """构造测试配置：_env_file=None 隔离本地 .env，可按项覆盖。"""
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
    """用 MockTransport 构造适配器，handler 决定每个请求的响应。"""
    return HttpEmbeddingProvider(settings(**overrides), transport=httpx.MockTransport(handler))


def embedding_response(texts: list[str], *, reverse: bool = False) -> httpx.Response:
    """构造 OpenAI 兼容的 embeddings 响应；reverse=True 模拟乱序返回。"""
    items = [
        {"index": i, "embedding": [float(i + 1)] * DIMENSION, "object": "embedding"}
        for i in range(len(texts))
    ]
    if reverse:
        items.reverse()
    return httpx.Response(200, json={"data": items, "model": "text-embedding-v4"})


def never_called(request: httpx.Request) -> httpx.Response:
    """断言处理器：请求到达即失败，用于验证某路径不应发请求。"""
    raise AssertionError(f"provider must not be called: {request.url}")


def patch_sleep(monkeypatch, delays: list[float]) -> None:
    """把 asyncio.sleep 替换为记录延迟的假实现，避免测试真实等待。"""

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def test_request_shape_and_vectors_follow_input_order():
    """验证请求方法/URL/鉴权头/请求体正确，且响应乱序时输出仍按输入顺序。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
    """输入超过批量上限时自动分批，分批顺序与总数保持输入一致。"""
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
    """429 限流按配置退避重试一次后成功。"""
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
    """超时属于可恢复错误，重试一次后成功。"""
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
    """5xx 连续失败时重试次数用尽，抛出可重试标记的错误。"""
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
    """401 立即失败不重试，原因里带 provider 信息但不泄露本方 API Key。"""
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
    """响应维度与配置不一致时拒绝，防止脏维度进入索引。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler, embedding_dimension=128).embed_texts(["文本"]))

    assert "维度" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_response_count_mismatch_is_rejected():
    """响应数量与输入数量不一致时拒绝。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return embedding_response(TEXTS[:1])

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(TEXTS))

    assert "数量" in str(excinfo.value)


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
    """四种畸形响应（缺字段/类型错/缺 index/缺 embedding）均转为明确错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(["文本"]))

    assert excinfo.value.retryable is False


def test_non_json_error_response_is_handled():
    """非 JSON 错误响应时退化为文本摘录，不崩溃。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>bad request</html>")

    with pytest.raises(EmbeddingError) as excinfo:
        asyncio.run(provider(handler).embed_texts(["文本"]))

    assert "400" in str(excinfo.value)
    assert "bad request" in str(excinfo.value)


def test_missing_api_key_is_rejected_at_construction():
    """构造时缺少 API Key 立即报错（依赖未满足如实报告）。"""
    with pytest.raises(EmbeddingError):
        HttpEmbeddingProvider(settings(embedding_api_key=None))


def test_blank_and_empty_inputs_never_call_the_provider():
    """空输入返回空列表、空白输入抛 ValueError，均不发出请求。"""
    adapter = provider(never_called)
    assert asyncio.run(adapter.embed_texts([])) == []
    with pytest.raises(ValueError):
        asyncio.run(adapter.embed_texts(["有效文本", "   "]))


def test_versioning_metadata_is_exposed_for_index_records():
    """模型与维度以属性暴露，供索引版本留档使用。"""
    adapter = provider(never_called, embedding_model="text-embedding-v3", embedding_dimension=768)
    assert adapter.model_version == "text-embedding-v3"
    assert adapter.dimension == 768


def test_factory_builds_provider_from_explicit_settings():
    """工厂函数按传入配置构建适配器。"""
    adapter = get_embedding_provider(settings())
    assert adapter.dimension == DIMENSION
    assert adapter.model_version == "text-embedding-v4"


def test_embedding_settings_reject_out_of_range_values():
    """维度、批量、重试次数的配置边界被 pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        settings(embedding_dimension=32)
    with pytest.raises(ValidationError):
        settings(embedding_batch_size=11)
    with pytest.raises(ValidationError):
        settings(embedding_max_retries=9)
