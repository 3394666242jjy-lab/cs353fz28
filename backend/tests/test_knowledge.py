"""Tests for the M3.2 public knowledge ingest/query service (issue #41)."""

import asyncio
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.config import Settings
from app.rag.knowledge_store import (
    KnowledgePoint,
    QdrantKnowledgeStore,
    knowledge_point_id,
)
from app.schemas.knowledge import KnowledgeQueryIn
from app.services import knowledge as knowledge_service
from app.services.knowledge import (
    list_published_sources,
    publish_catalog,
    query_references,
    retract_source,
)

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = ROOT / "knowledge"


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


class FakeEmbedding:
    """Deterministic fake embedding provider; vectors differ per text."""

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


class FakeStore:
    """In-memory knowledge store recording calls; search ranks by vector similarity."""

    def __init__(self, dimension: int):
        self.dimension = dimension
        self.points: dict[str, KnowledgePoint] = {}
        self.deleted: set[str] = set()
        self.ensure_calls = 0
        self.delete_calls: list[tuple[str, str]] = []
        self.fail_upsert = False
        self.fail_delete_times = 0

    async def ensure_collection(self) -> None:
        self.ensure_calls += 1

    async def upsert_chunks(self, points: list[KnowledgePoint]) -> None:
        if self.fail_upsert:
            raise RuntimeError("向量库不可用")
        for point in points:
            self.points[knowledge_point_id(point)] = point

    async def delete_source_points(self, *, source_id: str, source_version: str) -> None:
        self.delete_calls.append((source_id, source_version))
        if self.fail_delete_times > 0:
            self.fail_delete_times -= 1
            raise RuntimeError("向量库删除失败")
        for key, point in list(self.points.items()):
            if (point.source_id, point.source_version) == (source_id, source_version):
                del self.points[key]
        self.deleted.add(f"{source_id}@{source_version}")

    async def search(self, *, vector: list[float], top_k: int):
        # 测试用伪相似度：与存储向量差值越小分越高，足以区分候选。
        hits = []
        for point in self.points.values():
            if f"{point.source_id}@{point.source_version}" in self.deleted:
                continue
            diff = sum(abs(a - b) for a, b in zip(vector, point.vector, strict=False))
            hits.append(
                SimpleNamespace(
                    chunk_id=point.chunk_id,
                    source_id=point.source_id,
                    source_version=point.source_version,
                    index_version=point.index_version,
                    score=1.0 / (1.0 + diff),
                )
            )
        hits.sort(key=lambda h: -h.score)
        return hits[:top_k]


@pytest.fixture
def fake_db(monkeypatch):
    db = MagicMock()
    state = {"sources": [], "chunks": []}

    def matches(row, query):
        return all(row.get(k) == v for k, v in query.items() if not isinstance(v, dict))

    def apply_set(rows, query, update):
        for row in rows:
            if matches(row, query):
                row.update(update.get("$set", {}))

    def find_one_source(query, projection=None):
        for row in state["sources"]:
            if matches(row, query):
                return dict(row)
        return None

    def find_one_chunk(query, projection=None):
        for row in state["chunks"]:
            ok = matches(row, query)
            if ok and query.get("deleted") == {"$ne": True} and row["deleted"]:
                ok = False
            if ok:
                return dict(row)
        return None

    db.knowledge_sources.find_one.side_effect = find_one_source
    db.knowledge_sources.find.side_effect = lambda query, projection=None: []
    db.knowledge_sources.insert_one.side_effect = lambda row: state["sources"].append(dict(row))
    db.knowledge_sources.update_one.side_effect = lambda query, update: apply_set(
        state["sources"], query, update
    )
    db.knowledge_sources.delete_one.side_effect = lambda query: [
        state["sources"].remove(r) for r in list(state["sources"]) if matches(r, query)
    ]
    db.knowledge_chunks.find_one.side_effect = find_one_chunk
    db.knowledge_chunks.insert_many.side_effect = lambda rows: state["chunks"].extend(
        dict(r) for r in rows
    )
    db.knowledge_chunks.update_many.side_effect = lambda query, update: apply_set(
        state["chunks"], query, update
    )
    db.knowledge_chunks.delete_many.side_effect = lambda query: [
        state["chunks"].remove(r) for r in list(state["chunks"]) if matches(r, query)
    ]

    @contextmanager
    def fake_connection(_settings):
        yield db

    monkeypatch.setattr(knowledge_service, "database_connection", fake_connection)
    return db, state


