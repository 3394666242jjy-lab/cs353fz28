"""Tests for the dispatch outbox: at-least-once publish semantics (issue #24)."""

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.dispatch import (
    enqueue_dispatch,
    get_dispatch,
    publish_dispatch,
    sweep_pending_dispatches,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.dispatch.database_connection", connection)
    return connection.return_value.__enter__.return_value


def test_enqueue_dispatch_rejects_unknown_command(database):
    with pytest.raises(ValueError, match="Unknown dispatch command"):
        enqueue_dispatch(
            database,
            user_id="user_one",
            execution_id="exec_1",
            command="explode",
            expected_state_version=1,
        )


def test_enqueue_dispatch_inserts_pending_row(database):
    dispatch_id = enqueue_dispatch(
        database,
        user_id="user_one",
        execution_id="exec_1",
        command="resume",
        expected_state_version=3,
        input_ref="inp_1",
        session="session-x",
    )
    row = database.dispatch_events.insert_one.call_args.args[0]
    assert row["id"] == dispatch_id
    assert row["command"] == "resume"
    assert row["status"] == "pending"
    assert row["publish_attempts"] == 0
    assert row["input_ref"] == "inp_1"
    assert database.dispatch_events.insert_one.call_args.kwargs["session"] == "session-x"


def test_publish_dispatch_marks_published_only_after_send(settings, database, monkeypatch):
    database.dispatch_events.find_one.return_value = {
        "id": "disp_1",
        "user_id": "user_one",
        "execution_id": "exec_1",
        "command": "resume",
        "expected_state_version": 2,
        "input_ref": "inp_1",
        "status": "pending",
    }
    sent = {}
    celery = MagicMock()
    celery.send_task.side_effect = lambda name, args: sent.update({"name": name, "args": args})
    monkeypatch.setattr("app.worker.celery_app", celery)
    assert publish_dispatch(settings, "disp_1") is True
    assert sent["name"] == "careerpilot.resume_execution"
    assert sent["args"] == ["disp_1", "user_one", "exec_1", 2]
    update_query, update = database.dispatch_events.update_one.call_args.args
    assert update_query == {"id": "disp_1", "status": "pending"}
    assert update["$set"]["status"] == "published"
    assert update["$inc"] == {"publish_attempts": 1}


def test_publish_dispatch_skips_non_pending_rows(settings, database, monkeypatch):
    database.dispatch_events.find_one.return_value = {"id": "disp_1", "status": "published"}
    celery = MagicMock()
    monkeypatch.setattr("app.worker.celery_app", celery)
    assert publish_dispatch(settings, "disp_1") is False
    celery.send_task.assert_not_called()


def test_publish_failure_leaves_row_pending_for_sweeper(settings, database, monkeypatch):
    database.dispatch_events.find_one.return_value = {
        "id": "disp_1",
        "user_id": "user_one",
        "execution_id": "exec_1",
        "command": "start_plan",
        "expected_state_version": 1,
        "status": "pending",
    }
    celery = MagicMock()
    celery.send_task.side_effect = RuntimeError("broker down")
    monkeypatch.setattr("app.worker.celery_app", celery)
    assert publish_dispatch(settings, "disp_1") is False
    _, update = database.dispatch_events.update_one.call_args.args
    # Attempts are counted; status stays pending so the sweeper republishes.
    assert update["$inc"] == {"publish_attempts": 1}
    assert "$set" not in update


def test_sweep_republishes_pending_rows(settings, database, monkeypatch):
    database.dispatch_events.find.return_value.sort.return_value.limit.return_value = [
        {"id": "disp_1"},
        {"id": "disp_2"},
    ]
    outcomes = iter([True, False])
    monkeypatch.setattr(
        "app.services.dispatch.publish_dispatch", lambda settings, dispatch_id: next(outcomes)
    )
    assert sweep_pending_dispatches(settings) == 1


def test_get_dispatch_returns_row(settings, database):
    database.dispatch_events.find_one.return_value = {"id": "disp_1", "input_ref": "inp_1"}
    assert get_dispatch(settings, "disp_1")["input_ref"] == "inp_1"
