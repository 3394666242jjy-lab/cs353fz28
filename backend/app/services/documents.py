"""Owner-scoped MongoDB persistence for documents, versions and chunks.

Implements the M1 collections defined in dev/03: documents, document_versions
and chunks. Version metadata and chunk records are written here from the RAG
indexer result (see issue #8); this module is the authority that C's pipeline
persists into and that D's evidence assembly reads from.
"""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import Field
from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.schemas.analysis import PdfLocator, TextLocator
from app.schemas.common import ContractModel
from app.schemas.resources import DocumentResult
from app.services.database import database_connection


@runtime_checkable
class IndexedChunk(Protocol):
    """Chunk record produced by the RAG indexer (duck-typed, see issue #8)."""

    chunk_id: str
    text: str
    locator: TextLocator | PdfLocator
    vector_id: str


class IndexedVersionPayload(ContractModel):
    """Version metadata returned by the RAG indexer for B to persist."""

    version: int = Field(ge=1)
    extracted_text_key: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    chunking_config_version: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)
    embedding_dimension: int = Field(gt=0)
    index_version: str = Field(min_length=1)


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M1 indexes per dev/03; safe to repeat on every call."""
    database.documents.create_index("id", unique=True)
    database.documents.create_index([("user_id", 1), ("deleted_at", 1), ("created_at", -1)])
    database.document_versions.create_index("id", unique=True)
    database.document_versions.create_index([("document_id", 1), ("version", 1)], unique=True)
    database.chunks.create_index("id", unique=True)
    database.chunks.create_index(
        [("corpus", 1), ("user_id", 1), ("document_id", 1), ("version", 1)]
    )


def create_document(
    settings: Settings,
    user_id: str,
    *,
    kind: str,
    filename: str,
    media_type: str,
    size_bytes: int,
    storage_key: str,
) -> str:
    """Create a document pinned at version 1 in status uploaded.

    The version row is written first so the document never points at a missing
    version when a failure interrupts the two writes.
    """
    document_id = f"doc_{uuid4().hex}"
    version_id = f"dver_{uuid4().hex}"
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        database.document_versions.insert_one(
            {
                "id": version_id,
                "user_id": user_id,
                "document_id": document_id,
                "version": 1,
                "source_key": storage_key,
                "extracted_text_key": None,
                "content_hash": None,
                "parser_version": None,
                "chunking_config_version": None,
                "embedding_model": None,
                "embedding_dimension": None,
                "index_version": None,
                "status": "uploaded",
                "created_at": now,
                "updated_at": now,
            }
        )
        database.documents.insert_one(
            {
                "id": document_id,
                "user_id": user_id,
                "kind": kind,
                "filename": filename,
                "storage_key": storage_key,
                "media_type": media_type,
                "size_bytes": size_bytes,
                "current_version": 1,
                "status": "uploaded",
                "deleted_at": None,
                "purge_status": "active",
                "created_at": now,
                "updated_at": now,
            }
        )
    return document_id


def _to_result(document: dict[str, Any]) -> DocumentResult:
    return DocumentResult(
        id=document["id"],
        kind=document["kind"],
        filename=document["filename"],
        current_version=document["current_version"],
        status=document["status"],
        error=document.get("error"),
    )


def list_documents(
    settings: Settings, user_id: str, *, cursor: str | None, limit: int
) -> tuple[list[DocumentResult], str | None]:
    """Owner-scoped keyset pagination ordered by newest first."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        query: dict[str, Any] = {"user_id": user_id, "deleted_at": None}
        if cursor is not None:
            anchor = database.documents.find_one(
                {"id": cursor, "user_id": user_id, "deleted_at": None},
                {"_id": 0, "created_at": 1, "id": 1},
            )
            if anchor is not None:
                query["$or"] = [
                    {"created_at": {"$lt": anchor["created_at"]}},
                    {"created_at": anchor["created_at"], "id": {"$lt": anchor["id"]}},
                ]
        cursor_page = (
            database.documents.find(query, {"_id": 0})
            .sort([("created_at", -1), ("id", -1)])
            .limit(limit + 1)
        )
        rows = list(cursor_page)
    items = [_to_result(row) for row in rows[:limit]]
    next_cursor = rows[limit - 1]["id"] if len(rows) > limit and items else None
    return items, next_cursor


