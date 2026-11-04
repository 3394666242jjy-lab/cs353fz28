"""Dispatch outbox: MongoDB-persisted task commands with at-least-once publish.

Per dev/03 lifecycle #5: business writes and their dispatch command are
committed in the same transaction (callers pass ``session``); the publisher
marks the row published only *after* the broker accepts it, so a crash in
between causes a duplicate publish, never a lost one. Workers stay idempotent
by claiming executions through guarded state_version transitions, which is
what makes duplicate delivery safe. A sweeper re-publishes rows whose first
publish attempt never completed.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from app.config import Settings
from app.services.database import database_connection

logger = logging.getLogger(__name__)

COMMAND_TASKS = {
    # command -> (Celery task name, argument builder uses the row fields)
    "start_plan": "careerpilot.run_plan",
    "resume": "careerpilot.resume_execution",
}


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M2 indexes per dev/03; safe to repeat on every call."""
    database.dispatch_events.create_index("id", unique=True)
    database.dispatch_events.create_index([("status", 1), ("created_at", 1)])


def enqueue_dispatch(
    database: Database[dict[str, Any]],
    *,
    user_id: str,
    execution_id: str,
    command: str,
    expected_state_version: int,
    input_ref: str | None = None,
    session: Any = None,
) -> str:
    """Insert one pending dispatch row inside the caller's transaction."""
    if command not in COMMAND_TASKS:
        raise ValueError(f"Unknown dispatch command: {command}")
    _ensure_indexes(database)
    dispatch_id = f"disp_{uuid4().hex}"
    database.dispatch_events.insert_one(
        {
            "id": dispatch_id,
            "user_id": user_id,
            "execution_id": execution_id,
            "command": command,
            "expected_state_version": expected_state_version,
            "input_ref": input_ref,
            "status": "pending",
            "published_at": None,
            "publish_attempts": 0,
            "created_at": datetime.now(UTC),
        },
        session=session,
    )
    return dispatch_id


def _send(dispatch: dict[str, Any]) -> None:
    """Deliver one dispatch row to the broker; raises on broker failure."""
    from app.worker import celery_app

    celery_app.send_task(
        COMMAND_TASKS[dispatch["command"]],
        args=[
            dispatch["id"],
            dispatch["user_id"],
            dispatch["execution_id"],
            dispatch["expected_state_version"],
        ],
    )


def publish_dispatch(settings: Settings, dispatch_id: str) -> bool:
    """Publish one pending row now; returns False when already published.

    The row is marked published only after the broker accepts the message;
    if the process dies in between, the sweeper republishes and the worker's
    state_version claim discards the duplicate.
    """
    try:
        with database_connection(settings) as database:
            _ensure_indexes(database)
            row = database.dispatch_events.find_one({"id": dispatch_id}, {"_id": 0})
            if row is None or row["status"] != "pending":
                return False
            _send(row)
            database.dispatch_events.update_one(
                {"id": dispatch_id, "status": "pending"},
                {
                    "$set": {"status": "published", "published_at": datetime.now(UTC)},
                    "$inc": {"publish_attempts": 1},
                },
            )
        return True
    except Exception:  # noqa: BLE001 - row stays pending; sweeper retries
        logger.exception("Could not publish dispatch %s; leaving it pending", dispatch_id)
        with database_connection(settings) as database:
            database.dispatch_events.update_one(
                {"id": dispatch_id, "status": "pending"},
                {"$inc": {"publish_attempts": 1}},
            )
        return False


def sweep_pending_dispatches(settings: Settings, *, limit: int = 50) -> int:
    """Republish pending rows; returns how many were published this round."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.dispatch_events.find({"status": "pending"}, {"_id": 0, "id": 1})
            .sort("created_at", 1)
            .limit(limit)
        )
    published = 0
    for row in rows:
        if publish_dispatch(settings, row["id"]):
            published += 1
    return published


def get_dispatch(settings: Settings, dispatch_id: str) -> dict[str, Any] | None:
    """Worker-side lookup of one dispatch row (input_ref, command)."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.dispatch_events.find_one({"id": dispatch_id}, {"_id": 0})
    return row
