"""Execution inputs: the idempotency anchor for resume/confirm (dev/03).

Every user answer that resumes a paused execution is stored exactly once per
(execution_id, interrupt_id). Replaying the same request returns the recorded
acceptance; replaying the same interrupt with a *different* answer is a
conflict, so a retried request can never silently overwrite a recorded one.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.services.database import database_connection

InputKind = Literal["clarification", "plan_confirmation"]


class InputConflictError(RuntimeError):
    """Same interrupt replayed with a different answer (409 INPUT_CONFLICT)."""


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M2 indexes per dev/03; safe to repeat on every call.

    Never call this inside an open transaction; use
    :func:`ensure_input_indexes` before opening one.
    """
    database.execution_inputs.create_index("id", unique=True)
    database.execution_inputs.create_index([("execution_id", 1), ("interrupt_id", 1)], unique=True)


def ensure_input_indexes(settings: Settings) -> None:
    """Create the indexes outside any transaction; call before opening one."""
    with database_connection(settings) as database:
        _ensure_indexes(database)


def answer_content_hash(answer: dict[str, Any]) -> str:
    """Stable digest of the user answer; key order must not change the result."""
    canonical = json.dumps(answer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"


def record_execution_input(
    database: Database[dict[str, Any]],
    *,
    user_id: str,
    execution_id: str,
    interrupt_id: str,
    checkpoint_id: str,
    kind: InputKind,
    answer: dict[str, Any],
    document_versions: list[dict[str, Any]] | None = None,
    session: Any = None,
) -> str:
    """Persist one input inside the caller's transaction; returns its id.

    Idempotent: a replay with identical content returns the existing row id.
    A replay with different content raises InputConflictError.

    The caller must have run :func:`ensure_input_indexes` beforehand.
    """
    content_hash = answer_content_hash(answer)
    input_id = f"inp_{uuid4().hex}"
    try:
        database.execution_inputs.insert_one(
            {
                "id": input_id,
                "user_id": user_id,
                "execution_id": execution_id,
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "kind": kind,
                "answer": answer,
                "document_versions": document_versions or [],
                "content_hash": content_hash,
                "created_at": datetime.now(UTC),
            },
            session=session,
        )
    except DuplicateKeyError:
        existing = database.execution_inputs.find_one(
            {"execution_id": execution_id, "interrupt_id": interrupt_id}, {"_id": 0}
        )
        if existing is not None and existing["content_hash"] == content_hash:
            return existing["id"]
        raise InputConflictError(interrupt_id) from None
    return input_id


def get_execution_input(
    settings: Settings, user_id: str, execution_id: str, interrupt_id: str
) -> dict[str, Any] | None:
    """Return one recorded input owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.execution_inputs.find_one(
            {
                "user_id": user_id,
                "execution_id": execution_id,
                "interrupt_id": interrupt_id,
            },
            {"_id": 0},
        )
    return row


def load_input_by_id(settings: Settings, user_id: str, input_id: str) -> dict[str, Any] | None:
    """Worker-side lookup of the resume payload referenced by a dispatch row."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.execution_inputs.find_one({"id": input_id, "user_id": user_id}, {"_id": 0})
    return row