def get_document(settings: Settings, user_id: str, document_id: str) -> DocumentResult | None:
    """Return one live document owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        document = database.documents.find_one(
            {"id": document_id, "user_id": user_id, "deleted_at": None}, {"_id": 0}
        )
    if document is None:
        return None
    return _to_result(document)


def set_document_status(
    settings: Settings,
    user_id: str,
    document_id: str,
    *,
    status: str,
    error: str | None = None,
) -> bool:
    """Transition status with owner check; returns False when not found."""
    update: dict[str, Any] = {"status": status, "error": error, "updated_at": datetime.now(UTC)}
    with database_connection(settings) as database:
        _ensure_indexes(database)
        result = database.documents.update_one(
            {"id": document_id, "user_id": user_id, "deleted_at": None}, {"$set": update}
        )
    return result.matched_count == 1


def save_indexed_version(
    settings: Settings,
    user_id: str,
    document_id: str,
    payload: IndexedVersionPayload,
    chunks: Iterable[IndexedChunk],
) -> None:
    """Publish a new immutable version only after all of its chunks are saved.

    A published version can be referenced by existing analyses and must never
    be rebuilt in place. A failed, unpublished version may be retried.
    """
    payload = IndexedVersionPayload.model_validate(payload)
    chunks = list(chunks)
    if not chunks:
        raise ValueError("Cannot publish a document version without text chunks")
    now = datetime.now(UTC)
    version = payload.version
    with database_connection(settings) as database:
        _ensure_indexes(database)
        document = database.documents.find_one(
            {"id": document_id, "user_id": user_id, "deleted_at": None}
        )
        if document is None:
            raise ValueError("Document does not exist or is not accessible")
        if document.get("status") not in {"uploaded", "ready"}:
            raise ValueError("Document is no longer eligible for indexing")
        # The unique (document_id, version) index makes this an atomic claim:
        # a published or already-writing version cannot be claimed again.
        database.document_versions.update_one(
            {
                "document_id": document_id,
                "version": version,
                "user_id": user_id,
                "status": {"$in": ["uploaded", "failed"]},
            },
            {
                "$setOnInsert": {"id": f"dver_{uuid4().hex}", "created_at": now},
                "$set": {"status": "writing", "updated_at": now},
            },
            upsert=True,
        )
        try:
            database.chunks.delete_many(
                {
                    "user_id": user_id,
                    "corpus": "personal",
                    "document_id": document_id,
                    "version": version,
                }
            )
            database.chunks.insert_many(
                [
                    {
                        "id": chunk.chunk_id,
                        "user_id": user_id,
                        "corpus": "personal",
                        "document_id": document_id,
                        "version": version,
                        "text": chunk.text,
                        "locator": chunk.locator.model_dump(),
                        "vector_id": chunk.vector_id,
                        "source_url": None,
                        "deleted_at": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for chunk in chunks
                ]
            )
        except Exception:
            database.document_versions.update_one(
                {
                    "document_id": document_id,
                    "version": version,
                    "user_id": user_id,
                    "status": "writing",
                },
                {"$set": {"status": "failed", "updated_at": datetime.now(UTC)}},
            )
            raise
        published = database.document_versions.update_one(
            {
                "document_id": document_id,
                "version": version,
                "user_id": user_id,
                "status": "writing",
            },
            {
                "$set": {
                    "extracted_text_key": payload.extracted_text_key,
                    "content_hash": payload.content_hash,
                    "parser_version": payload.parser_version,
                    "chunking_config_version": payload.chunking_config_version,
                    "embedding_model": payload.embedding_model,
                    "embedding_dimension": payload.embedding_dimension,
                    "index_version": payload.index_version,
                    "status": "ready",
                    "updated_at": now,
                }
            },
        )
        if published.matched_count != 1:
            raise RuntimeError("Document version changed while it was being written")
        current = database.documents.update_one(
            {
                "id": document_id,
                "user_id": user_id,
                "deleted_at": None,
                "status": {"$in": ["uploaded", "ready"]},
            },
            {
                "$set": {
                    "current_version": version,
                    "status": "ready",
                    "error": None,
                    "updated_at": now,
                }
            },
        )
        if current.matched_count != 1:
            raise RuntimeError("Document was deleted while its version was being written")


def get_chunk(
    settings: Settings, user_id: str, chunk_id: str, version: int
) -> dict[str, Any] | None:
    """Return one live chunk owned by the user at an exact version, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.chunks.find_one(
            {"id": chunk_id, "user_id": user_id, "version": version, "deleted_at": None},
            {"_id": 0},
        )
    return row


