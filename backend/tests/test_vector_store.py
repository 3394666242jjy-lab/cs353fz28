"""vector_store 适配器单元测试（成员 C，issue #8）。

全部用例经 httpx.MockTransport 本地运行，不触达真实 Qdrant；
覆盖集合与索引创建、幂等自愈、维度守卫、upsert/替换结构、检索过滤与用户隔离。
"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rag.vector_store import (
    ChunkPoint,
    QdrantVectorStore,
    VectorStoreError,
    get_vector_store,
    point_id,
)
from app.schemas.analysis import DocumentVersion

DIMENSION = 64
API_KEY = "qdrant-unit-key"


def settings(**overrides) -> Settings:
    """构造测试配置：_env_file=None 隔离本地 .env，可按项覆盖。"""
    values = {
        "vector_store_url": "https://qdrant.test",
        "vector_store_api_key": API_KEY,
        "vector_store_collection": "careerpilot_chunks",
        "vector_store_timeout_seconds": 1.0,
        "vector_store_max_retries": 2,
        "embedding_dimension": DIMENSION,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def store(handler, **overrides) -> QdrantVectorStore:
    """用 MockTransport 构造向量库适配器，handler 决定每个请求的响应。"""
    return QdrantVectorStore(settings(**overrides), transport=httpx.MockTransport(handler))


def point(**overrides) -> ChunkPoint:
    """构造测试用 chunk 点，可按项覆盖字段。"""
    values = {
        "chunk_id": "chunk_demo",
        "document_id": "doc_demo",
        "version": 1,
        "corpus": "personal",
        "user_id": "user_demo",
        "index_version": "idx-v1",
        "vector": [0.1] * DIMENSION,
    }
    values.update(overrides)
    return ChunkPoint(**values)


def collection_config(size: int = DIMENSION) -> httpx.Response:
    """构造"集合已存在"的 GET 响应，size 可模拟不同维度的历史集合。"""
    return httpx.Response(
        200,
        json={
            "result": {
                "config": {"params": {"vectors": {"dense": {"size": size, "distance": "Cosine"}}}}
            }
        },
    )


def ok() -> httpx.Response:
    """通用成功响应。"""
    return httpx.Response(200, json={"result": {"status": "ok"}})


def test_ensure_collection_creates_named_vectors_and_payload_indexes():
    """集合不存在时：创建具名稠密向量集合，并建立 03 文档要求的全部 payload 索引。"""
    seen: list[tuple[str, str, dict]] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content or "{}")))
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                404, json={"status": {"error": "Collection `careerpilot_chunks` not found!"}}
            )
        return ok()

    asyncio.run(store(handler).ensure_collection())

    puts = [entry for entry in seen if entry[0] == "PUT"]
    assert (
        "PUT",
        "/collections/careerpilot_chunks",
        {"vectors": {"dense": {"size": 64, "distance": "Cosine"}}},
    ) in puts
    indexed = {
        body["field_name"]: body["field_schema"]
        for _, path, body in puts
        if path.endswith("/index")
    }
    assert indexed == {
        "corpus": "keyword",
        "user_id": "keyword",
        "document_id": "keyword",
        "version": "integer",
        "chunk_id": "keyword",
        "index_version": "keyword",
        "deleted": "bool",
    }
    assert all(request.headers["api-key"] == API_KEY for request in requests)


def test_ensure_collection_skips_recreation_but_reasserts_indexes():
    """集合已存在且维度一致：不重建集合，但仍重新断言索引（半失败状态可自愈）。"""
    created: list[str] = []
    indexed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == "/collections/careerpilot_chunks":
            created.append(request.url.path)
        if request.method == "PUT" and request.url.path.endswith("/index"):
            indexed.append(json.loads(request.content)["field_name"])
        return collection_config()

    asyncio.run(store(handler).ensure_collection())

    assert created == []
    assert len(indexed) == 7


def test_ensure_collection_rejects_embedding_dimension_drift():
    """已存在集合维度与配置不一致时拒绝继续，按文档要求走重建迁移。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return collection_config(size=768)

    with pytest.raises(VectorStoreError) as excinfo:
        asyncio.run(store(handler).ensure_collection())

    assert "重建索引" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_local_store_without_key_omits_api_key_header():
    """本地 compose Qdrant（无鉴权）：Key 留空时请求不带 api-key 头。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return collection_config()
        return ok()

    asyncio.run(store(handler, vector_store_api_key=None).ensure_collection())

    assert requests
    assert all("api-key" not in request.headers for request in requests)


def test_point_ids_are_deterministic_and_owner_scoped():
    """point id 确定性派生：同输入同 id，换用户/版本即不同（幂等且隔离）。"""
    first = point()
    again = point()
    other_owner = point(user_id="user_other")
    other_version = point(version=2)
    assert point_id(first) == point_id(again)
    assert point_id(first) != point_id(other_owner)
    assert point_id(first) != point_id(other_version)


def test_upsert_sends_deterministic_ids_named_vector_and_documented_payload():
    """upsert 请求体包含确定性 id、具名 dense 向量与 03 文档规定的 payload 字段。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or "{}"))
        return ok()

    asyncio.run(store(handler).upsert_chunks([point(), point(chunk_id="chunk_demo_2")]))

    assert len(bodies) == 1
    entries = bodies[0]["points"]
    assert [entry["id"] for entry in entries] == [
        point_id(point()),
        point_id(point(chunk_id="chunk_demo_2")),
    ]
    assert entries[0]["vector"] == {"dense": [0.1] * DIMENSION}
    assert set(entries[0]["payload"]) == {
        "corpus",
        "user_id",
        "document_id",
        "version",
        "chunk_id",
        "index_version",
        "deleted",
    }
    assert entries[0]["payload"]["deleted"] is False


