"""Tests for M2 document lifecycle: versions, soft delete and purge (issue #24)."""

import json
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.documents import (
    create_text_version,
    find_active_index_execution,
    get_deleted_document,
    get_document_source_key,
    list_purge_candidates,
    load_current_extraction,
    mark_chunks_deleted,
    set_purge_status,
    set_version_failed,
    soft_delete_document,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.documents.database_connection", connection)
    return connection.return_value.__enter__.return_value


def test_soft_delete_is_atomic_and_owner_scoped(settings, database):
    database.documents.find_one_and_update.return_value = {"id": "doc_1", "status": "deleted"}
    row = soft_delete_document(settings, "user_one", "doc_1")
    assert row["status"] == "deleted"
    query, update = database.documents.find_one_and_update.call_args.args
    assert query == {
        "id": "doc_1",
        "user_id": "user_one",
        "deleted_at": None,
        "status": {"$ne": "deleted"},
    }
    assert update["$set"]["status"] == "deleted"
    assert update["$set"]["purge_status"] == "pending"


def test_soft_delete_replay_returns_none(settings, database):
    database.documents.find_one_and_update.return_value = None
    assert soft_delete_document(settings, "user_one", "doc_1") is None


def test_create_text_version_guards_expected_version(settings, database):
    database.documents.find_one.return_value = None
    assert (
        create_text_version(
            settings, "user_one", "doc_1", expected_version=1, source_key="uploads/u/t.txt"
        )
        is None
    )
    query = database.documents.find_one.call_args.args[0]
    assert query["status"] == "ready"
    assert query["current_version"] == 1
    assert query["deleted_at"] is None
    database.document_versions.insert_one.assert_not_called()


def test_create_text_version_inserts_next_version_with_source(settings, database):
    database.documents.find_one.return_value = {"current_version": 2}
    assert (
        create_text_version(
            settings, "user_one", "doc_1", expected_version=2, source_key="uploads/u/v3.txt"
        )
        == 3
    )
    row = database.document_versions.insert_one.call_args.args[0]
    assert row["version"] == 3
    assert row["source_key"] == "uploads/u/v3.txt"
    assert row["status"] == "uploaded"
    assert row["user_id"] == "user_one"


def test_set_version_failed_only_touches_unpublished(settings, database):
    database.document_versions.update_one.return_value.matched_count = 0
    assert set_version_failed(settings, "user_one", "doc_1", 3, error="boom") is False
    query = database.document_versions.update_one.call_args.args[0]
    assert query["status"] == {"$in": ["uploaded", "writing"]}


def test_find_active_index_execution_matches_version(settings, database):
    database.executions.find_one.return_value = {"id": "exec_active"}
    assert find_active_index_execution(settings, "user_one", "doc_1", 2) == "exec_active"
    query = database.executions.find_one.call_args.args[0]
    assert query["type"] == "index"
    assert query["resource_version"] == 2
    assert query["status"] == {"$in": ["queued", "running"]}
    database.executions.find_one.return_value = None
    assert find_active_index_execution(settings, "user_one", "doc_1", 2) is None


def test_mark_chunks_deleted_returns_vector_ids(settings, database):
    database.chunks.find.return_value = [{"vector_id": "vec_1"}, {"vector_id": None}]
    assert mark_chunks_deleted(settings, "user_one", "doc_1", 1) == ["vec_1"]
    update_query = database.chunks.update_many.call_args.args[0]
    assert update_query["deleted_at"] is None


def test_load_current_extraction_text_and_pdf(settings, database, monkeypatch):
    database.documents.find_one.return_value = {"current_version": 2}
    database.document_versions.find_one.return_value = {
        "extracted_text_key": "extracted/u/doc_1/v2.txt"
    }
    monkeypatch.setattr(
        "app.services.storage.read_text_object", lambda settings, key: "plain text body"
    )
    assert load_current_extraction(settings, "user_one", "doc_1") == {
        "kind": "text",
        "text": "plain text body",
    }
    database.document_versions.find_one.return_value = {
        "extracted_text_key": "extracted/u/doc_1/v2.json"
    }
    pages = {"pages": [{"page": 1, "text": "第一页"}, {"page": 2, "text": ""}]}
    monkeypatch.setattr(
        "app.services.storage.read_text_object",
        lambda settings, key: json.dumps(pages, ensure_ascii=False),
    )
    assert load_current_extraction(settings, "user_one", "doc_1") == {
        "kind": "pdf",
        "pages": pages["pages"],
    }


def test_load_current_extraction_absent_when_not_ready_or_deleted(settings, database):
    database.documents.find_one.return_value = None
    assert load_current_extraction(settings, "user_one", "doc_1") is None
    database.documents.find_one.return_value = {"current_version": 1}
    database.document_versions.find_one.return_value = None
    assert load_current_extraction(settings, "user_one", "doc_1") is None


def test_purge_helpers_scope_and_filter(settings, database):
    database.documents.find_one.return_value = {"storage_key": "uploads/u/file.pdf"}
    assert get_document_source_key(settings, "user_one", "doc_1") == "uploads/u/file.pdf"
    database.documents.find_one.return_value = {"id": "doc_1", "status": "deleted"}
    assert get_deleted_document(settings, "user_one", "doc_1")["status"] == "deleted"
    query = database.documents.find_one.call_args.args[0]
    assert query["status"] == "deleted"
    from datetime import UTC, datetime

    before = datetime.now(UTC)
    database.documents.find.return_value.sort.return_value.limit.return_value = []
    assert list_purge_candidates(settings, before=before) == []
    purge_query = database.documents.find.call_args.args[0]
    assert purge_query["purge_status"] == {"$in": ["pending", "failed"]}
    assert purge_query["updated_at"] == {"$lte": before}


def test_set_purge_status_records_outcome(settings, database):
    set_purge_status(settings, "user_one", "doc_1", status="failed")
    query, update = database.documents.update_one.call_args.args
    assert query == {"id": "doc_1", "user_id": "user_one"}
    assert update["$set"]["purge_status"] == "failed"
