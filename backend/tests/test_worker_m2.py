"""Tests for M2 worker tasks: purge, plan generation and resume (issue #24)."""

from types import SimpleNamespace

import pytest

from app import worker
from app.config import Settings
from app.rag.vector_store import VectorStoreError


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, app_env="test", local_storage_dir=tmp_path / "storage")


def _purge_mocks(monkeypatch):
    calls = {"chunks": [], "vectors": [], "removed": [], "status": []}
    monkeypatch.setattr(
        worker,
        "get_deleted_document",
        lambda settings, user_id, document_id: {
            "id": document_id,
            "storage_key": f"uploads/{user_id}/file.pdf",
        },
    )
    monkeypatch.setattr(
        worker,
        "list_document_versions",
        lambda *a: [
            {"version": 1, "extracted_text_key": "extracted/u/doc_1/v1.json", "source_key": None},
            {
                "version": 2,
                "extracted_text_key": "extracted/u/doc_1/v2.txt",
                "source_key": "uploads/u/v2.txt",
            },
        ],
    )
    monkeypatch.setattr(
        worker,
        "mark_chunks_deleted",
        lambda settings, user_id, document_id, version: (
            calls["chunks"].append(version) or [f"vec_v{version}"]
        ),
    )

    class FakeStore:
        async def delete_document_points(self, *, user_id, document_id):
            calls["vectors"].append((user_id, document_id))

    monkeypatch.setattr(worker, "_load_vector_store", lambda *a: lambda settings: FakeStore())
    monkeypatch.setattr(
        worker,
        "remove_upload",
        lambda settings, key: calls["removed"].append(key) or True,
    )
    monkeypatch.setattr(
        worker,
        "remove_extracted_versions",
        lambda settings, user_id, document_id, keys: calls["removed"].extend(keys),
    )
    monkeypatch.setattr(
        worker,
        "set_purge_status",
        lambda settings, user_id, document_id, **kwargs: calls["status"].append(kwargs["status"]),
    )
    return calls


def test_purge_deletes_chunks_vectors_and_files(settings, monkeypatch):
    calls = _purge_mocks(monkeypatch)
    worker._run_purge(settings, "user_one", "doc_1")
    assert calls["chunks"] == [1, 2]
    assert calls["vectors"] == [("user_one", "doc_1")]
    assert "uploads/user_one/file.pdf" in calls["removed"]
    assert "extracted/u/doc_1/v2.txt" in calls["removed"]
    assert "uploads/u/v2.txt" in calls["removed"]
    assert calls["status"] == ["done"]


def test_purge_is_noop_when_document_not_deleted(settings, monkeypatch):
    monkeypatch.setattr(worker, "get_deleted_document", lambda *a: None)
    store_called = []
    monkeypatch.setattr(
        worker,
        "_load_vector_store",
        lambda *a: store_called.append(True) or (_ for _ in ()).throw(AssertionError),
    )
    worker._run_purge(settings, "user_one", "doc_1")
    assert store_called == []


def test_purge_task_marks_failed_after_non_retryable_error(settings, monkeypatch):
    monkeypatch.setattr(
        worker,
        "get_deleted_document",
        lambda *a: {"id": "doc_1", "storage_key": None},
    )
    monkeypatch.setattr(worker, "list_document_versions", lambda *a: [])

    class BrokenStore:
        async def delete_document_points(self, *, user_id, document_id):
            raise VectorStoreError("permanent", retryable=False)

    monkeypatch.setattr(worker, "_load_vector_store", lambda *a: lambda s: BrokenStore())
    statuses = []
    monkeypatch.setattr(
        worker,
        "set_purge_status",
        lambda settings, user_id, document_id, **kwargs: statuses.append(kwargs["status"]),
    )
    # Non-retryable errors must not retry; the sweeper re-dispatches later.
    worker.purge_document_task.run("user_one", "doc_1")
    assert statuses == ["failed"]


def _transition_recorder(monkeypatch, outcomes=None):
    calls = []
    queue = list(outcomes) if outcomes else None

    def fake_transition(settings, user_id, execution_id, **kwargs):
        calls.append(kwargs)
        return queue.pop(0) if queue else True

    monkeypatch.setattr(worker, "transition_execution", fake_transition)
    return calls


def test_run_plan_discards_duplicate_delivery(settings, monkeypatch):
    transitions = _transition_recorder(monkeypatch, outcomes=[False])
    monkeypatch.setattr(
        worker,
        "get_plan_by_execution",
        lambda *a: (_ for _ in ()).throw(AssertionError("duplicate reached plan load")),
    )
    worker.run_plan(settings, "disp_1", "user_one", "exec_1", 1)
    assert transitions[0]["status"] == "running"


