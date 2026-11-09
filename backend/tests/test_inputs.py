"""Tests for execution inputs: resume idempotency anchors (issue #24)."""

from unittest.mock import MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.services.inputs import (
    InputConflictError,
    answer_content_hash,
    get_execution_input,
    record_execution_input,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.inputs.database_connection", connection)
    return connection.return_value.__enter__.return_value


def test_content_hash_is_order_independent_and_content_sensitive():
    first = answer_content_hash({"text": "补充", "document_ids": ["doc_1"], "flag": True})
    second = answer_content_hash({"flag": True, "document_ids": ["doc_1"], "text": "补充"})
    assert first == second
    assert first.startswith("sha256:")
    assert first != answer_content_hash({"text": "别的", "document_ids": ["doc_1"], "flag": True})


def test_record_execution_input_inserts_with_unique_guard(database):
    input_id = record_execution_input(
        database,
        user_id="user_one",
        execution_id="exec_1",
        interrupt_id="int_1",
        checkpoint_id="chk_1",
        kind="clarification",
        answer={"text": "补充"},
        document_versions=[{"document_id": "doc_1", "version": 2}],
    )
    row = database.execution_inputs.insert_one.call_args.args[0]
    assert row["id"] == input_id
    assert row["kind"] == "clarification"
    assert row["document_versions"] == [{"document_id": "doc_1", "version": 2}]
    assert row["content_hash"].startswith("sha256:")


def test_record_execution_input_replay_same_content_returns_existing(database):
    answer = {"text": "补充", "document_ids": []}
    database.execution_inputs.insert_one.side_effect = DuplicateKeyError("dup")
    database.execution_inputs.find_one.return_value = {
        "id": "inp_existing",
        "content_hash": answer_content_hash({**answer, "document_versions": []}),
    }
    assert (
        record_execution_input(
            database,
            user_id="user_one",
            execution_id="exec_1",
            interrupt_id="int_1",
            checkpoint_id="chk_1",
            kind="clarification",
            answer=answer,
        )
        == "inp_existing"
    )


def test_record_execution_input_replay_different_content_conflicts(database):
    database.execution_inputs.insert_one.side_effect = DuplicateKeyError("dup")
    database.execution_inputs.find_one.return_value = {
        "id": "inp_existing",
        "content_hash": answer_content_hash({"text": "旧回答"}),
    }
    with pytest.raises(InputConflictError):
        record_execution_input(
            database,
            user_id="user_one",
            execution_id="exec_1",
            interrupt_id="int_1",
            checkpoint_id="chk_1",
            kind="clarification",
            answer={"text": "新回答"},
        )


def test_get_execution_input_scopes_to_owner(settings, database):
    database.execution_inputs.find_one.return_value = None
    assert get_execution_input(settings, "user_one", "exec_1", "int_1") is None
    query = database.execution_inputs.find_one.call_args.args[0]
    assert query["user_id"] == "user_one"
    assert query["execution_id"] == "exec_1"
    assert query["interrupt_id"] == "int_1"