def list_chunks_by_ids(
    settings: Settings, user_id: str, chunk_ids: list[str]
) -> list[dict[str, Any]]:
    """Owner-scoped batch lookup of chunk text/locator for evidence assembly."""
    if not chunk_ids:
        return []
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.chunks.find(
                {"id": {"$in": chunk_ids}, "user_id": user_id, "deleted_at": None}, {"_id": 0}
            )
        )
    return rows


def soft_delete_document(
    settings: Settings, user_id: str, document_id: str
) -> dict[str, Any] | None:
    """Mark a document as deleted so it is immediately invisible to queries.

    Returns the deleted document row (including storage_key and version info)
    so the caller can enqueue an asynchronous purge of chunks, vectors and
    files. Idempotent: deleting an already-deleted document returns None.
    """
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.documents.find_one_and_update(
            {
                "id": document_id,
                "user_id": user_id,
                "deleted_at": None,
                "status": {"$ne": "deleted"},
            },
            {
                "$set": {
                    "status": "deleted",
                    "deleted_at": now,
                    "purge_status": "pending",
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
    if row is None:
        return None
    return row


def list_document_versions(
    settings: Settings, user_id: str, document_id: str
) -> list[dict[str, Any]]:
    """Return all version rows for a document (including deleted ones)."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.document_versions.find(
                {"document_id": document_id, "user_id": user_id},
                {"_id": 0},
            ).sort("version", 1)
        )
    return rows


def mark_chunks_deleted(
    settings: Settings, user_id: str, document_id: str, version: int
) -> list[str]:
    """Soft-delete chunks of a specific version; returns their vector_ids for removal."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.chunks.find(
                {
                    "document_id": document_id,
                    "version": version,
                    "user_id": user_id,
                    "deleted_at": None,
                },
                {"_id": 0, "vector_id": 1},
            )
        )
        vector_ids = [row["vector_id"] for row in rows if row.get("vector_id")]
        database.chunks.update_many(
            {
                "document_id": document_id,
                "version": version,
                "user_id": user_id,
                "deleted_at": None,
            },
            {"$set": {"deleted_at": datetime.now(UTC)}},
        )
    return vector_ids


def set_purge_status(settings: Settings, user_id: str, document_id: str, *, status: str) -> None:
    """Record purge outcome so failed cleanup can be retried."""
    with database_connection(settings) as database:
        database.documents.update_one(
            {"id": document_id, "user_id": user_id},
            {"$set": {"purge_status": status, "updated_at": datetime.now(UTC)}},
        )


def create_text_version(
    settings: Settings,
    user_id: str,
    document_id: str,
    *,
    expected_version: int,
    source_key: str,
) -> int | None:
    """Open the next version for an edited text, guarded by expected_version.

    Returns the new version number, or None when the document is missing,
    deleted, not ready or the expected version is stale (409 VERSION_CONFLICT).
    The document stays on the published version until the new one is fully
    indexed (dev/03 lifecycle #1).
    """
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        document = database.documents.find_one(
            {
                "id": document_id,
                "user_id": user_id,
                "deleted_at": None,
                "status": "ready",
                "current_version": expected_version,
            },
            {"_id": 0, "current_version": 1},
        )
        if document is None:
            return None
        new_version = document["current_version"] + 1
        database.document_versions.insert_one(
            {
                "id": f"dver_{uuid4().hex}",
                "user_id": user_id,
                "document_id": document_id,
                "version": new_version,
                "source_key": source_key,
                "extracted_text_key": None,
                "content_hash": None,
                "parser_version": None,
                "chunking_config_version": None,
                "embedding_model": None,
                "embedding_dimension": None,
                "index_version": None,
                "status": "uploaded",
                "created_at": now,
                "updated_at": now,
            }
        )
    return new_version


def get_version_row(
    settings: Settings, user_id: str, document_id: str, version: int
) -> dict[str, Any] | None:
    """Return one version row owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.document_versions.find_one(
            {"document_id": document_id, "user_id": user_id, "version": version}, {"_id": 0}
        )
    return row


def set_version_failed(
    settings: Settings,
    user_id: str,
    document_id: str,
    version: int,
    *,
    error: str,
) -> bool:
    """Mark an unpublished version failed; published versions are immutable."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        result = database.document_versions.update_one(
            {
                "document_id": document_id,
                "user_id": user_id,
                "version": version,
                "status": {"$in": ["uploaded", "writing"]},
            },
            {"$set": {"status": "failed", "error": error, "updated_at": datetime.now(UTC)}},
        )
    return result.matched_count == 1


