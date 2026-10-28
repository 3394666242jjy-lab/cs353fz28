"""Tests for the owner-scoped documents data layer (issue #7)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from app.config import Settings
from app.schemas.resources import DocumentResult
from app.services.documents import (
    create_document,
    get_chunk,
    get_document,
    list_chunks_by_ids,
    list_documents,
    save_indexed_version,
    set_document_status,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.documents.database_connection", connection)
    db = connection.return_value.__enter__.return_value
    db.document_versions.update_one.return_value.matched_count = 1
    db.documents.update_one.return_value.matched_count = 1
    return db


def _chunk(chunk_id: str, page: int) -> SimpleNamespace:
    locator = MagicMock()
    locator.model_dump.return_value = {"kind": "pdf", "page": page}
    return SimpleNamespace(
        chunk_id=chunk_id, text=f"text {chunk_id}", locator=locator, vector_id=f"vec_{chunk_id}"
    )


PAYLOAD = {
    "version": 2,
    "extracted_text_key": "extracted/doc_x/v2.txt",
    "content_hash": "sha256:abc",
    "parser_version": "pdfminer-2025.1",
    "chunking_config_version": "chunking-v1",
    "embedding_model": "text-embedding-v4",
    "embedding_dimension": 1024,
    "index_version": "index-v1",
}


def test_create_document_pins_version_one_uploaded(settings, database):
    document_id = create_document(
        settings,
        "user_one",
        kind="resume",
        filename="resume.pdf",
        media_type="application/pdf",
        size_bytes=2048,
        storage_key="private/user_one/resume.pdf",
    )
    assert document_id.startswith("doc_")
    version = database.document_versions.insert_one.call_args.args[0]
    assert version["document_id"] == document_id
    assert version["version"] == 1
    assert version["status"] == "uploaded"
    assert version["user_id"] == "user_one"
    document = database.documents.insert_one.call_args.args[0]
    assert document["current_version"] == 1
    assert document["status"] == "uploaded"
    assert document["deleted_at"] is None


def test_list_documents_scopes_and_paginates_by_owner(settings, database):
    database.documents.find.return_value.sort.return_value.limit.return_value = []
    items, next_cursor = list_documents(settings, "user_one", cursor=None, limit=20)
    assert items == []
    assert next_cursor is None
    query = database.documents.find.call_args.args[0]
    assert query["user_id"] == "user_one"
    assert query["deleted_at"] is None


def test_list_documents_follows_cursor_anchor(settings, database):
    database.documents.find_one.return_value = {
        "id": "doc_anchor",
        "created_at": "2026-10-22T00:00:00+00:00",
    }
    database.documents.find.return_value.sort.return_value.limit.return_value = []
    list_documents(settings, "user_one", cursor="doc_anchor", limit=20)
    anchor_query = database.documents.find_one.call_args.args[0]
    assert anchor_query == {"id": "doc_anchor", "user_id": "user_one", "deleted_at": None}
    page_query = database.documents.find.call_args.args[0]
    assert "$or" in page_query


def test_get_document_excludes_deleted_and_other_users(settings, database):
    database.documents.find_one.return_value = None
    assert get_document(settings, "user_one", "doc_x") is None
    query = database.documents.find_one.call_args.args[0]
    assert query == {"id": "doc_x", "user_id": "user_one", "deleted_at": None}


def test_get_document_maps_result(settings, database):
    database.documents.find_one.return_value = {
        "id": "doc_x",
        "kind": "project",
        "filename": "project.md",
        "current_version": 1,
        "status": "ready",
        "error": None,
    }
    result = get_document(settings, "user_one", "doc_x")
    assert isinstance(result, DocumentResult)
    assert result.status == "ready"


def test_set_document_status_updates_owner_scoped(settings, database):
    database.documents.update_one.return_value.matched_count = 1
    assert set_document_status(settings, "user_one", "doc_x", status="processing") is True
    query = database.documents.update_one.call_args.args[0]
    assert query["id"] == "doc_x"
    assert query["user_id"] == "user_one"
    update = database.documents.update_one.call_args.args[1]
    assert update["$set"]["status"] == "processing"


def test_save_indexed_version_replaces_chunks_and_marks_ready(settings, database):
    save_indexed_version(
        settings, "user_one", "doc_x", PAYLOAD, [_chunk("chunk_1", 2), _chunk("chunk_2", 3)]
    )
    delete_query = database.chunks.delete_many.call_args.args[0]
    assert delete_query == {
        "user_id": "user_one",
        "corpus": "personal",
        "document_id": "doc_x",
        "version": 2,
    }
    inserted = database.chunks.insert_many.call_args.args[0]
    assert [row["id"] for row in inserted] == ["chunk_1", "chunk_2"]
    assert inserted[0]["corpus"] == "personal"
    assert inserted[0]["locator"] == {"kind": "pdf", "page": 2}
    assert inserted[0]["vector_id"] == "vec_chunk_1"
    version_query = database.document_versions.update_one.call_args.args[0]
    assert version_query["document_id"] == "doc_x"
    assert version_query["version"] == 2
    assert version_query["user_id"] == "user_one"
    version_set = database.document_versions.update_one.call_args.args[1]["$set"]
    assert version_set["status"] == "ready"
    assert version_set["index_version"] == "index-v1"
    document_query = database.documents.update_one.call_args.args[0]
    assert document_query["id"] == "doc_x"
    document_set = database.documents.update_one.call_args.args[1]["$set"]
    assert document_set["current_version"] == 2
    assert document_set["status"] == "ready"


def test_save_indexed_version_accepts_text_locator(settings, database):
    locator = MagicMock()
    locator.model_dump.return_value = {"kind": "text", "line_start": 10, "line_end": 18}
    chunk = SimpleNamespace(chunk_id="chunk_t", text="lines", locator=locator, vector_id="vec_t")
    save_indexed_version(settings, "user_one", "doc_x", PAYLOAD, [chunk])
    inserted = database.chunks.insert_many.call_args.args[0]
    assert inserted[0]["locator"]["line_start"] == 10


def test_save_indexed_version_rejects_bad_payload(settings, database):
    with pytest.raises(ValidationError):
        save_indexed_version(settings, "user_one", "doc_x", {"version": 0}, [])


def test_get_chunk_scopes_by_owner_and_version(settings, database):
    database.chunks.find_one.return_value = {"id": "chunk_1"}
    row = get_chunk(settings, "user_one", "chunk_1", 2)
    assert row == {"id": "chunk_1"}
    query = database.chunks.find_one.call_args.args[0]
    assert query == {"id": "chunk_1", "user_id": "user_one", "version": 2, "deleted_at": None}


def test_failed_chunk_write_never_publishes_version(settings, database):
    database.chunks.insert_many.side_effect = ConnectionFailure("write failed")
    with pytest.raises(ConnectionFailure):
        save_indexed_version(settings, "user_one", "doc_x", PAYLOAD, [_chunk("c1", 1)])
    assert database.document_versions.update_one.call_args.args[1]["$set"]["status"] == "failed"
    database.documents.update_one.assert_not_called()
    claim = database.document_versions.update_one.call_args_list[0].args[1]
    assert claim["$setOnInsert"]["id"].startswith("dver_")
    assert "created_at" in claim["$setOnInsert"]


def test_published_version_cannot_be_replaced(settings, database):
    database.document_versions.update_one.side_effect = DuplicateKeyError("already published")
    with pytest.raises(DuplicateKeyError):
        save_indexed_version(settings, "user_one", "doc_x", PAYLOAD, [_chunk("c1", 1)])
    database.chunks.delete_many.assert_not_called()
    database.documents.update_one.assert_not_called()


def test_version_is_not_published_if_write_claim_is_lost(settings, database):
    database.document_versions.update_one.side_effect = [
        SimpleNamespace(matched_count=1),
        SimpleNamespace(matched_count=0),
    ]
    with pytest.raises(RuntimeError, match="changed while"):
        save_indexed_version(settings, "user_one", "doc_x", PAYLOAD, [_chunk("c1", 1)])
    database.documents.update_one.assert_not_called()


def test_empty_chunks_are_rejected_before_writes(settings, database):
    with pytest.raises(ValueError, match="without text chunks"):
        save_indexed_version(settings, "user_one", "doc_x", PAYLOAD, [])
    database.document_versions.update_one.assert_not_called()
    database.chunks.delete_many.assert_not_called()


def test_list_chunks_by_ids_owner_scoped(settings, database):
    database.chunks.find.return_value = [{"id": "chunk_1"}]
    rows = list_chunks_by_ids(settings, "user_one", ["chunk_1", "chunk_2"])
    assert rows == [{"id": "chunk_1"}]
    query = database.chunks.find.call_args.args[0]
    assert query["id"] == {"$in": ["chunk_1", "chunk_2"]}
    assert query["user_id"] == "user_one"
    assert query["deleted_at"] is None
    assert list_chunks_by_ids(settings, "user_one", []) == []
    database.chunks.find.assert_called_once()
