"""indexer 入库管线单元测试（成员 C，issue #8）。

用两个真实适配器 + MockTransport 做全 HTTP 层编排测试：
验证请求顺序（先向量化后写库）、幂等重跑、失败路径不破坏旧索引，
以及返回给成员 B 的版本元数据完整性。
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.rag.chunking import PARSER_VERSION
from app.rag.embeddings import EmbeddingError, HttpEmbeddingProvider
from app.rag.indexer import build_index_version, index_document, make_chunk_id
from app.rag.vector_store import ChunkPoint, QdrantVectorStore, point_id

ROOT = Path(__file__).resolve().parents[2]
PROJECT_BYTES = (ROOT / "examples" / "project-demo.md").read_bytes()

DIMENSION = 64
EMBEDDING_URL = "https://embedding.test/v1/embeddings"
SCROLL_URL = "https://qdrant.test/collections/careerpilot_chunks/points/scroll"
UPSERT_URL = "https://qdrant.test/collections/careerpilot_chunks/points?wait=true"


def settings(**overrides) -> Settings:
    """构造测试配置：隔离本地 .env，可按项覆盖。"""
    values = {
        "embedding_base_url": "https://embedding.test/v1",
        "embedding_api_key": "sk-unit",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": DIMENSION,
        "embedding_batch_size": 10,
        "embedding_max_retries": 2,
        "chunk_max_chars": 100,
        "vector_store_url": "https://qdrant.test",
        "vector_store_api_key": "qdrant-key",
        "vector_store_collection": "careerpilot_chunks",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class RoutingHandler:
    """按 URL 路由 embedding 与 Qdrant 请求，并按调用顺序记录全部请求。"""

    def __init__(self, *, embedding_status: int = 200) -> None:
        self.calls: list[httpx.Request] = []
        self.embedding_status = embedding_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url == EMBEDDING_URL:
            if self.embedding_status != 200:
                return httpx.Response(self.embedding_status, json={"error": {"message": "nope"}})
            body = json.loads(request.content)
            items = [
                {"index": i, "embedding": [0.01 * (i + 1)] * DIMENSION}
                for i in range(len(body["input"]))
            ]
            return httpx.Response(200, json={"data": items, "model": "text-embedding-v4"})
        if url == SCROLL_URL:
            return httpx.Response(200, json={"result": {"points": []}})
        if url == UPSERT_URL:
            return httpx.Response(200, json={"result": {"status": "ok"}})
        raise AssertionError(f"unexpected request: {url}")


def pipeline(handler: RoutingHandler, **overrides):
    """由同一个 MockTransport 构建 embedder 与 store，保证请求顺序可观测。"""
    config = settings(**overrides)
    embedder = HttpEmbeddingProvider(config, transport=httpx.MockTransport(handler))
    store = QdrantVectorStore(config, transport=httpx.MockTransport(handler))
    return embedder, store, config


def urls(handler: RoutingHandler) -> list[str]:
    """取出全部请求的 URL 序列。"""
    return [str(request.url) for request in handler.calls]


def run_index(handler: RoutingHandler, *, embedder, store, config, **overrides):
    """执行一次 index_document 的同步封装。"""
    return asyncio.run(
        index_document(
            PROJECT_BYTES,
            user_id="user_c",
            document_id="doc_demo",
            version=1,
            embedder=embedder,
            store=store,
            settings=config,
            **overrides,
        )
    )


def test_project_demo_end_to_end_index_build():
    """端到端：4 个 chunk、行区间正确、元数据齐全、请求顺序为 向量化→删除→upsert。"""
    handler = RoutingHandler()
    embedder, store, config = pipeline(handler)

    report = run_index(handler, embedder=embedder, store=store, config=config)

    assert report.chunk_count == 4
    assert [(c.locator.line_start, c.locator.line_end) for c in report.chunks] == [
        (1, 7),
        (9, 11),
        (13, 16),
        (18, 24),
    ]
    assert report.chunks[1].text.startswith("## 数据库设计工作")
    assert "负责设计设备" in report.chunks[1].text
    assert report.corpus == "personal"
    assert report.user_id == "user_c"
    assert report.parser_version == PARSER_VERSION
    assert report.chunking_config_version == "block-pack-v1::100"
    assert report.embedding_model == "text-embedding-v4"
    assert report.embedding_dimension == DIMENSION
    assert report.index_version == (
        f"{PARSER_VERSION}/block-pack-v1::100/text-embedding-v4@{DIMENSION}/dense-Cosine"
    )
    assert report.document_versions[0].document_id == "doc_demo"
    assert report.document_versions[0].version == 1

    # 报告中的 vector_id 必须与存储层自身派生的 point id 一致。
    first = ChunkPoint(
        chunk_id=report.chunks[0].chunk_id,
        document_id="doc_demo",
        version=1,
        corpus="personal",
        user_id="user_c",
        index_version=report.index_version,
        vector=[0.01] * DIMENSION,
    )
    assert report.chunks[0].vector_id == point_id(first)

    # 顺序与结构：先向量化，再按版本删除，最后 upsert。
    assert urls(handler) == [EMBEDDING_URL, SCROLL_URL, UPSERT_URL]
    delete_body = json.loads(handler.calls[1].content)
    conditions = {item["key"]: item["match"]["value"] for item in delete_body["filter"]["must"]}
    assert conditions == {"user_id": "user_c", "document_id": "doc_demo", "version": 1}
    upsert_body = json.loads(handler.calls[2].content)
    assert len(upsert_body["points"]) == 4
    assert all(len(entry["vector"]["dense"]) == DIMENSION for entry in upsert_body["points"])
    assert all(entry["payload"]["user_id"] == "user_c" for entry in upsert_body["points"])
    assert all(
        entry["payload"]["index_version"] == report.index_version for entry in upsert_body["points"]
    )


def test_chunk_ids_for_same_version_are_deterministic():
    """在隔离存储中，相同版本和内容产生相同标识。"""
    handler = RoutingHandler()
    embedder, store, config = pipeline(handler)

    first = run_index(handler, embedder=embedder, store=store, config=config)
    second = run_index(handler, embedder=embedder, store=store, config=config)

    assert first == second
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]
    assert [c.vector_id for c in first.chunks] == [c.vector_id for c in second.chunks]
    assert urls(handler).count(SCROLL_URL) == 2
    assert urls(handler).count(UPSERT_URL) == 2


def test_empty_document_does_not_touch_existing_index():
    """空文档：明确失败，不调用 Embedding 或删除已有向量。"""
    handler = RoutingHandler()
    embedder, store, config = pipeline(handler)

    with pytest.raises(ValueError, match="没有可索引的文本"):
        asyncio.run(
            index_document(
                b"\n\n",
                user_id="user_c",
                document_id="doc_demo",
                version=1,
                embedder=embedder,
                store=store,
                settings=config,
            )
        )
    assert handler.calls == []


def test_parse_failure_never_touches_embedding_or_store():
    """解析失败直接抛出，Embedding 与存储都不被调用。"""
    handler = RoutingHandler()
    embedder, store, config = pipeline(handler)

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            index_document(
                b"\xff\xfe",
                user_id="user_c",
                document_id="doc_demo",
                version=1,
                embedder=embedder,
                store=store,
                settings=config,
            )
        )
    assert "UTF-8" in str(excinfo.value)
    assert handler.calls == []


def test_embedding_failure_leaves_the_previous_index_intact():
    """Embedding 401 快失败：存储零调用，旧索引不受影响。"""
    handler = RoutingHandler(embedding_status=401)
    embedder, store, config = pipeline(handler)

    with pytest.raises(EmbeddingError):
        asyncio.run(
            index_document(
                PROJECT_BYTES,
                user_id="user_c",
                document_id="doc_demo",
                version=1,
                embedder=embedder,
                store=store,
                settings=config,
            )
        )
    assert urls(handler) == [EMBEDDING_URL]  # 401 不重试，存储从未被调用


def test_dimension_drift_fails_before_deleting_the_old_index():
    """provider 静默改维度：在删除发生前拒绝，旧索引保留。"""

    class DriftingEmbedder:
        """返回 32 维向量的假 embedder，模拟服务端维度漂移。"""

        model_version = "text-embedding-v4"
        dimension = DIMENSION

        async def embed_texts(self, texts):
            return [[0.5] * 32 for _ in texts]

    handler = RoutingHandler()
    store = QdrantVectorStore(settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            index_document(
                PROJECT_BYTES,
                user_id="user_c",
                document_id="doc_demo",
                version=1,
                embedder=DriftingEmbedder(),
                store=store,
                settings=settings(),
            )
        )
    assert "32" in str(excinfo.value)
    assert handler.calls == []  # 未发删除、未发 upsert


def test_index_version_moves_with_chunking_parameters():
    """切分参数变化时 chunking_config_version 与 index_version 随之变化。"""
    handler = RoutingHandler()
    embedder, store, config = pipeline(handler)
    another_handler = RoutingHandler()
    another_embedder, another_store, another_config = pipeline(another_handler, chunk_max_chars=120)

    first = run_index(handler, embedder=embedder, store=store, config=config)
    second = run_index(
        another_handler, embedder=another_embedder, store=another_store, config=another_config
    )

    assert first.chunking_config_version != second.chunking_config_version
    assert first.index_version != second.index_version


def test_chunk_and_index_version_helpers():
    """chunk_id 与 index_version 辅助函数：确定性与组合格式。"""
    assert make_chunk_id("doc", 1, 0) == make_chunk_id("doc", 1, 0)
    assert make_chunk_id("doc", 1, 0) != make_chunk_id("doc", 1, 1)
    assert (
        build_index_version(
            parser_version=PARSER_VERSION,
            chunking_config_version="block-pack-v1::60",
            embedding_model="text-embedding-v4",
            embedding_dimension=1024,
        )
        == f"{PARSER_VERSION}/block-pack-v1::60/text-embedding-v4@1024/dense-Cosine"
    )
