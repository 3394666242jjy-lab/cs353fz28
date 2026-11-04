"""Task event persistence for SSE.

Events are append-only rows keyed by execution; the API serves them as an
SSE stream with Last-Event-ID replay. A snapshot of the execution state is
embedded so a reconnecting client can catch up without N round-trips.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.services.database import database_connection


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    database.events.create_index("id", unique=True)
    database.events.create_index([("user_id", 1), ("execution_id", 1), ("created_at", -1)])
    database.events.create_index([("user_id", 1), ("created_at", -1)])


def append_event(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Append one event; returns its id. Never raises to the caller (best effort)."""
    event_id = f"ev_{uuid4().hex}"
    now = datetime.now(UTC)
    try:
        with database_connection(settings) as database:
            _ensure_indexes(database)
            database.events.insert_one(
                {
                    "id": event_id,
                    "user_id": user_id,
                    "execution_id": execution_id,
                    "type": event_type,
                    "payload": payload,
                    "created_at": now,
                }
            )
    except Exception:
        # Event logging must not break the business flow; SSE consumers can
        # fall back to polling the execution state.
        pass
    return event_id


def list_events(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    after_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return events for one execution, optionally after a given event id."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        query: dict[str, Any] = {"user_id": user_id, "execution_id": execution_id}
        if after_id is not None:
            anchor = database.events.find_one({"id": after_id, "user_id": user_id}, {"_id": 0, "created_at": 1})
            if anchor is not None:
                query["created_at"] = {"$gt": anchor["created_at"]}
        rows = list(
            database.events.find(query, {"_id": 0})
            .sort("created_at", 1)
            .limit(limit)
        )
    return rows
