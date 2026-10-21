"""retrieval 证据检索单元测试（成员 C，issue #8）。

用真实 embedder 与真实 store + MockTransport 验证检索链路：
候选组装、用户与版本过滤下发、空结果给原因、失败路径不伪造证据。
本模块只返回候选元数据，原文文本由后端查 MongoDB 补齐（不在此测试范围内）。
"""

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.rag.embeddings import EmbeddingError, HttpEmbeddingProvider
from app.rag.retrieval import EvidenceQueryResult, QdrantEvidenceSource, get_evidence_source
from app.rag.vector_store import QdrantVectorStore
from app.schemas.analysis import DocumentVersion

DIMENSION = 64
EMBEDDING_URL = "https://embedding.test/v1/embeddings"
SEARCH_URL = "https://qdrant.test/collections/careerpilot_chunks/points/search"


def settings(**overrides) -> Settings:
    """构造测试配置：隔离本地 .env，可按项覆盖。"""
    values = {
        "embedding_base_url": "https://embedding.test/v1",
        "embedding_api_key": "sk-unit",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": DIMENSION,
        "embedding_max_retries": 2,
        "retrieval_top_k": 5,
        "vector_store_url": "https://qdrant.test",
        "vector_store_api_key": "qdrant-key",
        "vector_store_collection": "careerpilot_chunks",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def hit(chunk_id: str, score: float, version: int = 1) -> dict:
    """构造一条 Qdrant 检索命中的原始响应条目。"""
    return {
        "id": chunk_id,
        "score": score,
        "payload": {
            "chunk_id": chunk_id,
            "document_id": "doc_demo",
            "version": version,
            "corpus": "personal",
        },
    }


def source(hits: list[dict], *, embedding_status: int = 200):
    """构建被测候选源（真实适配器）并返回请求记录列表。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = str(request.url)
        if url == EMBEDDING_URL:
            if embedding_status != 200:
                return httpx.Response(embedding_status, json={"error": {"message": "nope"}})
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [0.01] * DIMENSION}
                        for i in range(len(body["input"]))
                    ],
                    "model": "text-embedding-v4",
                },
            )
        if url == SEARCH_URL:
            return httpx.Response(200, json={"result": hits})
        raise AssertionError(f"unexpected request: {url}")

    transport = httpx.MockTransport(handler)
    config = settings()
    embedder = HttpEmbeddingProvider(config, transport=transport)
    store = QdrantVectorStore(config, transport=transport)
    return QdrantEvidenceSource(config, embedder=embedder, store=store), calls


def search_body(calls: list[httpx.Request]) -> dict:
    """断言恰好发生一次检索请求，并返回其请求体。"""
    search_calls = [c for c in calls if str(c.url) == SEARCH_URL]
    assert len(search_calls) == 1
    return json.loads(search_calls[0].content)


def test_retrieve_returns_ranked_candidates_with_config_version():
    """候选保持检索返回顺序，元数据完整，且恰好按 向量化→检索 顺序发生两次调用。"""
    adapter, calls = source([hit("chunk_a", 0.71), hit("chunk_b", 0.42)])

    result = asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="数据库设计经历"))

    assert isinstance(result, EvidenceQueryResult)
    assert [e.chunk_id for e in result.evidence] == ["chunk_a", "chunk_b"]
    assert [e.retrieval_score for e in result.evidence] == [0.71, 0.42]
    assert all(e.document_id == "doc_demo" and e.version == 1 for e in result.evidence)
    assert all(e.corpus == "personal" for e in result.evidence)
    assert result.warnings == []
    assert result.retrieval_config_version == f"dense/text-embedding-v4@{DIMENSION}/top5"
    assert [str(c.url) for c in calls] == [EMBEDDING_URL, SEARCH_URL]
    body = search_body(calls)
    conditions = {json.dumps(item, sort_keys=True) for item in body["filter"]["must"]}
    assert (
        json.dumps({"key": "user_id", "match": {"value": "user_c"}}, sort_keys=True) in conditions
    )
    assert body["limit"] == 5


def test_document_versions_are_forwarded_as_version_pairs():
    """文档版本白名单以 (document_id, version) 对的形式下发到检索过滤器。"""
    adapter, calls = source([hit("chunk_a", 0.5)])

    asyncio.run(
        adapter.retrieve_evidence(
            user_id="user_c",
            query="缓存优化",
            document_versions=[DocumentVersion(document_id="doc_a", version=1)],
            top_k=3,
        )
    )

    body = search_body(calls)
    assert body["limit"] == 3
    assert body["filter"]["must"][-1]["should"] == [
        {
            "must": [
                {"key": "document_id", "match": {"value": "doc_a"}},
                {"key": "version", "match": {"value": 1}},
            ]
        }
    ]


def test_empty_result_returns_an_explicit_reason_not_fabricated_evidence():
    """零命中返回空列表加明确原因，不伪造证据、不抛异常。"""
    adapter, calls = source([])

    result = asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="Docker 镜像部署"))

    assert result.evidence == []
    assert result.warnings == ["在请求的用户与文档版本范围内，没有匹配该查询的个人证据"]
    assert [str(c.url) for c in calls] == [EMBEDDING_URL, SEARCH_URL]


def test_blank_query_is_rejected_before_any_call():
    """空白查询在发出任何请求前即被拒绝。"""
    adapter, calls = source([hit("chunk_a", 0.5)])

    with pytest.raises(ValueError):
        asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="   "))

    assert calls == []


def test_embedding_failure_propagates_and_skips_the_search():
    """向量化失败如实向上传播，且不会发生检索调用。"""
    adapter, calls = source([], embedding_status=401)

    with pytest.raises(EmbeddingError):
        asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="数据库设计"))

    assert [str(c.url) for c in calls] == [EMBEDDING_URL]


def test_config_version_tracks_model_and_top_k():
    """检索配置版本反映模型、维度与 top_k；工厂函数可按配置构建。"""
    adapter, _ = source([hit("chunk_a", 0.5)])
    assert isinstance(get_evidence_source(settings()), QdrantEvidenceSource)

    first = asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="q", top_k=3))
    second = asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="q", top_k=7))
    assert first.retrieval_config_version.endswith("/top3")
    assert second.retrieval_config_version.endswith("/top7")


def test_default_top_k_comes_from_settings():
    """未显式指定 top_k 时取配置 retrieval_top_k。"""
    adapter, calls = source([hit("chunk_a", 0.5)])

    asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="q"))

    assert search_body(calls)["limit"] == settings().retrieval_top_k


def test_repeated_query_is_deterministic():
    """同一查询重复执行结果一致。"""
    adapter, _ = source([hit("chunk_a", 0.61), hit("chunk_b", 0.31)])

    first = asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="Redis 缓存"))
    second = asyncio.run(adapter.retrieve_evidence(user_id="user_c", query="Redis 缓存"))

    assert first == second
