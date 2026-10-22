"""Executions are MongoDB's authoritative task state (dev/03); Celery only dispatches."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field
from pymongo.database import Database

from app.config import Settings
from app.schemas.common import ContractModel
from app.schemas.resources import ExecutionError, ExecutionResult, PendingInput
from app.services.database import database_connection


class ExecutionCreate(ContractModel):
    """Internal creation payload; resource ownership is validated by the caller."""

    type: Literal["index", "analysis", "plan"]
    resource_id: str = Field(min_length=1)


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M1 indexes per dev/03; safe to repeat on every call."""
    database.executions.create_index("id", unique=True)
    database.executions.create_index([("user_id", 1), ("created_at", -1)])
    database.executions.create_index([("status", 1), ("created_at", -1)])


def create_execution(settings: Settings, user_id: str, payload: ExecutionCreate) -> str:
    """Queue a new execution at state_version 1."""
    execution_id = f"exec_{uuid4().hex}"
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        database.executions.insert_one(
            {
                "id": execution_id,
                "user_id": user_id,
                "type": payload.type,
                "resource_id": payload.resource_id,
                "status": "queued",
                "state_version": 1,
                "result_type": None,
                "result_id": None,
                "pending_input": None,
                "latest_event_id": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    return execution_id


def get_execution(settings: Settings, user_id: str, execution_id: str) -> ExecutionResult | None:
    """Return one execution owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        document = database.executions.find_one(
            {"id": execution_id, "user_id": user_id}, {"_id": 0}
        )
    if document is None:
        return None
    error = document.get("error")
    pending_input = document.get("pending_input")
    return ExecutionResult(
        id=document["id"],
        type=document["type"],
        status=document["status"],
        state_version=document["state_version"],
        result_type=document.get("result_type"),
        result_id=document.get("result_id"),
        pending_input=PendingInput.model_validate(pending_input) if pending_input else None,
        error=ExecutionError.model_validate(error) if error else None,
        latest_event_id=document.get("latest_event_id"),
    )


def transition_execution(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    expected_state_version: int,
    status: Literal["running", "succeeded", "failed"],
    result_type: Literal["document", "analysis", "plan"] | None = None,
    result_id: str | None = None,
    error: ExecutionError | None = None,
) -> bool:
    """Guarded transition; returns False on stale state version or wrong owner.

    Only waiting_for_input carries pending_input and only failed carries an
    error, matching ExecutionResult's validators.
    """
    # Validate state semantics against the shared contract before writing.
    ExecutionResult(
        id=execution_id,
        type="index",
        status=status,
        state_version=expected_state_version + 1,
        result_type=result_type,
        result_id=result_id,
        error=error,
    )
    update: dict[str, Any] = {
        "status": status,
        "result_type": result_type,
        "result_id": result_id,
        "error": error.model_dump() if error else None,
        "updated_at": datetime.now(UTC),
    }
    with database_connection(settings) as database:
        _ensure_indexes(database)
        result = database.executions.update_one(
            {
                "id": execution_id,
                "user_id": user_id,
                "state_version": expected_state_version,
            },
            {"$set": update, "$inc": {"state_version": 1}},
        )
    return result.matched_count == 1