def _point(row, vector=None):
    return KnowledgePoint(
        chunk_id=f"{row['source_id']}#c1",
        source_id=row["source_id"],
        source_version=row["version"],
        index_version="iv",
        vector=vector or [0.0] * 8,
    )


def _seed_published(state, rows):
    state["sources"] = [dict(row, status="published") for row in rows]
    state["chunks"] = [
        {
            "source_id": row["source_id"],
            "source_version": row["version"],
            "chunk_id": f"{row['source_id']}#c1",
            "text": row["text"],
            "deleted": False,
        }
        for row in rows
    ]


def test_point_id_is_deterministic_and_scoped_to_version():
    base = dict(
        chunk_id="c1", source_id="pm-pair", source_version="2026-11-13.r1", index_version="iv"
    )
    first = KnowledgePoint(vector=[0.0] * 8, **base)
    same = KnowledgePoint(vector=[1.0] * 8, **base)
    other_version = KnowledgePoint(
        vector=[0.0] * 8,
        chunk_id="c1",
        source_id="pm-pair",
        source_version="2026-11-13.r2",
        index_version="iv",
    )
    assert knowledge_point_id(first) == knowledge_point_id(same)
    assert knowledge_point_id(first) != knowledge_point_id(other_version)
    assert first.payload() == {
        "source_id": "pm-pair",
        "source_version": "2026-11-13.r1",
        "chunk_id": "c1",
        "index_version": "iv",
        "deleted": False,
    }


def test_store_creates_collection_with_payload_indexes(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"status": {"error": "Not found"}})
        return httpx.Response(200, json={"result": True})

    store = QdrantKnowledgeStore(settings, transport=httpx.MockTransport(handler))
    asyncio.run(store.ensure_collection())
    puts = [path for method, path in requests if method == "PUT"]
    assert puts[0] == "/collections/careerpilot_knowledge"
    index_puts = puts[1:]
    assert len(index_puts) == 5
    assert all(path == "/collections/careerpilot_knowledge/index" for path in index_puts)


def test_store_search_filters_deleted_and_parses_hits(settings):
    seen_filters = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_filters.append(json.loads(request.content)["filter"])
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "score": 0.9,
                        "payload": {
                            "chunk_id": "k1",
                            "source_id": "pm-pair",
                            "source_version": "2026-11-13.r1",
                            "index_version": "iv",
                        },
                    }
                ]
            },
        )

    store = QdrantKnowledgeStore(settings, transport=httpx.MockTransport(handler))
    hits = asyncio.run(store.search(vector=[0.0] * settings.embedding_dimension, top_k=4))
    assert seen_filters == [{"must": [{"key": "deleted", "match": {"value": False}}]}]
    assert len(hits) == 1
    assert hits[0].source_id == "pm-pair"
    assert hits[0].score == 0.9


