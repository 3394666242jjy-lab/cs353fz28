"""Opt-in M2 checks against a real MongoDB replica set (transactions required).

Uses a dedicated database that is dropped after each test; never touches the
development database. Run locally with ``RUN_MONGO_M2_TESTS=1`` after
``docker compose up -d`` (the compose mongod runs as replica set rs0).
"""

import os
from datetime import date
from uuid import uuid4

import pytest

from app.config import Settings
from app.services import dispatch as dispatch_service
from app.services.database import database_connection, transactional_connection
from app.services.dispatch import (
    enqueue_dispatch,
    get_dispatch,
    publish_dispatch,
    sweep_pending_dispatches,
)
from app.services.events import append_event, count_events, get_event_sequence, list_events
from app.services.executions import ExecutionCreate, insert_execution
from app.services.inputs import get_execution_input
from app.services.plans import (
    PlanStateError,
    attach_plan_execution,
    confirm_plan,
    create_plan_record,
    get_plan,
    list_plan_tasks,
    save_plan_draft,
)

TEST_DATABASE = "careerpilot_m2_test"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MONGO_M2_TESTS") != "1",
    reason="Needs local MongoDB replica set (docker compose up -d)",
)


@pytest.fixture
def settings():
    instance = Settings(_env_file=None, app_env="test", mongodb_database=TEST_DATABASE)
    yield instance
    with database_connection(instance) as database:
        database.client.drop_database(TEST_DATABASE)


def _draft_plan(settings, user_id: str) -> tuple[str, str]:
    """Create a draft plan with a waiting execution; return (plan_id, execution_id)."""
    with transactional_connection(settings) as (database, session):
        plan_id = create_plan_record(
            database,
            user_id=user_id,
            analysis_id="analysis_1",
            deadline=date(2099, 1, 2),
            timezone="Asia/Shanghai",
            daily_minutes=60,
            session=session,
        )
        execution_id = insert_execution(
            database,
            user_id,
            ExecutionCreate(type="plan", resource_id=plan_id),
            session=session,
        )
        attach_plan_execution(database, user_id, plan_id, execution_id, session=session)
    assert save_plan_draft(
        settings,
        user_id,
        plan_id,
        draft_tasks=[
            {
                "title": "复习索引",
                "requirement_ids": ["req_01"],
                "scheduled_date": "2099-01-01",
                "estimated_minutes": 45,
                "source_refs": [],
            }
        ],
    )
    with database_connection(settings) as database:
        database.executions.update_one(
            {"id": execution_id, "user_id": user_id},
            {
                "$set": {
                    "status": "waiting_for_input",
                    "pending_input": {
                        "interrupt_id": "int_1",
                        "checkpoint_id": "ckpt_1",
                        "kind": "plan_confirmation",
                        "plan_id": plan_id,
                    },
                }
            },
        )
    return plan_id, execution_id


def test_confirm_plan_commits_atomically_and_replays(settings):
    user_id = f"user_{uuid4().hex}"
    plan_id, execution_id = _draft_plan(settings, user_id)

    outcome = confirm_plan(settings, user_id, plan_id, analysis_is_fresh=lambda aid: True)

    assert outcome["resumed"] is True
    assert outcome["plan"]["status"] == "confirmed"
    assert outcome["plan"]["confirmed_at"] is not None
    tasks = outcome["tasks"]
    assert [task["id"] for task in tasks] == [f"{plan_id}_t001"]
    assert tasks[0]["status"] == "todo"

    # The input is recorded under the interrupt identity.
    recorded = get_execution_input(settings, user_id, execution_id, "int_1")
    assert recorded is not None
    assert recorded["answer"] == {"confirmed": True, "plan_id": plan_id}
    assert recorded["kind"] == "plan_confirmation"

    # The execution left the waiting state and carries no pending input.
    with database_connection(settings) as database:
        execution = database.executions.find_one({"id": execution_id}, {"_id": 0})
        assert execution["status"] == "queued"
        assert "pending_input" not in execution
        dispatches = list(database.dispatch_events.find({"execution_id": execution_id}))
    assert len(dispatches) == 1
    assert dispatches[0]["command"] == "resume"
    assert dispatches[0]["input_ref"] == recorded["id"]
    assert dispatches[0]["status"] in {"pending", "published"}

    # Replay: same confirmed outcome, no duplicate tasks/inputs/dispatches.
    replay = confirm_plan(settings, user_id, plan_id, analysis_is_fresh=lambda aid: True)
    assert replay["resumed"] is False
    assert [task["id"] for task in replay["tasks"]] == [f"{plan_id}_t001"]
    with database_connection(settings) as database:
        assert database.plan_tasks.count_documents({"plan_id": plan_id}) == 1
        assert database.execution_inputs.count_documents({"execution_id": execution_id}) == 1
        assert database.dispatch_events.count_documents({"execution_id": execution_id}) == 1