def find_active_index_execution(
    settings: Settings, user_id: str, document_id: str, version: int
) -> str | None:
    """Return the queued/running index execution for one version, if any.

    POST /documents/{id}/reindex reuses it instead of stacking duplicate
    indexing work on the same version (dev/04).
    """
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.executions.find_one(
            {
                "user_id": user_id,
                "type": "index",
                "resource_id": document_id,
                "resource_version": version,
                "status": {"$in": ["queued", "running"]},
            },
            {"_id": 0, "id": 1},
        )
    return row["id"] if row is not None else None


def get_deleted_document(
    settings: Settings, user_id: str, document_id: str
) -> dict[str, Any] | None:
    """Purge-side lookup: the deleted document row including storage keys."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.documents.find_one(
            {"id": document_id, "user_id": user_id, "status": "deleted"}, {"_id": 0}
        )
    return row


def list_purge_candidates(
    settings: Settings, *, before: datetime, limit: int = 20
) -> list[dict[str, Any]]:
    """Deleted documents whose purge is unfinished and due for a retry."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.documents.find(
                {
                    "status": "deleted",
                    "purge_status": {"$in": ["pending", "failed"]},
                    "updated_at": {"$lte": before},
                },
                {"_id": 0, "id": 1, "user_id": 1},
            )
            .sort("updated_at", 1)
            .limit(limit)
        )
    return rows


def get_document_source_key(settings: Settings, user_id: str, document_id: str) -> str | None:
    """Return the original upload key of a live document (reindex fallback)."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.documents.find_one(
            {"id": document_id, "user_id": user_id, "deleted_at": None},
            {"_id": 0, "storage_key": 1},
        )
    return row.get("storage_key") if row else None


def load_current_extraction(
    settings: Settings, user_id: str, document_id: str
) -> dict[str, Any] | None:
    """Read the published version's extraction object for preview (dev/04).

    Returns {"kind": "text", "text": ...} for TXT/Markdown sources or
    {"kind": "pdf", "pages": [{"page", "text"}, ...]} for PDFs; None when no
    extraction has been published yet or the object is gone.
    """
    with database_connection(settings) as database:
        _ensure_indexes(database)
        document = database.documents.find_one(
            {"id": document_id, "user_id": user_id, "deleted_at": None},
            {"_id": 0, "current_version": 1},
        )
        if document is None:
            return None
        version = database.document_versions.find_one(
            {
                "document_id": document_id,
                "user_id": user_id,
                "version": document["current_version"],
                "status": "ready",
            },
            {"_id": 0, "extracted_text_key": 1},
        )
    if version is None or not version.get("extracted_text_key"):
        return None
    from app.services.storage import read_text_object

    raw = read_text_object(settings, version["extracted_text_key"])
    if raw is None:
        return None
    if str(version["extracted_text_key"]).endswith(".json"):
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        pages = payload.get("pages")
        if isinstance(pages, list):
            return {"kind": "pdf", "pages": pages}
        return None
    return {"kind": "text", "text": raw}