def test_upsert_with_no_points_does_not_call_the_store():
    """空列表不发请求。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"store must not be called: {request.url}")

    assert asyncio.run(store(handler).upsert_chunks([])) is None


def test_upsert_rejects_wrong_dimension_before_sending():
    """维度不符在发送前拒绝。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"store must not be called: {request.url}")

    with pytest.raises(VectorStoreError) as excinfo:
        asyncio.run(store(handler).upsert_chunks([point(vector=[0.1] * 32)]))

    assert "32" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_search_always_filters_user_and_deleted_and_maps_hits():
    """检索请求内置 user_id 与 deleted=false 过滤，响应正确映射为 SearchHit。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or "{}"))
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "id": "1",
                        "score": 0.91,
                        "payload": {
                            "chunk_id": "chunk_demo",
                            "document_id": "doc_demo",
                            "version": 1,
                            "corpus": "personal",
                        },
                    }
                ]
            },
        )

    hits = asyncio.run(
        store(handler).search(user_id="user_demo", vector=[0.2] * DIMENSION, top_k=3)
    )

    assert len(bodies) == 1
    body = bodies[0]
    assert body["vector"] == {"name": "dense", "vector": [0.2] * DIMENSION}
    assert body["limit"] == 3
    assert body["with_payload"] is True
    conditions = {json.dumps(item, sort_keys=True) for item in body["filter"]["must"]}
    assert (
        json.dumps({"key": "user_id", "match": {"value": "user_demo"}}, sort_keys=True)
        in conditions
    )
    assert json.dumps({"key": "deleted", "match": {"value": False}}, sort_keys=True) in conditions
    assert [
        (hit.chunk_id, hit.document_id, hit.version, hit.corpus, hit.score) for hit in hits
    ] == [("chunk_demo", "doc_demo", 1, "personal", 0.91)]


def test_search_limits_to_requested_document_versions():
    """指定文档版本时，过滤器用 should-of-must 精确限定 (document_id, version) 对。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or "{}"))
        return httpx.Response(200, json={"result": []})

    asyncio.run(
        store(handler).search(
            user_id="user_demo",
            vector=[0.2] * DIMENSION,
            document_versions=[
                DocumentVersion(document_id="doc_a", version=1),
                DocumentVersion(document_id="doc_b", version=3),
            ],
        )
    )

    should = bodies[0]["filter"]["must"][-1]["should"]
    assert should == [
        {
            "must": [
                {"key": "document_id", "match": {"value": "doc_a"}},
                {"key": "version", "match": {"value": 1}},
            ]
        },
        {
            "must": [
                {"key": "document_id", "match": {"value": "doc_b"}},
                {"key": "version", "match": {"value": 3}},
            ]
        },
    ]


