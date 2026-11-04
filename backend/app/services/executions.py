"""Executions are MongoDB's authoritative task state (dev/03); Celery only dispatches."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field
from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import Settings
from app.schemas.common import ContractModel
from app.schemas.resources import ExecutionError, ExecutionResult, PendingInput
from app.services.database import database_connection
from app.services.events import append_event


class ExecutionCreate(ContractModel):
    """Internal creation payload; resource ownership is validated by the caller."""

    type: Literal["index", "analysis", "plan"]
    resource_id: str = Field(min_length=1)


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M1 indexes per dev/03; safe to repeat on every call."""
    database.executions.create_index("id", unique=True)
    database.executions.create_index([("user_id", 1), ("created_at", -1)])
    database.executions.create_index([("status", 1), ("created_at", -1)])
    database.executions.create_index(
        [("user_id", 1), ("type", 1), ("resource_id", 1), ("status", 1)]
    )


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
    if document.get("status") in {"queued", "running"}:
        document = _recover_expired(settings, user_id, execution_id, document["status"]) or document
    if document.get("status") == "failed" and document.get("resource_id"):
        _reconcile_failed_resource(settings, user_id, document)
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
                "recovery_claim": {"$exists": False},
                "status": {
                    "$in": ["queued"]
                    if status == "running"
                    else ["running"]
                    if status == "succeeded"
                    else ["queued", "running"]
                },
            },
            {"$set": update, "$inc": {"state_version": 1}},
        )
    if result.matched_count == 1:
        append_event(
            settings,
            user_id,
            execution_id,
            event_type="status_change",
            payload={
                "status": status,
                "state_version": expected_state_version + 1,
                "result_type": result_type,
                "result_id": result_id,
                "error": error.model_dump() if error else None,
            },
        )
    return result.matched_count == 1


def heartbeat_execution(settings: Settings, user_id: str, execution_id: str) -> None:
    """Renew a running worker lease; never revive a claimed or finished task."""
    with database_connection(settings) as database:
        database.executions.update_one(
            {
                "id": execution_id,
                "user_id": user_id,
                "status": "running",
                "recovery_claim": {"$exists": False},
            },
            {"$set": {"updated_at": datetime.now(UTC)}},
        )