def test_run_plan_missing_plan_fails_execution(settings, monkeypatch):
    transitions = _transition_recorder(monkeypatch)
    monkeypatch.setattr(worker, "get_plan_by_execution", lambda *a: None)
    worker.run_plan(settings, "disp_1", "user_one", "exec_1", 1)
    assert transitions[-1]["status"] == "failed"
    assert transitions[-1]["error"].code == "PLAN_MISSING"


def test_run_plan_without_d_module_fails_explicitly(settings, monkeypatch):
    transitions = _transition_recorder(monkeypatch)
    monkeypatch.setattr(
        worker,
        "get_plan_by_execution",
        lambda *a: {"id": "plan_1", "analysis_id": "analysis_1"},
    )
    monkeypatch.setattr(worker, "get_analysis", lambda *a: {"id": "analysis_1", "status": "ready"})

    def missing():
        raise RuntimeError("计划生成模块（app.agent.plan，成员 D）尚未集成")

    monkeypatch.setattr(worker, "_load_plan_generator", missing)
    worker.run_plan(settings, "disp_1", "user_one", "exec_1", 1)
    assert transitions[-1]["status"] == "failed"
    assert transitions[-1]["error"].code == "PLAN_GENERATION_FAILED"
    assert "尚未集成" in transitions[-1]["error"].message


def test_run_plan_success_parks_at_plan_confirmation(settings, monkeypatch):
    _transition_recorder(monkeypatch)
    monkeypatch.setattr(
        worker,
        "get_plan_by_execution",
        lambda *a: {"id": "plan_1", "analysis_id": "analysis_1"},
    )
    monkeypatch.setattr(worker, "get_analysis", lambda *a: {"id": "analysis_1", "status": "ready"})

    async def fake_generate(*, user_id, plan, analysis, settings):
        return SimpleNamespace(
            draft_tasks=[
                {"title": "复习索引", "scheduled_date": "2026-11-06", "estimated_minutes": 45}
            ],
            interrupt_id="int_1",
            checkpoint_id="chk_1",
        )

    monkeypatch.setattr(worker, "_load_plan_generator", lambda: fake_generate)
    saved = {}
    monkeypatch.setattr(
        worker,
        "save_plan_draft",
        lambda settings, user_id, plan_id, **kwargs: saved.update(kwargs) or True,
    )
    waiting = {}
    monkeypatch.setattr(
        worker,
        "transition_to_waiting",
        lambda settings, user_id, execution_id, **kwargs: waiting.update(kwargs) or True,
    )
    worker.run_plan(settings, "disp_1", "user_one", "exec_1", 1)
    assert saved["draft_tasks"][0]["title"] == "复习索引"
    assert waiting["pending_input"].kind == "plan_confirmation"
    assert waiting["pending_input"].plan_id == "plan_1"
    assert waiting["pending_input"].interrupt_id == "int_1"


def test_run_resume_discards_duplicate_delivery(settings, monkeypatch):
    _transition_recorder(monkeypatch, outcomes=[False])
    monkeypatch.setattr(
        worker,
        "get_dispatch",
        lambda *a: (_ for _ in ()).throw(AssertionError("duplicate reached dispatch load")),
    )
    worker.run_resume(settings, "disp_1", "user_one", "exec_1", 2)


def test_run_resume_without_input_record_fails(settings, monkeypatch):
    transitions = _transition_recorder(monkeypatch)
    monkeypatch.setattr(worker, "get_dispatch", lambda *a: {"id": "disp_1", "input_ref": "inp_1"})
    monkeypatch.setattr(worker, "load_input_by_id", lambda *a: None)
    worker.run_resume(settings, "disp_1", "user_one", "exec_1", 2)
    assert transitions[-1]["status"] == "failed"
    assert transitions[-1]["error"].code == "RESUME_FAILED"


def test_run_resume_without_d_module_fails_explicitly(settings, monkeypatch):
    transitions = _transition_recorder(monkeypatch)
    monkeypatch.setattr(worker, "get_dispatch", lambda *a: {"id": "disp_1", "input_ref": "inp_1"})
    monkeypatch.setattr(
        worker, "load_input_by_id", lambda *a: {"id": "inp_1", "answer": {"text": "补充"}}
    )

    def missing():
        raise RuntimeError("执行恢复模块（app.agent.resume，成员 D）尚未集成")

    monkeypatch.setattr(worker, "_load_resume_handler", missing)
    worker.run_resume(settings, "disp_1", "user_one", "exec_1", 2)
    assert transitions[-1]["status"] == "failed"
    assert "尚未集成" in transitions[-1]["error"].message
