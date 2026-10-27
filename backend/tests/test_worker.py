"""Tests for the indexing worker orchestration (issue #7).

Member C's pipeline is injected as a fake; per dev/08 the worker must fail the
execution explicitly when a dependency is missing or the pipeline errors, and
must never leave the execution queued forever.
"""

from types import SimpleNamespace

import pytest

from app import worker
from app.config import Settings
from app.schemas.analysis import TextLocator
from app.services.storage import read_upload, save_upload

INDEXED = SimpleNamespace(
    parser_version="parser-1",
    chunking_config_version="chunking-1",
    embedding_model="embed-1",
    embedding_dimension=8,
    index_version="index-1",
    chunks=[
        SimpleNamespace(
            chunk_id="chunk_1",
            text="led a team",
            locator=TextLocator(kind="text", line_start=1, line_end=2),
            vector_id="vec_1",
        )
    ],
)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, app_env="test", local_storage_dir=tmp_path / "storage")


def _successful_indexer():
    async def fake(raw: bytes, **kwargs):
        return INDEXED

    return fake


def _record_calls(monkeypatch):
    """Patch worker-level service boundaries with recording fakes."""
    calls = {"transitions": [], "statuses": [], "saved_versions": []}

    def fake_transition(settings_obj, user_id, execution_id, **kwargs):
        calls["transitions"].append((execution_id, kwargs))
        return True

    monkeypatch.setattr(worker, "transition_execution", fake_transition)
    monkeypatch.setattr(
        worker,
        "set_document_status",
        lambda settings_obj, user_id, document_id, **kwargs: calls["statuses"].append(
            (document_id, kwargs)
        ),
    )
    monkeypatch.setattr(
        worker,
        "save_indexed_version",
        lambda settings_obj, user_id, document_id, payload, chunks: calls["saved_versions"].append(
            (document_id, payload, list(chunks))
        ),
    )
    return calls


def test_run_indexing_success_persists_version_and_chunks(settings, tmp_path, monkeypatch):
    calls = _record_calls(monkeypatch)
    monkeypatch.setattr(worker, "_load_indexer", _successful_indexer)
    storage_key = save_upload(settings, "user_one", "resume.md", b"# Demo")

    worker.run_indexing(settings, storage_key, "user_one", "doc_x", 1, "exec_1")

    assert [kwargs["status"] for _, kwargs in calls["transitions"]] == ["running", "succeeded"]
    succeeded = calls["transitions"][1][1]
    assert succeeded["result_type"] == "document"
    assert succeeded["result_id"] == "doc_x"
    document_id, payload, chunks = calls["saved_versions"][0]
    assert document_id == "doc_x"
    assert payload.version == 1
    assert payload.content_hash.startswith("sha256:")
    assert payload.parser_version == "parser-1"
    assert payload.index_version == "index-1"
    assert [chunk.chunk_id for chunk in chunks] == ["chunk_1"]
    # save_indexed_version owns the ready transition; the worker must not also
    # flip the document, and it stores real extracted text for the version key.
    assert calls["statuses"] == []
    extracted = read_upload(settings, payload.extracted_text_key)
    assert extracted == b"# Demo"


def test_run_indexing_missing_file_fails_execution(settings, tmp_path, monkeypatch):
    calls = _record_calls(monkeypatch)
    monkeypatch.setattr(worker, "_load_indexer", _successful_indexer)

    worker.run_indexing(settings, "uploads/user_one/gone.md", "user_one", "doc_x", 1, "exec_1")

    assert [kwargs["status"] for _, kwargs in calls["transitions"]] == ["failed"]
    assert calls["transitions"][0][1]["error"].code == "FILE_MISSING"
    assert calls["statuses"] == [
        ("doc_x", {"status": "failed", "error": "上传文件丢失，请重新上传。"})
    ]
    assert calls["saved_versions"] == []


def test_run_indexing_pipeline_error_fails_execution(settings, tmp_path, monkeypatch):
    calls = _record_calls(monkeypatch)

    def raising_loader():
        async def broken(raw: bytes, **kwargs):
            raise ValueError("bad pdf")

        return broken

    monkeypatch.setattr(worker, "_load_indexer", raising_loader)
    storage_key = save_upload(settings, "user_one", "bad.pdf", b"%PDF-broken")

    worker.run_indexing(settings, storage_key, "user_one", "doc_x", 1, "exec_1")

    assert [kwargs["status"] for _, kwargs in calls["transitions"]] == ["running", "failed"]
    error = calls["transitions"][1][1]["error"]
    assert error.code == "INDEXING_FAILED"
    assert "bad pdf" in error.message
    assert calls["statuses"][0][1]["status"] == "failed"


def test_run_indexing_fails_explicitly_when_rag_module_missing(settings, tmp_path, monkeypatch):
    calls = _record_calls(monkeypatch)

    def missing_loader():
        raise RuntimeError("RAG 索引模块（app.rag.indexer，成员 C）尚未集成")

    monkeypatch.setattr(worker, "_load_indexer", missing_loader)
    storage_key = save_upload(settings, "user_one", "resume.md", b"# Demo")

    worker.run_indexing(settings, storage_key, "user_one", "doc_x", 1, "exec_1")

    error = calls["transitions"][1][1]["error"]
    assert calls["transitions"][1][1]["status"] == "failed"
    assert error.code == "INDEXING_FAILED"
    assert "尚未集成" in error.message


def test_run_indexing_stale_execution_never_indexes(settings, tmp_path, monkeypatch):
    calls = {"transitions": [], "statuses": []}

    def stale_transition(settings_obj, user_id, execution_id, **kwargs):
        calls["transitions"].append((execution_id, kwargs))
        return False

    monkeypatch.setattr(worker, "transition_execution", stale_transition)
    monkeypatch.setattr(
        worker,
        "set_document_status",
        lambda settings_obj, user_id, document_id, **kwargs: calls["statuses"].append(
            (document_id, kwargs)
        ),
    )
    loaded = {"called": False}
    monkeypatch.setattr(
        worker,
        "_load_indexer",
        lambda: loaded.__setitem__("called", True),
    )
    storage_key = save_upload(settings, "user_one", "resume.md", b"# Demo")

    worker.run_indexing(settings, storage_key, "user_one", "doc_x", 1, "exec_1")

    assert loaded["called"] is False
    assert len(calls["transitions"]) == 2
    assert calls["transitions"][0][1]["status"] == "running"
    assert calls["transitions"][1][1]["status"] == "failed"
    assert calls["transitions"][1][1]["error"].code == "STALE_EXECUTION"
