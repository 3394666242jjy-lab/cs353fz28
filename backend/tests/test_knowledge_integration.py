"""Opt-in M3.2 public knowledge checks against real MongoDB and Qdrant.

Uses a dedicated database that is dropped after each test; never touches the
development database. Run locally with ``RUN_KNOWLEDGE_TESTS=1`` after
``docker compose up -d``. Embedding uses a deterministic fake provider so the
test needs no external API key; it verifies the Mongo/Qdrant wiring, index
creation, publish/query/retract semantics — not embedding quality.
"""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.rag.knowledge_store import get_knowledge_store
from app.services.database import database_connection
from app.services.knowledge import (
    list_published_sources,
    publish_catalog,
    query_references,
    retract_source,
)

KNOWLEDGE_ROOT = Path(__file__).resolve().parents[3] / "knowledge"
TEST_DATABASE = "careerpilot_knowledge_test"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_KNOWLEDGE_TESTS") != "1",
    reason="Needs local MongoDB and Qdrant (docker compose up -d)",
)


class FakeEmbedding:
    def __init__(self, dimension: int):
        self._dimension = dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        return [((seed >> (i % 60)) % 1000) / 1000.0 for i in range(self._dimension)]

    def model_version(self) -> str:
        return "fake-emb-v1"

    def dimension(self) -> int:
        return self._dimension


@pytest.fixture
def settings():
    uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/?directConnection=true")
    instance = Settings(
        _env_file=None,
        app_env="test",
        mongodb_uri=uri,
        mongodb_database=TEST_DATABASE,
        knowledge_vector_collection=f"knowledge_test_{uuid.uuid4().hex[:8]}",
    )
    yield instance
    with database_connection(instance) as database:
        database.client.drop_database(TEST_DATABASE)
    store = get_knowledge_store(instance)
    try:

        async def drop_collection():
            response = await store._send(
                "DELETE", f"/collections/{instance.knowledge_vector_collection}"
            )
            assert response.status_code in {200, 404}

        asyncio.run(drop_collection())
    except Exception:  # noqa: BLE001 - 清理失败不掩盖测试结果
        pass


def test_publish_query_and_retract_roundtrip(settings):
    embedding = FakeEmbedding(settings.embedding_dimension)
    store = get_knowledge_store(settings)

    report = asyncio.run(
        publish_catalog(
            settings,
            catalog_root=KNOWLEDGE_ROOT,
            embedding=embedding,
            store=store,
        )
    )
    assert not report.failed
    assert len(report.published) == 13

    # 独立集合已创建且与个人集合分离。
    assert store.collection_name == settings.knowledge_vector_collection

    items = list_published_sources(settings, role="agent_engineer")
    assert len(items) >= 4
    assert all(item.usage == "reference_only" for item in items)

    # 真实向量召回 + MongoDB 发布状态复核：agent 方向查 interrupt 主题命中来源。
    result = asyncio.run(
        query_references(
            settings, role="agent_engineer", query="中断 恢复 持久化", embedding=embedding
        )
    )
    assert result.items, "应至少命中一个公共知识引用"
    assert all(item.usage == "reference_only" for item in result.items)
    assert all("agent_engineer" in item.roles for item in result.items)

    # 撤回一个命中来源后，查询立即不再返回它，向量点被物理清理。
    retracted = result.items[0]
    asyncio.run(
        retract_source(
            settings, source_id=retracted.source_id, source_version=retracted.source_version
        )
    )
    after = asyncio.run(
        query_references(
            settings, role="agent_engineer", query="中断 恢复 持久化", embedding=embedding
        )
    )
    assert all(
        item.source_id != retracted.source_id or item.source_version != retracted.source_version
        for item in after.items
    )
    with database_connection(settings) as db:
        row = db.knowledge_sources.find_one({"source_id": retracted.source_id})
        assert row["status"] == "retracted"
        assert row["purge_status"] == "purged"
        remaining = db.knowledge_chunks.count_documents(
            {
                "source_id": retracted.source_id,
                "source_version": retracted.source_version,
                "deleted": {"$ne": True},
            }
        )
        assert remaining == 0

    # 幂等重跑发布：全部跳过。
    again = asyncio.run(
        publish_catalog(settings, catalog_root=KNOWLEDGE_ROOT, embedding=embedding, store=store)
    )
    assert again.published == [] and len(again.skipped) == 13
