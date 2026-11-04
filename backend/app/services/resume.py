"""Resume a paused execution from a user answer (dev/03/dev/04 M2).

The answer, the waiting→queued transition and the resume dispatch commit in
one transaction: a replayed request either finds its recorded input and
returns the same acceptance, or conflicts loudly (409 INPUT_CONFLICT) — a
retry can never double-apply an answer. Stale interrupts/checkpoints are
rejected before any write (409 STALE_CHECKPOINT).
"""

from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.schemas.resources import ResumeAnswer
from app.services.database import transactional_connection
from app.services.dispatch import enqueue_dispatch, publish_dispatch
from app.services.inputs import (
    InputConflictError,
    answer_content_hash,
    get_execution_input,
    record_execution_input,
)


class ResumeError(RuntimeError):
    """Validation failure with an HTTP-mappable code and message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def resume_execution(
    settings: Settings,
    user_id: str,
    execution_id: str,
    *,
    interrupt_id: str,
    checkpoint_id: str,
    answer: ResumeAnswer,
    document_versions: list[dict[str, Any]],
) -> str:
    """Record the answer and enqueue the resume; returns the execution id.

    ``document_versions`` carries the caller-pinned versions of any newly
    supplied documents (already ownership/readiness-checked by the caller);
    they become part of the idempotency hash, so a replay with different
    documents is an INPUT_CONFLICT, not a silent rewrite.
    """
    answer_payload = {
        "text": answer.text,
        "continue_without_evidence": answer.continue_without_evidence,
        "document_ids": list(answer.document_ids),
    }
    content_hash = answer_content_hash({**answer_payload, "document_versions": document_versions})
    existing = get_execution_input(settings, user_id, execution_id, interrupt_id)
    if existing is not None:
        # Idempotent fast path: same recorded answer → same acceptance.
        if existing["content_hash"] == content_hash:
            return execution_id
        raise InputConflictError(interrupt_id)
    now = datetime.now(UTC)
    dispatch_id: str | None = None
    with transactional_connection(settings) as (database, session):
        execution = database.executions.find_one(
            {"id": execution_id, "user_id": user_id}, {"_id": 0}, session=session
        )
        if execution is None:
            raise ResumeError(404, "NOT_FOUND", "任务不存在或不属于当前用户。")
        pending = execution.get("pending_input") or {}
        if execution.get("status") != "waiting_for_input":
            raise ResumeError(409, "NOT_WAITING", "任务不在等待输入状态。")
        if pending.get("kind") != "clarification":
            raise ResumeError(
                409, "RESUME_KIND_MISMATCH", "当前等待的不是资料补充，计划确认请使用计划接口。"
            )
        if (
            pending.get("interrupt_id") != interrupt_id
            or pending.get("checkpoint_id") != checkpoint_id
        ):
            raise ResumeError(409, "STALE_CHECKPOINT", "恢复标识已过期，请刷新任务状态后重试。")
        input_id = record_execution_input(
            database,
            user_id=user_id,
            execution_id=execution_id,
            interrupt_id=interrupt_id,
            checkpoint_id=checkpoint_id,
            kind="clarification",
            answer=answer_payload,
            document_versions=document_versions,
            session=session,
        )
        resumed = database.executions.update_one(
            {
                "id": execution_id,
                "user_id": user_id,
                "status": "waiting_for_input",
                "state_version": execution["state_version"],
            },
            {
                "$set": {"status": "queued", "updated_at": now},
                "$unset": {"pending_input": ""},
                "$inc": {"state_version": 1},
            },
            session=session,
        )
        if resumed.matched_count != 1:
            raise ResumeError(409, "STATE_CONFLICT", "任务状态已变化，请刷新后重试。")
        dispatch_id = enqueue_dispatch(
            database,
            user_id=user_id,
            execution_id=execution_id,
            command="resume",
            expected_state_version=execution["state_version"] + 1,
            input_ref=input_id,
            session=session,
        )
    if dispatch_id is not None:
        # Best effort: the durable row is republished by the sweeper on failure.
        publish_dispatch(settings, dispatch_id)
    return execution_id


__all__ = [
    "InputConflictError",
    "ResumeError",
    "resume_execution",
]
