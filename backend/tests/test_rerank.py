"""Rerank 适配器单元测试（RAG 模块，成员 C，issue #25）。"""

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.rag.rerank import DashScopeRerankProvider, RerankError, get_rerank_provider

RERANK_URL = "https://dashscope.test/api/v1/services/rerank/text-rerank/text-rerank"


def settings(**overrides) -> Settings:
    """构造测试配置：隔离本地 .env，可按项覆盖。"""
    values = {
        "rerank_url": RERANK_URL,
        "rerank_api_key": "sk-rerank",
        "rerank_model": "gte-rerank-v2",
        "rerank_max_retries": 0,
        "embedding_api_key": "sk-embed",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def provider(handler, **overrides) -> DashScopeRerankProvider:
    """构建被测适配器，请求走 MockTransport。"""
    return DashScopeRerankProvider(settings(**overrides), transport=httpx.MockTransport(handler))


def results(*pairs: tuple[int, float]) -> httpx.Response:
    """构造标准 rerank 成功响应。"""
    return httpx.Response(
        200,
        json={
            "output": {
                "results": [{"index": index, "relevance_score": score} for index, score in pairs]
            },
            "usage": {"total_tokens": 10},
        },
    )


def test_rerank_sends_native_dashscope_payload():
    """按百炼原生协议发送：input.query/documents + parameters，且不回传文档正文。"""
    bodies: list[dict] = []
    headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        headers.append(dict(request.headers))
        return results((0, 0.9), (1, 0.1))

    asyncio.run(
        provider(handler).rerank(
            query="缓存命中率", documents=["缓存命中率 92%", "数据库版本 8.0.36"], top_n=2
        )
    )

    assert bodies[0] == {
        "model": "gte-rerank-v2",
        "input": {"query": "缓存命中率", "documents": ["缓存命中率 92%", "数据库版本 8.0.36"]},
        "parameters": {"return_documents": False, "top_n": 2},
    }
    assert headers[0]["authorization"] == "Bearer sk-rerank"


def test_rerank_sorts_by_score_and_maps_back_to_input_order():
    """provider 返回的顺序不可信：按分数降序排，并把下标映射回输入位置。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return results((2, 0.2), (0, 0.95), (1, 0.5))

    scored = asyncio.run(provider(handler).rerank(query="q", documents=["a", "b", "c"], top_n=3))

    assert scored == [(0, 0.95), (1, 0.5), (2, 0.2)]


def test_rerank_with_no_documents_makes_no_request():
    """空候选直接返回，不浪费一次调用。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起请求")

    assert asyncio.run(provider(handler).rerank(query="q", documents=[])) == []


def test_rerank_blank_query_is_rejected_before_any_call():
    """空查询是调用方错误，直接拒绝。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起请求")

    with pytest.raises(ValueError):
        asyncio.run(provider(handler).rerank(query="  ", documents=["a"]))


def test_rerank_result_count_mismatch_is_an_error():
    """未指定 top_n 时少返回几条，说明 provider 静默丢了候选——必须报错。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return results((0, 0.9))

    with pytest.raises(RerankError) as excinfo:
        asyncio.run(provider(handler).rerank(query="q", documents=["a", "b"]))

    assert "不一致" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_rerank_top_n_truncation_is_accepted():
    """指定 top_n 时 provider 只返回前 n 条，这是协议行为而不是丢候选。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return results((1, 0.9), (0, 0.4))

    scored = asyncio.run(provider(handler).rerank(query="q", documents=["a", "b", "c"], top_n=2))

    assert scored == [(1, 0.9), (0, 0.4)]


def test_rerank_out_of_range_index_is_an_error():
    """下标越界说明响应不可信，不能用错误候选去打分。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return results((5, 0.9))

    with pytest.raises(RerankError, match="越界"):
        asyncio.run(provider(handler).rerank(query="q", documents=["a"]))


def test_rerank_classifies_rate_limit_and_server_errors_as_retryable():
    """429/5xx 可重试；4xx 立即失败并摘录 provider 原因。"""

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "slow down"})

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "AccessDenied", "message": "Access denied."})

    with pytest.raises(RerankError) as limited_error:
        asyncio.run(provider(limited).rerank(query="q", documents=["a"]))
    with pytest.raises(RerankError) as broken_error:
        asyncio.run(provider(broken).rerank(query="q", documents=["a"]))
    with pytest.raises(RerankError) as denied_error:
        asyncio.run(provider(denied).rerank(query="q", documents=["a"]))

    assert limited_error.value.retryable is True
    assert broken_error.value.retryable is True
    assert denied_error.value.retryable is False
    assert "Access denied." in str(denied_error.value)


def test_rerank_retries_then_succeeds(monkeypatch):
    """可重试错误按退避重试，成功后正常返回。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"message": "slow down"})
        return results((0, 0.8))

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    scored = asyncio.run(provider(handler, rerank_max_retries=2).rerank(query="q", documents=["a"]))

    assert len(calls) == 2
    assert scored == [(0, 0.8)]


async def _no_sleep(_seconds: float) -> None:
    """测试用：跳过退避等待。"""
    return None


def test_rerank_api_key_falls_back_to_embedding_key():
    """两个服务共用同一个百炼密钥：RERANK_API_KEY 留空时回退到 Embedding 的密钥。"""
    headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(dict(request.headers))
        return results((0, 0.5))

    asyncio.run(provider(handler, rerank_api_key=None).rerank(query="q", documents=["a"]))

    assert headers[0]["authorization"] == "Bearer sk-embed"


def test_factory_returns_none_when_rerank_is_not_configured():
    """未配置模型或地址时返回 None，由调用方决定报错——未配置不等于可以静默跳过。"""
    assert get_rerank_provider(settings(rerank_model="")) is None
    assert get_rerank_provider(settings(rerank_url="")) is None
    assert get_rerank_provider(settings()) is not None
