"""Tests for execution event persistence and SSE replay primitives (issue #24)."""

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.events import (
    append_event,
    append_event_best_effort,
    count_events,
    get_event_sequence,
    list_events,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test", event_history_retention_count=3)


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.events.database_connection", connection)
    return connection.return_value.__enter__.return_value


def test_append_event_allocates_sequence_and_updates_cursor(settings, database):
    database.executions.find_one_and_update.return_value = {"event_seq": 7}
    event_id = append_event(
        settings,
        "user_one",
        "exec_1",
        event_type="stage",
        status="running",
        stage="planning",
        payload={"execution_id": "exec_1", "status": "running", "stage": "planning"},
    )
    assert event_id is not None and event_id.startswith("ev_")
    _, update = database.executions.find_one_and_update.call_args.args
    assert update["$inc"] == {"event_seq": 1}
    assert update["$set"]["latest_event_id"] == event_id
    inserted = database.execution_events.insert_one.call_args.args[0]
    assert inserted["sequence"] == 7
    assert inserted["event_type"] == "stage"
    assert inserted["stage"] == "planning"
    assert inserted["user_id"] == "user_one"
    # Retention: with sequence 7 and retention 3, events <= 4 are pruned.
    prune = database.execution_events.delete_many.call_args.args[0]
    assert prune == {"execution_id": "exec_1", "sequence": {"$lte": 4}}


def test_append_event_returns_none_for_missing_execution(settings, database):
    database.executions.find_one_and_update.return_value = None
    assert (
        append_event(settings, "user_one", "exec_gone", event_type="stage", status="running")
        is None
    )
    database.execution_events.insert_one.assert_not_called()


def test_append_event_best_effort_never_raises(settings, database):
    database.executions.find_one_and_update.side_effect = RuntimeError("mongo down")
    append_event_best_effort(settings, "user_one", "exec_1", event_type="failed", status="failed")


def test_get_event_sequence_scopes_to_owner_and_execution(settings, database):
    database.execution_events.find_one.return_value = {"sequence": 5}
    assert get_event_sequence(settings, "user_one", "exec_1", "ev_x") == 5
    query = database.execution_events.find_one.call_args.args[0]
    assert query == {"id": "ev_x", "user_id": "user_one", "execution_id": "exec_1"}
    database.execution_events.find_one.return_value = None
    assert get_event_sequence(settings, "user_one", "exec_1", "ev_foreign") is None


def test_list_events_replays_after_sequence_in_order(settings, database):
    database.execution_events.find.return_value.sort.return_value.limit.return_value = [
        {"id": "ev_2", "sequence": 2},
        {"id": "ev_3", "sequence": 3},
    ]
    rows = list_events(settings, "user_one", "exec_1", after_sequence=1, limit=50)
    assert [row["sequence"] for row in rows] == [2, 3]
    query = database.execution_events.find.call_args.args[0]
    assert query["sequence"] == {"$gt": 1}
    assert query["user_id"] == "user_one"
    assert query["execution_id"] == "exec_1"


def test_count_events_scopes_to_owner(settings, database):
    database.execution_events.count_documents.return_value = 2
    assert count_events(settings, "user_one", "exec_1") == 2
    query = database.execution_events.count_documents.call_args.args[0]
    assert query == {"user_id": "user_one", "execution_id": "exec_1"}
