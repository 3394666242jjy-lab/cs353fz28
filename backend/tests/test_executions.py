"""Tests for the executions data layer: MongoDB is the authoritative task state."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.resources import ExecutionError, ExecutionResult
from app.services.executions import (
    ExecutionCreate,
    create_execution,
    get_execution,
    transition_execution,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.executions.database_connection", connection)
    return connection.return_value.__enter__.return_value


def test_create_execution_queues_at_state_version_one(settings, database):
    execution_id = create_execution(
        settings, "user_one", ExecutionCreate(type="index", resource_id="doc_x")
    )
    assert execution_id.startswith("exec_")
    document = database.executions.insert_one.call_args.args[0]
    assert document["id"] == execution_id
    assert document["user_id"] == "user_one"
    assert document["status"] == "queued"
    assert document["state_version"] == 1
    assert document["result_type"] is None
    assert document["error"] is None


def test_get_execution_maps_result(settings, database):
    database.executions.find_one.return_value = {
        "id": "exec_x",
        "type": "analysis",
        "status": "succeeded",
        "state_version": 3,
        "result_type": "analysis",
        "result_id": "ana_x",
        "pending_input": None,
        "error": None,
        "latest_event_id": None,
    }
    result = get_execution(settings, "user_one", "exec_x")
    assert isinstance(result, ExecutionResult)
    assert result.status == "succeeded"
    assert result.result_id == "ana_x"
    query = database.executions.find_one.call_args.args[0]
    assert query == {"id": "exec_x", "user_id": "user_one"}


def test_get_execution_missing_returns_none(settings, database):
    database.executions.find_one.return_value = None
    assert get_execution(settings, "user_one", "exec_missing") is None


def test_transition_requires_matching_state_version(settings, database):
    database.executions.update_one.return_value.matched_count = 0
    moved = transition_execution(
        settings, "user_one", "exec_x", expected_state_version=1, status="running"
    )
    assert moved is False
    query = database.executions.update_one.call_args.args[0]
    assert query["state_version"] == 1
    assert query["user_id"] == "user_one"
    assert query["status"] == {"$in": ["queued"]}
    update = database.executions.update_one.call_args.args[1]
    assert update["$inc"] == {"state_version": 1}
    assert update["$set"]["status"] == "running"


def test_transition_bumps_state_version_on_success(settings, database):
    database.executions.update_one.return_value.matched_count = 1
    moved = transition_execution(
        settings,
        "user_one",
        "exec_x",
        expected_state_version=2,
        status="succeeded",
        result_type="document",
        result_id="doc_x",
    )
    assert moved is True
    assert database.executions.update_one.call_args.args[0]["status"] == {"$in": ["running"]}


def test_transition_rejects_succeeded_without_result(settings, database):
    with pytest.raises(ValidationError):
        transition_execution(
            settings, "user_one", "exec_x", expected_state_version=1, status="succeeded"
        )


def test_transition_rejects_failed_without_error(settings, database):
    with pytest.raises(ValidationError):
        transition_execution(
            settings, "user_one", "exec_x", expected_state_version=1, status="failed"
        )


def test_transition_failed_carries_error(settings, database):
    database.executions.update_one.return_value.matched_count = 1
    error = ExecutionError(code="PARSER_FAILED", message="PDF has no extractable text")
    moved = transition_execution(
        settings, "user_one", "exec_x", expected_state_version=1, status="failed", error=error
    )
    assert moved is True
    update = database.executions.update_one.call_args.args[1]
    assert update["$set"]["error"] == {
        "code": "PARSER_FAILED",
        "message": "PDF has no extractable text",
    }


def test_create_rejects_unknown_type(settings, database):
    with pytest.raises(ValidationError):
        ExecutionCreate(type="unknown", resource_id="doc_x")
