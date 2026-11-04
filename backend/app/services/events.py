"""Execution event persistence for SSE replay (dev/03: execution_events).

Events are append-only rows with a per-execution ``sequence`` allocated by an
atomic counter on the execution itself, so replay ordering never depends on
wall-clock ties. The execution's ``latest_event_id`` is maintained in the same
update, giving reconnecting clients a snapshot cursor. Retention is bounded:
once old events are pruned, replay from a pruned cursor is rejected and the
client falls back to the execution snapshot (410 EVENT_HISTORY_EXPIRED).
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.services.database import database_connection

EventType = Literal["stage", "waiting_for_input", "completed", "failed"]
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"completed", "failed"})

logger = logging.getLogger(__name__)


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M2 indexes per dev/03; safe to repeat on every call."""
    database.execution_events.create_index("id", unique=True)
    database.execution_events.create_index([("execution_id", 1), ("sequence", 1)], unique=True)


def append_event(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    event_type: EventType,
    status: str,
    stage: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Append one event and advance the execution cursor atomically.

    Returns the event id, or None when the execution does not exist (the
    business transition already failed in that case, so losing the event
    cannot hide a state change). Retention pruning keeps the newest
    ``event_history_retention_count`` events per execution.
    """
    event_id = f"ev_{uuid4().hex}"
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        cursor = database.executions.find_one_and_update(
            {"id": execution_id, "user_id": user_id},
            {"$inc": {"event_seq": 1}, "$set": {"latest_event_id": event_id}},
            projection={"_id": 0, "event_seq": 1},
            return_document=ReturnDocument.AFTER,
        )
        if cursor is None:
            return None
        sequence = cursor["event_seq"]
        database.execution_events.insert_one(
            {
                "id": event_id,
                "user_id": user_id,
                "execution_id": execution_id,
                "sequence": sequence,
                "event_type": event_type,
                "status": status,
                "stage": stage,
                "payload": payload or {},
                "created_at": now,
            }
        )
        retention_floor = sequence - settings.event_history_retention_count
        if retention_floor > 0:
            database.execution_events.delete_many(
                {"execution_id": execution_id, "sequence": {"$lte": retention_floor}}
            )
    return event_id


def append_event_best_effort(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    event_type: EventType,
    status: str,
    stage: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append an event without ever breaking the business flow.

    Event durability matters for SSE replay, but a client can always fall
    back to the execution snapshot, so a transient event-write outage is
    logged instead of failing the transition it describes.
    """
    try:
        append_event(
            settings,
            user_id,
            execution_id,
            event_type=event_type,
            status=status,
            stage=stage,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 - see docstring; snapshot is the fallback
        logger.exception("Could not persist execution event %s for %s", event_type, execution_id)


def get_event_sequence(
    settings: Settings, user_id: str, execution_id: str, event_id: str
) -> int | None:
    """Return the sequence of a replay anchor owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.execution_events.find_one(
            {"id": event_id, "user_id": user_id, "execution_id": execution_id},
            {"_id": 0, "sequence": 1},
        )
    return row["sequence"] if row is not None else None


def count_events(settings: Settings, user_id: str, execution_id: str) -> int:
    """Number of retained events; 0 with a terminal execution means snapshot only."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        return database.execution_events.count_documents(
            {"user_id": user_id, "execution_id": execution_id}
        )


def list_events(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return retained events of one execution in sequence order."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.execution_events.find(
                {
                    "user_id": user_id,
                    "execution_id": execution_id,
                    "sequence": {"$gt": after_sequence},
                },
                {"_id": 0},
            )
            .sort("sequence", 1)
            .limit(limit)
        )
    return rows
