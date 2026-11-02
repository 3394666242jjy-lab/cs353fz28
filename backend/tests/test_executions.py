"""Tests for the executions data layer: MongoDB is the authoritative task state."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.resources import ExecutionError, ExecutionResult
from app.services.executions import (
    ExecutionCreate,
    _recover_expired,
    create_execution,
    get_execution,
    heartbeat_execution,
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


def test_expired_index_is_failed_only_after_resource_claim(settings, database):
    database.executions.find_one_and_update.side_effect = [
        {"id": "exec_x", "type": "index", "resource_id": "doc_x", "status": "running"},
        {"id": "exec_x", "status": "failed"},
    ]
    database.documents.find_one.return_value = {"status": "failed"}
    result = _recover_expired(settings, "user_one", "exec_x", "running")
    assert result["status"] == "failed"
    assert database.documents.update_one.call_args.args[0]["status"] == "uploaded"
    query, update = database.executions.find_one_and_update.call_args.args
    assert query["status"] == "running" and query["recovery_claim"]
    assert update["$set"]["error"]["code"] == "WORKER_INTERRUPTED"


def test_expired_analysis_with_ready_result_becomes_succeeded(settings, database):
    database.executions.find_one_and_update.side_effect = [
        {"id": "exec_x", "type": "analysis", "resource_id": "ana_x", "status": "running"},
        {"id": "exec_x", "status": "succeeded"},
    ]
    database.analyses.find_one.return_value = {"status": "ready"}
    result = _recover_expired(settings, "user_one", "exec_x", "running")
    assert result["status"] == "succeeded"
    update = database.executions.find_one_and_update.call_args.args[1]
    assert update["$set"]["result_id"] == "ana_x"
    assert database.analyses.update_one.call_args.args[0]["status"] == "pending"


def test_expiry_lost_to_heartbeat_does_not_touch_resource(settings, database):
    database.executions.find_one_and_update.return_value = None
    database.executions.find_one.return_value = {"status": "running"}
    assert _recover_expired(settings, "user_one", "exec_x", "running")["status"] == "running"
    database.documents.update_one.assert_not_called()
    database.analyses.update_one.assert_not_called()


def test_heartbeat_cannot_resurrect_recovery(settings, database):
    heartbeat_execution(settings, "user_one", "exec_x")
    query = database.executions.update_one.call_args.args[0]
    assert query["status"] == "running"
    assert query["recovery_claim"] == {"$exists": False}


def test_document_poll_recovers_only_owner_index_executions(settings, database, monkeypatch):
    from app.services.executions import recover_document_execution

    database.executions.find.return_value = [{"id": "exec_x", "status": "queued"}]
    recovered = []
    monkeypatch.setattr(
        "app.services.executions._recover_expired", lambda *args: recovered.append(args)
    )
    recover_document_execution(settings, "user_one", "doc_x")
    query = database.executions.find.call_args.args[0]
    assert query["user_id"] == "user_one"
    assert query["resource_id"] == "doc_x" and query["type"] == "index"
    assert recovered == [(settings, "user_one", "exec_x", "queued")]


def test_failed_index_reconciliation_only_updates_uploaded_owned_document(settings, database):
    from app.services.executions import _reconcile_failed_resource

    execution = {"type": "index", "resource_id": "doc_x", "error": {"message": "parse failed"}}
    _reconcile_failed_resource(settings, "user_one", execution)
    _reconcile_failed_resource(settings, "user_one", execution)
    query, update = database.documents.update_one.call_args.args
    assert query == {"id": "doc_x", "user_id": "user_one", "deleted_at": None, "status": "uploaded"}
    assert update["$set"]["status"] == "failed"
    assert update["$set"]["error"] == "parse failed"
    assert database.documents.update_one.call_count == 2


def test_failed_analysis_reconciliation_only_updates_pending(settings, database):
    from app.services.executions import _reconcile_failed_resource

    execution = {"type": "analysis", "resource_id": "ana_x", "error": {"message": "agent failed"}}
    _reconcile_failed_resource(settings, "user_one", execution)
    query, update = database.analyses.update_one.call_args.args
    assert query == {"id": "ana_x", "user_id": "user_one", "status": "pending"}
    assert update["$set"]["error"] == "agent failed"


def test_document_poll_reconciles_failed_execution(settings, database, monkeypatch):
    from app.services.executions import recover_document_execution

    row = {"id": "exec_x", "type": "index", "resource_id": "doc_x", "status": "failed"}
    database.executions.find.return_value = [row]
    reconciled = []
    monkeypatch.setattr(
        "app.services.executions._reconcile_failed_resource", lambda *args: reconciled.append(args)
    )
    recover_document_execution(settings, "user_one", "doc_x")
    assert "failed" in database.executions.find.call_args.args[0]["status"]["$in"]
    assert reconciled == [(settings, "user_one", row)]


def test_execution_poll_reconciles_failed_analysis(settings, database, monkeypatch):
    row = {
        "id": "exec_x",
        "type": "analysis",
        "resource_id": "ana_x",
        "status": "failed",
        "state_version": 3,
        "error": {"code": "ANALYSIS_FAILED", "message": "failed"},
    }
    database.executions.find_one.return_value = row
    reconciled = []
    monkeypatch.setattr(
        "app.services.executions._reconcile_failed_resource", lambda *args: reconciled.append(args)
    )
    assert get_execution(settings, "user_one", "exec_x").status == "failed"
    assert reconciled == [(settings, "user_one", row)]


def test_sweeper_bounds_batch_and_reconciles_without_request(settings, database, monkeypatch):
    from app.services.executions import sweep_expired_executions

    database.executions.find.return_value.sort.return_value.limit.return_value = [
        {"id": "exec_x", "user_id": "user_one", "status": "running"},
        {"id": "exec_y", "user_id": "user_two", "type": "analysis", "status": "failed"},
    ]
    recovered, reconciled = [], []
    monkeypatch.setattr("app.services.executions._recover_expired", lambda *a: recovered.append(a))
    monkeypatch.setattr(
        "app.services.executions._reconcile_failed_resource", lambda *a: reconciled.append(a)
    )
    sweep_expired_executions(settings, limit=12)
    database.executions.find.return_value.sort.return_value.limit.assert_called_once_with(12)
    assert recovered == [(settings, "user_one", "exec_x", "running")]
    assert reconciled[0][1:] == (
        "user_two",
        {"id": "exec_y", "user_id": "user_two", "type": "analysis", "status": "failed"},
    )


def test_recovery_loop_survives_database_errors(settings, monkeypatch):
    from app.main import _recovery_loop

    stop = MagicMock()
    stop.wait.side_effect = [False, False, True]
    calls = []

    def sweep(_settings):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("database unavailable")

    monkeypatch.setattr("app.main.sweep_expired_executions", sweep)
    _recovery_loop(settings, stop)
    assert len(calls) == 2


def test_api_lifecycle_starts_and_stops_recovery_thread(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    thread = MagicMock()
    factory = MagicMock(return_value=thread)
    monkeypatch.setattr("app.main.Thread", factory)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        thread.start.assert_called_once()
    thread.join.assert_called_once_with(timeout=1)
    assert factory.call_args.kwargs["daemon"] is True