def test_confirm_plan_rejects_other_users(settings):
    owner_id = f"user_{uuid4().hex}"
    plan_id, _ = _draft_plan(settings, owner_id)

    other_id = f"user_{uuid4().hex}"
    assert get_plan(settings, other_id, plan_id) is None
    assert list_plan_tasks(settings, other_id, plan_id) == []
    with pytest.raises(PlanStateError, match="计划不存在"):
        confirm_plan(settings, other_id, plan_id, analysis_is_fresh=lambda aid: True)


def test_events_have_gapless_sequences_and_retention(settings):
    settings.event_history_retention_count = 3
    user_id = f"user_{uuid4().hex}"
    with database_connection(settings) as database:
        execution_id = insert_execution(
            database, user_id, ExecutionCreate(type="analysis", resource_id="analysis_1")
        )

    event_ids = [
        append_event(settings, user_id, execution_id, event_type="stage", status="running")
        for _ in range(6)
    ]
    assert all(event_id is not None for event_id in event_ids)

    # Retention keeps the newest 3 events with gapless sequences.
    assert count_events(settings, user_id, execution_id) == 3
    retained = list_events(settings, user_id, execution_id)
    assert [row["sequence"] for row in retained] == [4, 5, 6]
    assert get_event_sequence(settings, user_id, execution_id, event_ids[-1]) == 6
    replayed = list_events(settings, user_id, execution_id, after_sequence=4)
    assert [row["sequence"] for row in replayed] == [5, 6]

    # Events are invisible to other users.
    other_id = f"user_{uuid4().hex}"
    assert list_events(settings, other_id, execution_id) == []
    assert get_event_sequence(settings, other_id, execution_id, event_ids[-1]) is None
    assert append_event(settings, other_id, execution_id, event_type="stage", status="x") is None


def test_dispatch_outbox_publish_failure_and_sweep(settings, monkeypatch):
    user_id = f"user_{uuid4().hex}"
    with database_connection(settings) as database:
        execution_id = insert_execution(
            database, user_id, ExecutionCreate(type="plan", resource_id="plan_x")
        )

    # The dispatch row commits inside the same transaction as the business write.
    with transactional_connection(settings) as (database, session):
        dispatch_id = enqueue_dispatch(
            database,
            user_id=user_id,
            execution_id=execution_id,
            command="start_plan",
            expected_state_version=1,
            session=session,
        )
    row = get_dispatch(settings, dispatch_id)
    assert row["status"] == "pending"

    # A broker failure leaves the row pending and counts the attempt.
    def broken_send(row):
        raise ConnectionError("broker down")

    monkeypatch.setattr(dispatch_service, "_send", broken_send)
    assert publish_dispatch(settings, dispatch_id) is False
    row = get_dispatch(settings, dispatch_id)
    assert row["status"] == "pending"
    assert row["publish_attempts"] == 1

    # Once the broker recovers, the sweeper republishes; republish is idempotent.
    sent = []
    monkeypatch.setattr(dispatch_service, "_send", lambda row: sent.append(row["id"]))
    assert sweep_pending_dispatches(settings) == 1
    assert sent == [dispatch_id]
    row = get_dispatch(settings, dispatch_id)
    assert row["status"] == "published"
    assert publish_dispatch(settings, dispatch_id) is False
    assert sweep_pending_dispatches(settings) == 0
