"""公共知识版本化入库与查询服务（M3.2，issue #41）。

数据分层（dev/03、dev/16）：MongoDB 是来源清单、正文与 span 的权威副本；
Qdrant 独立公共集合只存向量。查询永远先按发布状态与岗位过滤 MongoDB 行，
向量召回只做候选排序——撤回/未发布的版本在 MongoDB 层被阻断，不依赖向量
库侧的过滤正确性。个人语料与用户隔离的既有边界完全不受影响。

发布流程：publishing → 向量写入 → published；中途失败的状态不被查询放行，
重跑同一 (source_id, version) 幂等跳过。撤回流程：先置 retracted（立即阻断
所有查询），再按 (source_id, source_version) 过滤物理删除向量点（指数退避
重试），最后标记 chunk 行已清理；清理持续失败时记录 purge_status=failed，
修复向量库后重跑本函数即可完成清理。
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.knowledge import KnowledgeReference, Role, load_catalog
from app.rag.knowledge_store import (
    KnowledgePoint,
    QdrantKnowledgeStore,
    get_knowledge_store,
    knowledge_point_id,
)
from app.rag.structured_chunking import ChunkPolicy, split_structured
from app.schemas.knowledge import KnowledgeQueryResult, KnowledgeSourceResult
from app.services.database import database_connection

# 撤回时向量物理清理的最大尝试次数与退避基数（秒）：第 n 次重试等待
# base * 2^(n-1)，与个人资料清理的语义一致（先阻断、后清理、可重跑）。
PURGE_MAX_ATTEMPTS = 5
PURGE_BACKOFF_BASE_SECONDS = 0.5

SOURCE_STATUS_PUBLISHED = "published"
SOURCE_STATUS_RETRACTED = "retracted"
SOURCE_STATUS_PUBLISHING = "publishing"


@dataclass
class PublishReport:
    """一次目录发布的统计：新增、已存在跳过与失败明细。"""

    published: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_indexes(db) -> None:
    """幂等建立公共知识集合索引；服务层每个入口先调用。"""
    db.knowledge_sources.create_index(
        [("source_id", 1), ("version", 1)], unique=True, name="knowledge_source_version"
    )
    db.knowledge_sources.create_index([("status", 1)], name="knowledge_source_status")
    db.knowledge_chunks.create_index(
        [("source_id", 1), ("source_version", 1), ("chunk_id", 1)],
        unique=True,
        name="knowledge_chunk_identity",
    )
    db.knowledge_chunks.create_index(
        [("source_id", 1), ("source_version", 1)], name="knowledge_chunk_source"
    )


def _index_version(chunking_config_version: str, embedding_model: str) -> str:
    return f"{chunking_config_version}|emb:{embedding_model}"


def _chunks_to_rows(
    source: dict, chunks, *, embedding_model: str
) -> tuple[dict, list[dict], list[KnowledgePoint]]:
    """把结构化切分结果转成 Mongo 行与向量点；行与点一一对应。"""
    index_version = _index_version(chunks[0].policy_version, embedding_model)
    source_row = {
        "source_id": source["id"],
        "version": source["version"],
        "title": source["title"],
        "url": source["url"],
        "roles": list(source["roles"]),
        "topics": list(source["topics"]),
        "verified_on": source["verified_on"].isoformat(),
        "content_kind": source["content_kind"],
        "usage": source["usage"],
        "content_sha256": source["sha256"],
        "chunking_config_version": chunks[0].policy_version,
        "embedding_model": embedding_model,
        "index_version": index_version,
        "status": SOURCE_STATUS_PUBLISHING,
        "created_at": _utcnow(),
    }
    chunk_rows = []
    for chunk in chunks:
        chunk_rows.append(
            {
                "source_id": source["id"],
                "source_version": source["version"],
                "chunk_id": chunk.id,
                "text": chunk.text,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
                "heading_path": list(chunk.heading_path),
                "index_version": index_version,
                "point_id": knowledge_point_id(
                    KnowledgePoint(
                        chunk_id=chunk.id,
                        source_id=source["id"],
                        source_version=source["version"],
                        index_version=index_version,
                        vector=[0.0],
                    )
                ),
                "deleted": False,
            }
        )
    return source_row, chunk_rows, index_version


async def publish_catalog(
    settings: Settings,
    *,
    root: Path | None = None,
    catalog_root: Path | None = None,
    embedding: EmbeddingProvider | None = None,
    store: QdrantKnowledgeStore | None = None,
    batch_size: int | None = None,
) -> PublishReport:
    """把 knowledge/ 目录的审核目录发布为可检索的公共知识版本。

    对已存在的 (source_id, version) 幂等跳过；整批处理，单个来源失败不阻塞
    其余来源，失败明细写入报告。``catalog_root`` 为 knowledge/ 目录路径。
    """
    root = Path(catalog_root) if catalog_root else Path(__file__).resolve().parents[3] / "knowledge"
    report = PublishReport()
    provider = embedding or get_embedding_provider(settings)
    vector_store = store or get_knowledge_store(settings)
    catalog = load_catalog(root)
    batch = batch_size or settings.embedding_batch_size

    with database_connection(settings) as db:
        _ensure_indexes(db)
        await vector_store.ensure_collection()
        for source, text in catalog:
            key = f"{source.id}@{source.version}"
            existing = db.knowledge_sources.find_one(
                {"source_id": source.id, "version": source.version}
            )
            if existing is not None:
                # 任何已落库的版本（published/retracted/publishing）都不重复发布；
                # 内容修订必须递增版本号（dev/17 来源治理）。
                report.skipped.append(key)
                continue
            try:
                chunks = split_structured(
                    text,
                    source_id=source.id,
                    source_version=source.version,
                    policy=ChunkPolicy(),
                )
                if not chunks:
                    raise ValueError("来源切分后没有任何 chunk")
                vectors: list[list[float]] = []
                for start in range(0, len(chunks), batch):
                    vectors.extend(
                        await provider.embed_texts(
                            [c.embedding_text for c in chunks[start : start + batch]]
                        )
                    )
                source_row, chunk_rows, index_version = _chunks_to_rows(
                    source.model_dump(), chunks, embedding_model=provider.model_version()
                )
                db.knowledge_sources.insert_one(source_row)
                db.knowledge_chunks.insert_many(chunk_rows)
                try:
                    await vector_store.upsert_chunks(
                        [
                            KnowledgePoint(
                                chunk_id=row["chunk_id"],
                                source_id=row["source_id"],
                                source_version=row["source_version"],
                                index_version=index_version,
                                vector=vector,
                            )
                            for row, vector in zip(chunk_rows, vectors, strict=True)
                        ]
                    )
                except Exception:
                    # 向量写入失败：回滚 Mongo 行，保持"库中无半成品版本"，
                    # 修复向量库后重跑本函数即可。
                    db.knowledge_chunks.delete_many(
                        {"source_id": source.id, "source_version": source.version}
                    )
                    db.knowledge_sources.delete_one(
                        {"source_id": source.id, "version": source.version}
                    )
                    raise
                db.knowledge_sources.update_one(
                    {"source_id": source.id, "version": source.version},
                    {"$set": {"status": SOURCE_STATUS_PUBLISHED, "published_at": _utcnow()}},
                )
                report.published.append(key)
            except Exception as exc:  # noqa: BLE001 - 失败明细入报告，不中断整批
                report.failed[key] = f"{type(exc).__name__}: {exc}"
    return report


def list_published_sources(settings: Settings, *, role: Role) -> list[KnowledgeSourceResult]:
    """列出已发布的公共知识来源（按岗位过滤）；撤回与未发布版本不可见。"""
    with database_connection(settings) as db:
        _ensure_indexes(db)
        rows = db.knowledge_sources.find(
            {"status": SOURCE_STATUS_PUBLISHED, "roles": role}, {"_id": 0}
        )
        return [
            KnowledgeSourceResult(
                id=row["source_id"],
                title=row["title"],
                url=row["url"],
                roles=row["roles"],
                topics=row["topics"],
                version=row["version"],
                verified_on=row["verified_on"],
                content_kind=row["content_kind"],
                usage=row["usage"],
            )
            for row in sorted(rows, key=lambda item: item["source_id"])
        ]


async def retract_source(
    settings: Settings,
    *,
    source_id: str,
    source_version: str,
    store: QdrantKnowledgeStore | None = None,
) -> None:
    """撤回一个已发布版本：先阻断查询，再物理清理向量，最后标记 chunk 行。

    全程幂等：重复调用对已撤回版本直接完成清理收尾。向量清理持续失败时
    记录 purge_status=failed 并抛出，修复后重跑本函数继续清理。
    """
    vector_store = store or get_knowledge_store(settings)
    with database_connection(settings) as db:
        _ensure_indexes(db)
        row = db.knowledge_sources.find_one({"source_id": source_id, "version": source_version})
        if row is None:
            raise ValueError(f"未知知识来源版本: {source_id}@{source_version}")
        # 第一步：立即阻断查询（含本函数之前的失败重跑）。
        db.knowledge_sources.update_one(
            {"source_id": source_id, "version": source_version},
            {
                "$set": {
                    "status": SOURCE_STATUS_RETRACTED,
                    "retracted_at": _utcnow(),
                    "purge_status": "purging",
                }
            },
        )
        # 第二步：物理清理向量点，指数退避重试。
        last_error: Exception | None = None
        for attempt in range(PURGE_MAX_ATTEMPTS):
            try:
                await vector_store.delete_source_points(
                    source_id=source_id, source_version=source_version
                )
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - 记录最后一次错误用于报告
                last_error = exc
                if attempt < PURGE_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(PURGE_BACKOFF_BASE_SECONDS * (2**attempt))
        if last_error is not None:
            db.knowledge_sources.update_one(
                {"source_id": source_id, "version": source_version},
                {"$set": {"purge_status": "failed"}},
            )
            raise RuntimeError(
                f"向量清理失败（{source_id}@{source_version}）：{last_error}"
            ) from last_error
        # 第三步：标记 chunk 行已清理。
        db.knowledge_chunks.update_many(
            {"source_id": source_id, "source_version": source_version},
            {"$set": {"deleted": True}},
        )
        db.knowledge_sources.update_one(
            {"source_id": source_id, "version": source_version},
            {"$set": {"purge_status": "purged"}},
        )


async def query_references(
    settings: Settings,
    *,
    role: Role,
    query: str,
    top_k: int = 4,
    max_chars: int = 6000,
    embedding: EmbeddingProvider | None = None,
    store: QdrantKnowledgeStore | None = None,
) -> KnowledgeQueryResult:
    """公共知识引用查询：向量召回只做候选，发布状态与岗位在 MongoDB 层复核。

    命中按 (source_id, source_version) 精确回到 MongoDB 行：撤回/未发布/
    岗位不匹配/已删除的候选一律丢弃；无命中返回空列表，绝不编造引用。
    返回引用复用 KnowledgeReference 契约（reference_only），供 D 侧
    knowledge_policy 的公共通道直接消费。
    """
    if not query.strip():
        raise ValueError("查询文本不能为空")
    if not 1 <= top_k <= 8 or not 500 <= max_chars <= 16000:
        raise ValueError("top_k 必须在 1..8，max_chars 必须在 500..16000")
    provider = embedding or get_embedding_provider(settings)
    vector_store = store or get_knowledge_store(settings)

    with database_connection(settings) as db:
        _ensure_indexes(db)
        published = {
            (row["source_id"], row["version"]): row
            for row in db.knowledge_sources.find(
                {"status": SOURCE_STATUS_PUBLISHED, "roles": role}, {"_id": 0}
            )
        }
        if not published:
            return KnowledgeQueryResult(items=[])
        # 多取 3 倍候选供状态/岗位过滤后填充，过滤是 MongoDB 强约束而非提示。
        vectors = await provider.embed_texts([query])
        hits = await vector_store.search(vector=vectors[0], top_k=min(top_k * 3, 60))
        items: list[KnowledgeReference] = []
        used = 0
        for hit in hits:
            source = published.get((hit.source_id, hit.source_version))
            if source is None:
                continue
            chunk = db.knowledge_chunks.find_one(
                {
                    "source_id": hit.source_id,
                    "source_version": hit.source_version,
                    "chunk_id": hit.chunk_id,
                    "deleted": {"$ne": True},
                },
                {"_id": 0, "text": 1},
            )
            if chunk is None:
                continue
            text = chunk["text"]
            if len(items) >= top_k or used + len(text) > max_chars:
                continue
            items.append(
                KnowledgeReference(
                    source_id=source["source_id"],
                    source_version=source["version"],
                    source_url=source["url"],
                    title=source["title"],
                    summary=text,
                    roles=source["roles"],
                )
            )
            used += len(text)
        return KnowledgeQueryResult(items=items)
