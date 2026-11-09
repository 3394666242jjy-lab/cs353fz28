"""Tests for the resume service: validation, idempotency and txn writes (issue #24)."""

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.schemas.resources import ResumeAnswer
from app.services.inputs import InputConflictError, answer_content_hash
from app.services.resume import ResumeError, resume_execution


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def mocks(settings, monkeypatch):
    connection = MagicMock()
    session = MagicMock(name="session")
    connection.return_value.__enter__.return_value = (db := MagicMock(), session)
    monkeypatch.setattr("app.services.resume.transactional_connection", connection)
    for name in (
        "ensure_execution_indexes",
        "ensure_input_indexes",
        "ensure_dispatch_indexes",
    ):
        monkeypatch.setattr(f"app.services.resume.{name}", lambda *a: None)
    recorded = {}
    monkeypatch.setattr(
        "app.services.resume.record_execution_input",
        lambda *a, **k: recorded.setdefault("called", True) or "inp_1",
    )
    monkeypatch.setattr("app.services.resume.enqueue_dispatch", lambda *a, **k: "disp_1")
    published = []
    monkeypatch.setattr(
        "app.services.resume.publish_dispatch",
        lambda settings, dispatch_id: published.append(dispatch_id) or True,
    )
    return db, published


ANSWER = ResumeAnswer(text="补充一段数据库经历")


def _answer_payload():
    return {"text": ANSWER.text, "continue_without_evidence": False, "document_ids": []}


def test_resume_replay_same_input_returns_without_writes(settings, mocks, monkeypatch):
    db, published = mocks
    content_hash = answer_content_hash({**_answer_payload(), "document_versions": []})
    monkeypatch.setattr(
        "app.services.resume.get_execution_input",
        lambda *a: {"id": "inp_existing", "content_hash": content_hash},
    )
    assert (
        resume_execution(
            settings,
            "user_one",
            "exec_1",
            interrupt_id="int_1",
            checkpoint_id="chk_1",
            answer=ANSWER,
            document_versions=[],
        )
        == "exec_1"
    )
    db.executions.find_one.assert_not_called()
    assert published == []


def test_resume_replay_different_input_conflicts(settings, mocks, monkeypatch):
    monkeypatch.setattr(
        "app.services.resume.get_execution_input",
        lambda *a: {"id": "inp_existing", "content_hash": answer_content_hash({"text": "旧"})},
    )
    with pytest.raises(InputConflictError):
        resume_execution(
            settings,
            "user_one",
            "exec_1",
            interrupt_id="int_1",
            checkpoint_id="chk_1",
            answer=ANSWER,
            document_versions=[],
        )


def _waiting(**overrides):
    row = {
        "id": "exec_1",
        "user_id": "user_one",
        "status": "waiting_for_input",
        "state_version": 4,
        "pending_input": {
            "kind": "clarification",
            "interrupt_id": "int_1",
            "checkpoint_id": "chk_1",
        },
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("execution", "status_code", "code"),
    [
        (None, 404, "NOT_FOUND"),
        (_waiting(status="running"), 409, "NOT_WAITING"),
        (
            _waiting(pending_input={"kind": "plan_confirmation"}),
            409,
            "RESUME_KIND_MISMATCH",
        ),
        (
            _waiting(
                pending_input={
                    "kind": "clarification",
                    "interrupt_id": "old",
                    "checkpoint_id": "chk_1",
                }
            ),
            409,
            "STALE_CHECKPOINT",
        ),
        (
            _waiting(
                pending_input={
                    "kind": "clarification",
                    "interrupt_id": "int_1",
                    "checkpoint_id": "old",
                }
            ),
            409,
            "STALE_CHECKPOINT",
        ),
    ],
)
def test_resume_validation_failures(settings, mocks, monkeypatch, execution, status_code, code):
    db, _ = mocks
    monkeypatch.setattr("app.services.resume.get_execution_input", lambda *a: None)
    db.executions.find_one.return_value = execution
    with pytest.raises(ResumeError) as excinfo:
        resume_execution(
            settings,
            "user_one",
            "exec_1",
            interrupt_id="int_1",
            checkpoint_id="chk_1",
            answer=ANSWER,
            document_versions=[],
        )
    assert excinfo.value.status_code == status_code
    assert excinfo.value.code == code


def test_resume_commits_input_transition_and_dispatch(settings, mocks, monkeypatch):
    db, published = mocks
    monkeypatch.setattr("app.services.resume.get_execution_input", lambda *a: None)
    db.executions.find_one.return_value = _waiting()
    db.executions.update_one.return_value.matched_count = 1
    versions = [{"document_id": "doc_1", "version": 2}]
    assert (
        resume_execution(
            settings,
            "user_one",
            "exec_1",
            interrupt_id="int_1",
            checkpoint_id="chk_1",
            answer=ResumeAnswer(text="", document_ids=["doc_1"]),
            document_versions=versions,
        )
        == "exec_1"
    )
    resume_query, resume_update = db.executions.update_one.call_args.args
    assert resume_query["status"] == "waiting_for_input"
    assert resume_query["state_version"] == 4
    assert resume_update["$set"]["status"] == "queued"
    assert resume_update["$inc"] == {"state_version": 1}
    assert published == ["disp_1"]