def _recover_expired(
    settings: Settings, user_id: str, execution_id: str, status: str
) -> dict[str, Any] | None:
    """Reconcile an expired worker with its resource using guarded writes.

    Claiming blocks heartbeats and regular execution transitions. A ready
    resource wins over timeout; otherwise claiming its pending state prevents
    the old worker publishing after recovery. Claims can be reclaimed after
    a second timeout if the recovering API process itself dies.
    """
    now = datetime.now(UTC)
    timeout = (
        settings.execution_queue_timeout_seconds
        if status == "queued"
        else settings.execution_lease_seconds
    )
    cutoff = now - timedelta(seconds=timeout)
    token = uuid4().hex
    with database_connection(settings) as database:
        claim = database.executions.find_one_and_update(
            {
                "id": execution_id,
                "user_id": user_id,
                "status": status,
                "updated_at": {"$lte": cutoff},
                "$or": [
                    {"recovery_claim": {"$exists": False}},
                    {"recovery_claimed_at": {"$lte": cutoff}},
                ],
            },
            {"$set": {"recovery_claim": token, "recovery_claimed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if claim is None:
            return database.executions.find_one(
                {"id": execution_id, "user_id": user_id}, {"_id": 0}
            )
        resource_id = claim["resource_id"]
        message = "后台任务中断或超时，请重新提交。"
        resource_type = claim["type"]
        if resource_type == "index":
            collection = database.documents
            pending = "uploaded"
            result_type = "document"
            resource_filter = {"id": resource_id, "user_id": user_id, "deleted_at": None}
        elif resource_type == "analysis":
            collection = database.analyses
            pending = "pending"
            result_type = "analysis"
            resource_filter = {"id": resource_id, "user_id": user_id}
        else:
            collection = None
            pending = ""
            result_type = None
            resource_filter = {}
        if collection is not None:
            collection.update_one(
                {**resource_filter, "status": pending},
                {"$set": {"status": "failed", "error": message, "updated_at": now}},
            )
            resource = collection.find_one(resource_filter, {"status": 1})
        else:
            resource = None
        success = bool(resource and resource.get("status") == "ready")
        status = "succeeded" if success else "failed"
        changes = {
            "status": status,
            "updated_at": now,
            "result_type": result_type if success else None,
            "result_id": resource_id if success else None,
            "error": None if success else {"code": "WORKER_INTERRUPTED", "message": message},
        }
        return database.executions.find_one_and_update(
            {
                "id": execution_id,
                "user_id": user_id,
                "status": claim["status"],
                "recovery_claim": token,
            },
            {
                "$set": changes,
                "$inc": {"state_version": 1},
                "$unset": {"recovery_claim": "", "recovery_claimed_at": ""},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )


def _reconcile_failed_resource(settings: Settings, user_id: str, execution: dict[str, Any]) -> None:
    """Repair a crash between marking execution failed and updating its resource.

    Only unfinished resources can be marked failed; a published result is never
    overwritten. M1 creates a new document/analysis ID for every new operation.
    """
    resource_type = execution.get("type")
    resource_id = execution.get("resource_id")
    if not resource_id or resource_type not in {"index", "analysis"}:
        return
    error = execution.get("error") or {}
    message = error.get("message") or "后台任务失败，请重新提交。"
    with database_connection(settings) as database:
        if resource_type == "index":
            collection = database.documents
            resource_filter = {
                "id": resource_id,
                "user_id": user_id,
                "deleted_at": None,
                "status": "uploaded",
            }
        else:
            collection = database.analyses
            resource_filter = {"id": resource_id, "user_id": user_id, "status": "pending"}
        collection.update_one(
            resource_filter,
            {"$set": {"status": "failed", "error": message, "updated_at": datetime.now(UTC)}},
        )
        if execution.get("id"):
            database.executions.update_one(
                {"id": execution["id"], "user_id": user_id, "status": "failed"},
                {"$set": {"resource_reconciled_at": datetime.now(UTC)}},
            )


def sweep_expired_executions(settings: Settings, *, limit: int = 100) -> None:
    """Bounded background reconciliation; CAS permits concurrent API instances."""
    now = datetime.now(UTC)

    def expired(status: str, timeout: int) -> dict[str, Any]:
        cutoff = now - timedelta(seconds=timeout)
        return {
            "status": status,
            "updated_at": {"$lte": cutoff},
            "$or": [
                {"recovery_claim": {"$exists": False}},
                {"recovery_claimed_at": {"$lte": cutoff}},
            ],
        }

    with database_connection(settings) as database:
        _ensure_indexes(database)
        candidates = list(
            database.executions.find(
                {
                    "$or": [
                        expired("queued", settings.execution_queue_timeout_seconds),
                        expired("running", settings.execution_lease_seconds),
                        {
                            "status": "failed",
                            "type": {"$in": ["index", "analysis"]},
                            "resource_reconciled_at": {"$exists": False},
                            "resource_id": {"$type": "string"},
                        },
                    ]
                },
                {"id": 1, "user_id": 1, "type": 1, "resource_id": 1, "status": 1, "error": 1},
            )
            .sort("updated_at", 1)
            .limit(limit)
        )
    for execution in candidates:
        if execution["status"] == "failed":
            _reconcile_failed_resource(settings, execution["user_id"], execution)
        else:
            _recover_expired(settings, execution["user_id"], execution["id"], execution["status"])


def recover_document_execution(settings: Settings, user_id: str, document_id: str) -> None:
    """Reconcile indexing executions during document polling, including interrupted failures."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        executions = list(
            database.executions.find(
                {
                    "user_id": user_id,
                    "type": "index",
                    "resource_id": document_id,
                    "status": {"$in": ["queued", "running", "failed"]},
                },
                {"id": 1, "type": 1, "resource_id": 1, "status": 1, "error": 1},
            )
        )
    for execution in executions:
        if execution["status"] == "failed":
            _reconcile_failed_resource(settings, user_id, execution)
        else:
            _recover_expired(settings, user_id, execution["id"], execution["status"])