def test_search_on_missing_collection_names_the_setup_step():
    """集合不存在时给出"先执行 ensure_collection"的明确提示。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": {"error": "Collection not found"}})

    with pytest.raises(VectorStoreError) as excinfo:
        asyncio.run(store(handler).search(user_id="user_demo", vector=[0.2] * DIMENSION))

    assert "请先执行 ensure_collection" in str(excinfo.value)


def test_search_rejects_wrong_dimension():
    """查询向量维度不符在发送前拒绝。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"store must not be called: {request.url}")

    with pytest.raises(VectorStoreError) as excinfo:
        asyncio.run(store(handler).search(user_id="user_demo", vector=[0.2] * 32))

    assert "32" in str(excinfo.value)


def test_rate_limited_upsert_is_retried_then_succeeds(monkeypatch):
    """429 限流按配置退避重试一次后成功。"""
    import app.rag.vector_store as module

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, json={"status": {"error": "rate limited"}})
        return ok()

    asyncio.run(store(handler).upsert_chunks([point()]))

    assert state["calls"] == 2
    assert delays == [0.5]


def test_personal_chunks_must_document_their_owner():
    """personal 片段必须带 user_id，否则构造即拒绝。"""
    with pytest.raises(ValidationError):
        point(user_id=None)
    assert point(user_id="user_demo").user_id == "user_demo"


def test_public_chunks_carry_no_owner():
    """public 片段允许无属主，payload 中 user_id 为 null。"""
    entry = point(corpus="public", user_id=None)
    assert entry.payload()["corpus"] == "public"
    assert entry.payload()["user_id"] is None


def test_factory_builds_store_from_explicit_settings():
    """工厂按传入配置构建适配器，集合名可读。"""
    adapter = get_vector_store(settings(vector_store_collection="other_chunks"))
    assert isinstance(adapter, QdrantVectorStore)
    assert adapter.collection_name == "other_chunks"


def test_vector_store_settings_reject_out_of_range_values():
    """超时与重试次数的配置边界被 pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        settings(vector_store_max_retries=9)
    with pytest.raises(ValidationError):
        settings(vector_store_timeout_seconds=120.0)


def test_new_version_checks_for_existing_points_then_upserts():
    """已发布版本不可原地覆盖，新版本先检查再写入。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/scroll"):
            return httpx.Response(200, json={"result": {"points": []}})
        return ok()

    asyncio.run(
        store(handler).replace_document_points(
            user_id="user_demo", document_id="doc_demo", version=1, points=[point()]
        )
    )

    assert calls == [
        ("POST", "/collections/careerpilot_chunks/points/scroll"),
        ("PUT", "/collections/careerpilot_chunks/points"),
    ]


def test_empty_points_cannot_delete_a_version():
    """空点列表不能删除已有版本。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return ok()

    with pytest.raises(VectorStoreError):
        asyncio.run(
            store(handler).replace_document_points(
                user_id="user_demo", document_id="doc_demo", version=1, points=[]
            )
        )
    assert calls == []


def test_existing_version_is_never_deleted_or_overwritten():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"result": {"points": [{"id": "old"}]}})

    with pytest.raises(VectorStoreError, match="不能原地重建"):
        asyncio.run(
            store(handler).replace_document_points(
                user_id="user_demo", document_id="doc_demo", version=1, points=[point()]
            )
        )
    assert calls == [("POST", "/collections/careerpilot_chunks/points/scroll")]


def test_empty_document_version_filter_never_searches_all_documents():
    def handler(request):
        raise AssertionError("empty version filter must not call Qdrant")

    result = asyncio.run(
        store(handler).search(
            user_id="user_demo",
            vector=[0.1] * DIMENSION,
            document_versions=[],
        )
    )
    assert result == []


def test_replace_rejects_points_from_another_version_before_any_request():
    """点不属于目标版本时拒绝，且删除尚未发生。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"store must not be called: {request.url}")

    with pytest.raises(VectorStoreError) as excinfo:
        asyncio.run(
            store(handler).replace_document_points(
                user_id="user_demo",
                document_id="doc_demo",
                version=1,
                points=[point(version=2)],
            )
        )

    assert "不属于" in str(excinfo.value)


def test_replace_rejects_wrong_dimension_before_deleting():
    """维度不符在删除前拒绝，坏批次不会把好索引清空。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"store must not be called: {request.url}")

    with pytest.raises(VectorStoreError) as excinfo:
        asyncio.run(
            store(handler).replace_document_points(
                user_id="user_demo",
                document_id="doc_demo",
                version=1,
                points=[point(vector=[0.1] * 32)],
            )
        )

    assert "32" in str(excinfo.value)