def test_store_rejects_wrong_dimension(settings):
    store = QdrantKnowledgeStore(
        settings, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    with pytest.raises(Exception, match="维"):
        asyncio.run(store.search(vector=[0.0] * 8, top_k=4))


def test_publish_catalog_is_idempotent_and_publishes_rows(settings, fake_db):
    db, state = fake_db
    embedding = FakeEmbedding(settings.embedding_dimension)
    store = FakeStore(settings.embedding_dimension)

    report = asyncio.run(
        publish_catalog(settings, catalog_root=KNOWLEDGE_ROOT, embedding=embedding, store=store)
    )
    assert not report.failed
    assert len(report.published) == 13
    assert len(state["sources"]) == 13
    assert len(state["chunks"]) > 13
    assert all(row["status"] == "published" for row in state["sources"])
    assert len(store.points) == len(state["chunks"])
    # 幂等：第二次整批跳过。
    again = asyncio.run(
        publish_catalog(settings, catalog_root=KNOWLEDGE_ROOT, embedding=embedding, store=store)
    )
    assert again.published == [] and len(again.skipped) == 13
    assert len(store.points) == len(state["chunks"])


def test_publish_rolls_back_mongo_rows_when_vector_write_fails(settings, fake_db):
    db, state = fake_db
    store = FakeStore(settings.embedding_dimension)
    store.fail_upsert = True
    report = asyncio.run(
        publish_catalog(
            settings,
            catalog_root=KNOWLEDGE_ROOT,
            embedding=FakeEmbedding(settings.embedding_dimension),
            store=store,
        )
    )
    assert len(report.failed) == 13
    # Mongo 行被回滚：库里没有任何半成品版本。
    assert state["sources"] == [] and state["chunks"] == []


def test_retract_blocks_query_then_cleans_vectors(settings, fake_db, monkeypatch):
    monkeypatch.setattr(knowledge_service, "PURGE_BACKOFF_BASE_SECONDS", 0)
    db, state = fake_db
    embedding = FakeEmbedding(settings.embedding_dimension)
    store = FakeStore(settings.embedding_dimension)
    asyncio.run(
        publish_catalog(settings, catalog_root=KNOWLEDGE_ROOT, embedding=embedding, store=store)
    )
    source_id = state["sources"][0]["source_id"]
    version = state["sources"][0]["version"]

    asyncio.run(retract_source(settings, source_id=source_id, source_version=version, store=store))
    row = next(r for r in state["sources"] if r["source_id"] == source_id)
    assert row["status"] == "retracted"
    assert row["purge_status"] == "purged"
    assert all(
        chunk["deleted"]
        for chunk in state["chunks"]
        if chunk["source_id"] == source_id and chunk["source_version"] == version
    )
    assert not any(
        point.source_id == source_id and point.source_version == version
        for point in store.points.values()
    )
    # 幂等：重复撤回直接收尾，不报错。
    asyncio.run(retract_source(settings, source_id=source_id, source_version=version, store=store))


def test_retract_marks_failed_when_purge_keeps_failing(settings, fake_db, monkeypatch):
    monkeypatch.setattr(knowledge_service, "PURGE_BACKOFF_BASE_SECONDS", 0)
    db, state = fake_db
    store = FakeStore(settings.embedding_dimension)
    asyncio.run(
        publish_catalog(
            settings,
            catalog_root=KNOWLEDGE_ROOT,
            embedding=FakeEmbedding(settings.embedding_dimension),
            store=store,
        )
    )
    source_id = state["sources"][0]["source_id"]
    version = state["sources"][0]["version"]
    store.fail_delete_times = 10
    with pytest.raises(RuntimeError, match="向量清理失败"):
        asyncio.run(
            retract_source(settings, source_id=source_id, source_version=version, store=store)
        )
    row = next(r for r in state["sources"] if r["source_id"] == source_id)
    assert row["status"] == "retracted"  # 查询已被阻断
    assert row["purge_status"] == "failed"


def test_retract_unknown_source_raises(settings, fake_db):
    with pytest.raises(ValueError, match="未知知识来源版本"):
        asyncio.run(retract_source(settings, source_id="nope", source_version="v0"))


def test_query_filters_retracted_role_mismatch_and_notice(settings, fake_db):
    db, state = fake_db
    rows = [
        {
            "source_id": "pm-pair",
            "version": "r1",
            "title": "PAIR",
            "url": "https://example.com/pair",
            "roles": ["ai_product_manager"],
            "topics": ["用户需求"],
            "text": "产品价值 " * 200,
        },
        {
            "source_id": "pm-metrics",
            "version": "r1",
            "title": "Rules of ML",
            "url": "https://example.com/ml",
            "roles": ["ai_product_manager", "agent_engineer"],
            "topics": ["指标"],
            "text": "指标基线 " * 200,
        },
        {
            "source_id": "full-react",
            "version": "r1",
            "title": "React",
            "url": "https://example.com/react",
            "roles": ["fullstack"],
            "topics": ["前端"],
            "text": "组件状态 " * 200,
        },
    ]
    _seed_published(state, rows)
    embedding = FakeEmbedding(settings.embedding_dimension)
    store = FakeStore(settings.embedding_dimension)
    for row in rows:
        point = _point(row, vector=embedding._vector(row["text"]))
        store.points[knowledge_point_id(point)] = point
    db.knowledge_sources.find.side_effect = lambda query, projection=None: [
        r
        for r in state["sources"]
        if r["status"] == "published" and "ai_product_manager" in r["roles"]
    ]
    state["sources"][0]["status"] = "retracted"

    result = asyncio.run(
        query_references(
            settings, role="ai_product_manager", query="指标 基线", embedding=embedding, store=store
        )
    )
    ids = [item.source_id for item in result.items]
    assert "pm-pair" not in ids  # 撤回版本被阻断
    assert "full-react" not in ids  # 岗位不匹配被阻断
    assert ids == ["pm-metrics"]
    assert all(item.usage == "reference_only" for item in result.items)
    assert "不能证明用户具有某项经历" in result.notice


def test_query_no_hit_returns_empty_list(settings, fake_db):
    db, state = fake_db
    db.knowledge_sources.find.side_effect = lambda query, projection=None: []
    result = asyncio.run(
        query_references(
            settings,
            role="fullstack",
            query="任何主题",
            embedding=FakeEmbedding(settings.embedding_dimension),
            store=FakeStore(settings.embedding_dimension),
        )
    )
    assert result.items == []


def test_query_enforces_top_k_and_budget(settings, fake_db):
    db, state = fake_db
    rows = [
        {
            "source_id": f"src-{i}",
            "version": "r1",
            "title": f"来源{i}",
            "url": "https://example.com/x",
            "roles": ["agent_engineer"],
            "topics": ["t"],
            "text": f"内容{i} " * 120,
        }
        for i in range(6)
    ]
    _seed_published(state, rows)
    embedding = FakeEmbedding(settings.embedding_dimension)
    store = FakeStore(settings.embedding_dimension)
    for row in rows:
        point = _point(row, vector=embedding._vector(row["text"]))
        store.points[knowledge_point_id(point)] = point
    db.knowledge_sources.find.side_effect = lambda query, projection=None: list(state["sources"])
    result = asyncio.run(
        query_references(
            settings,
            role="agent_engineer",
            query="内容",
            top_k=2,
            max_chars=500,
            embedding=embedding,
            store=store,
        )
    )
    assert len(result.items) <= 2
    assert sum(len(item.summary) for item in result.items) <= 500


def test_list_published_sources_only_returns_published_and_role_filtered(settings, fake_db):
    db, state = fake_db
    state["sources"] = [
        {
            "source_id": "a",
            "version": "r1",
            "title": "A",
            "url": "https://example.com/a",
            "roles": ["fullstack"],
            "topics": ["x"],
            "verified_on": "2026-11-13",
            "content_kind": "editorial_summary",
            "usage": "reference_only",
            "status": "published",
        },
        {
            "source_id": "b",
            "version": "r1",
            "title": "B",
            "url": "https://example.com/b",
            "roles": ["ai_product_manager"],
            "topics": ["y"],
            "verified_on": "2026-11-13",
            "content_kind": "editorial_summary",
            "usage": "reference_only",
            "status": "retracted",
        },
    ]
    db.knowledge_sources.find.side_effect = lambda query, projection=None: [
        r for r in state["sources"] if r["status"] == "published" and "fullstack" in r["roles"]
    ]
    items = list_published_sources(settings, role="fullstack")
    assert [item.id for item in items] == ["a"]


def test_query_input_contract_validates_role_and_query():
    with pytest.raises(ValueError):
        KnowledgeQueryIn(role="intern", query="x")
    with pytest.raises(ValueError):
        KnowledgeQueryIn(role="fullstack", query="")
    payload = KnowledgeQueryIn(role="fullstack", query="React 状态", top_k=8)
    assert payload.top_k == 8


def test_query_filters_topics_and_source_version(settings, fake_db):
    db, state = fake_db
    rows = [
        {
            "source_id": "agent-patterns",
            "version": "r1",
            "title": "模式",
            "url": "https://example.com/p",
            "roles": ["agent_engineer"],
            "topics": ["工作流", "工具"],
            "text": "路由模式 " * 100,
        },
        {
            "source_id": "agent-persistence",
            "version": "r2",
            "title": "持久化",
            "url": "https://example.com/s",
            "roles": ["agent_engineer"],
            "topics": ["持久化", "恢复"],
            "text": "检查点恢复 " * 100,
        },
    ]
    _seed_published(state, rows)
    embedding = FakeEmbedding(settings.embedding_dimension)
    store = FakeStore(settings.embedding_dimension)
    for row in rows:
        point = _point(row, vector=embedding._vector(row["text"]))
        store.points[knowledge_point_id(point)] = point

    captured = {}

    def find_sources(query, projection=None):
        captured.update(query)
        rows = list(state["sources"])
        for key, value in query.items():
            if key == "status":
                rows = [r for r in rows if r["status"] == value]
            elif key == "roles":
                rows = [r for r in rows if value in r["roles"]]
            elif key == "topics":
                rows = [r for r in rows if set(r["topics"]) & set(value["$in"])]
            elif key == "version":
                rows = [r for r in rows if r["version"] == value]
        return rows

    db.knowledge_sources.find.side_effect = find_sources
    asyncio.run(
        query_references(
            settings,
            role="agent_engineer",
            query="检查点",
            topics=["持久化"],
            source_version="r2",
            embedding=embedding,
            store=store,
        )
    )
    assert captured["topics"] == {"$in": ["持久化"]}
    assert captured["version"] == "r2"

    # 版本过滤不满足时返回空列表。
    result = asyncio.run(
        query_references(
            settings,
            role="agent_engineer",
            query="检查点",
            source_version="r9",
            embedding=embedding,
            store=store,
        )
    )
    assert result.items == []


def test_knowledge_routes_are_authenticated_and_return_envelopes(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.dependencies import current_user
    from app.main import create_app
    from app.schemas.resources import UserResult

    client = TestClient(create_app(Settings(_env_file=None, app_env="test")))
    # 未登录一律 401。
    assert client.get("/api/v1/knowledge/sources", params={"role": "fullstack"}).status_code == 401
    assert (
        client.post("/api/v1/knowledge/query", json={"role": "fullstack", "query": "x"}).status_code
        == 401
    )

    client.app.dependency_overrides[current_user] = lambda: UserResult(
        id="u1", email="u@example.com", display_name="U"
    )
    monkeypatch.setattr(
        "app.api.routes.list_published_sources",
        lambda settings, *, role: list_published_sources_result(role),
    )

    async def fake_query(settings, *, role, query, top_k, max_chars, topics, source_version):
        from app.rag.knowledge import KnowledgeReference
        from app.schemas.knowledge import KnowledgeQueryResult

        return KnowledgeQueryResult(
            items=[
                KnowledgeReference(
                    source_id="full-react",
                    source_version="r1",
                    source_url="https://example.com/react",
                    title="React",
                    summary="组件状态",
                    roles=["fullstack"],
                )
            ]
        )

    monkeypatch.setattr("app.api.routes.query_references", fake_query)

    ok = client.get("/api/v1/knowledge/sources", params={"role": "fullstack"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["data"]["items"][0]["id"] == "full-react"
    assert body["request_id"]

    queried = client.post(
        "/api/v1/knowledge/query", json={"role": "fullstack", "query": "组件 状态"}
    )
    assert queried.status_code == 200
    items = queried.json()["data"]["items"]
    assert items[0]["usage"] == "reference_only"
    assert items[0]["source_version"] == "r1"

    bad = client.get("/api/v1/knowledge/sources", params={"role": "intern"})
    assert bad.status_code == 422


def list_published_sources_result(role):
    from app.schemas.knowledge import KnowledgeSourceResult

    return [
        KnowledgeSourceResult(
            id="full-react",
            title="React",
            url="https://example.com/react",
            roles=["fullstack"],
            topics=["前端"],
            version="r1",
            verified_on="2026-11-13",
            content_kind="editorial_summary",
            usage="reference_only",
        )
    ]
