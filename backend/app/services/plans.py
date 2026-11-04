"""Formal plan persistence.

A plan starts as a draft produced by the agent (member D) while the execution
pauses at waiting_for_input. The user confirms it via POST /executions/{id}/confirm;
this module persists the confirmed plan as an immutable, owner-scoped record and
idempotently resumes the agent execution.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from app.config import Settings
from app.services.database import database_connection


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    database.plans.create_index("id", unique=True)
    database.plans.create_index([("user_id", 1), ("created_at", -1)])
    database.plans.create_index([("user_id", 1), ("execution_id", 1)], unique=True)


def create_confirmed_plan(
    settings: Settings,
    user_id: str,
    *,
    execution_id: str,
    analysis_id: str,
    draft: dict[str, Any],
    confirmed_at: datetime | None = None,
) -> str:
    """Persist a confirmed plan. Idempotent per (user, execution_id)."""
    plan_id = f"plan_{uuid4().hex}"
    now = confirmed_at or datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        existing = database.plans.find_one(
            {"user_id": user_id, "execution_id": execution_id}, {"_id": 0}
        )
        if existing is not None:
            return existing["id"]
        database.plans.insert_one(
            {
                "id": plan_id,
                "user_id": user_id,
                "execution_id": execution_id,
                "analysis_id": analysis_id,
                "draft": draft,
                "status": "confirmed",
                "created_at": now,
                "updated_at": now,
            }
        )
    return plan_id


def get_plan(
    settings: Settings, user_id: str, plan_id: str
) -> dict[str, Any] | None:
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.plans.find_one({"id": plan_id, "user_id": user_id}, {"_id": 0})
    return row
