"""Plan persistence and transactional confirmation (dev/03, dev/04 M2).

Lifecycle: POST /plans creates a draft plan row, its execution and a
``start_plan`` dispatch in one transaction; the worker (member D's planner)
fills ``draft_tasks`` and parks the execution at waiting_for_input. Confirm
re-validates ownership, source freshness and the schedule, then in one
transaction marks the plan confirmed, writes stable-id plan_tasks, records
the confirmation input and enqueues the resume dispatch. Replays return the
already-confirmed state; no duplicate tasks or resume messages are created.
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo.database import Database

from app.config import Settings
from app.services.database import database_connection, transactional_connection
from app.services.dispatch import enqueue_dispatch, publish_dispatch
from app.services.inputs import InputConflictError, record_execution_input

TASK_STATUSES = {"todo", "in_progress", "done"}


class PlanStateError(RuntimeError):
    """Plan or its execution is not in the state this operation requires (409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PlanStaleError(RuntimeError):
    """The source analysis went stale after the draft was produced (409)."""


class PlanInfeasibleError(RuntimeError):
    """The draft violates the scheduling constraints (422 PLAN_INFEASIBLE)."""

    def __init__(self, details: list[str]) -> None:
        super().__init__("; ".join(details))
        self.details = details


class _ConcurrentConfirm(Exception):
    """Internal: another confirm won the plan guard; abort and replay its result."""


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M2 indexes per dev/03; safe to repeat on every call."""
    database.plans.create_index("id", unique=True)
    database.plans.create_index([("user_id", 1), ("created_at", -1)])
    database.plan_tasks.create_index("id", unique=True)
    database.plan_tasks.create_index([("plan_id", 1), ("id", 1)], unique=True)


def validate_timezone(timezone: str) -> ZoneInfo:
    """Parse an IANA timezone; raises ValueError for unknown identifiers."""
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone}") from exc


def validate_schedule(
    draft_tasks: list[dict[str, Any]],
    *,
    deadline: date,
    timezone: str,
    daily_minutes: int,
    today: date | None = None,
) -> None:
    """Check the draft against deadline/timezone/daily_minutes (dev/04).

    Raises PlanInfeasibleError listing every violated constraint; never
    silently drops or reschedules tasks.
    """
    tz = validate_timezone(timezone)
    today = today or datetime.now(tz).date()
    problems: list[str] = []
    per_day: dict[date, int] = {}
    for task in draft_tasks:
        try:
            scheduled = date.fromisoformat(str(task.get("scheduled_date", "")))
        except ValueError:
            problems.append(f"任务 {task.get('title')!r} 的日期无效。")
            continue
        minutes = task.get("estimated_minutes")
        if not isinstance(minutes, int) or minutes <= 0:
            problems.append(f"任务 {task.get('title')!r} 的工时无效。")
            continue
        if scheduled < today:
            problems.append(f"任务 {task.get('title')!r} 排在今天之前（{scheduled.isoformat()}）。")
        if scheduled > deadline:
            problems.append(
                f"任务 {task.get('title')!r} 超出截止日期（{scheduled.isoformat()} > "
                f"{deadline.isoformat()}）。"
            )
        per_day[scheduled] = per_day.get(scheduled, 0) + minutes
    for day, minutes in per_day.items():
        if minutes > daily_minutes:
            problems.append(
                f"{day.isoformat()} 安排 {minutes} 分钟，超过每日可用的 {daily_minutes} 分钟。"
            )
    if problems:
        raise PlanInfeasibleError(problems)


def create_plan_record(
    database: Database[dict[str, Any]],
    *,
    user_id: str,
    analysis_id: str,
    execution_id: str | None = None,
    deadline: date,
    timezone: str,
    daily_minutes: int,
    session: Any = None,
) -> str:
    """Insert the draft plan row inside the caller's transaction."""
    _ensure_indexes(database)
    plan_id = f"plan_{uuid4().hex}"
    now = datetime.now(UTC)
    database.plans.insert_one(
        {
            "id": plan_id,
            "user_id": user_id,
            "analysis_id": analysis_id,
            "execution_id": execution_id,
            "deadline": deadline.isoformat(),
            "timezone": timezone,
            "daily_minutes": daily_minutes,
            "status": "draft",
            "draft_tasks": [],
            "confirmed_at": None,
            "created_at": now,
            "updated_at": now,
        },
        session=session,
    )
    return plan_id


def get_plan(settings: Settings, user_id: str, plan_id: str) -> dict[str, Any] | None:
    """Return one plan owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.plans.find_one({"id": plan_id, "user_id": user_id}, {"_id": 0})
    return row


def attach_plan_execution(
    database: Database[dict[str, Any]],
    user_id: str,
    plan_id: str,
    execution_id: str,
    *,
    session: Any = None,
) -> None:
    """Bind the freshly created execution onto the draft plan (same txn)."""
    database.plans.update_one(
        {"id": plan_id, "user_id": user_id, "status": "draft"},
        {"$set": {"execution_id": execution_id, "updated_at": datetime.now(UTC)}},
        session=session,
    )


def get_plan_by_execution(
    settings: Settings, user_id: str, execution_id: str
) -> dict[str, Any] | None:
    """Worker-side lookup of the plan that owns one execution."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.plans.find_one(
            {"execution_id": execution_id, "user_id": user_id}, {"_id": 0}
        )
    return row


def save_plan_draft(
    settings: Settings,
    user_id: str,
    plan_id: str,
    *,
    draft_tasks: list[dict[str, Any]],
) -> bool:
    """Fill the generated draft exactly once; a confirmed plan is immutable."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        result = database.plans.update_one(
            {"id": plan_id, "user_id": user_id, "status": "draft"},
            {
                "$set": {
                    "draft_tasks": draft_tasks,
                    "updated_at": datetime.now(UTC),
                }
            },
        )
    return result.matched_count == 1


def _public_task(row: dict[str, Any]) -> dict[str, Any]:
    """Project a stored plan_tasks row down to the public contract fields."""
    return {
        "id": row["id"],
        "plan_id": row["plan_id"],
        "title": row["title"],
        "requirement_ids": list(row.get("requirement_ids") or []),
        "scheduled_date": row["scheduled_date"],
        "estimated_minutes": row["estimated_minutes"],
        "status": row["status"],
        "source_refs": list(row.get("source_refs") or []),
    }


def list_plan_tasks(settings: Settings, user_id: str, plan_id: str) -> list[dict[str, Any]]:
    """Return confirmed tasks of one plan in stable id order."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        rows = list(
            database.plan_tasks.find({"plan_id": plan_id, "user_id": user_id}, {"_id": 0}).sort(
                "id", 1
            )
        )
    return [_public_task(row) for row in rows]


def _task_rows(plan: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Build stable-id task rows from the confirmed draft (dev/03 plan_tasks)."""
    rows = []
    for index, draft in enumerate(plan["draft_tasks"], start=1):
        rows.append(
            {
                "id": f"{plan['id']}_t{index:03d}",
                "user_id": plan["user_id"],
                "plan_id": plan["id"],
                "title": draft["title"],
                "requirement_ids": list(draft.get("requirement_ids") or []),
                "scheduled_date": draft["scheduled_date"],
                "estimated_minutes": draft["estimated_minutes"],
                "status": "todo",
                "source_refs": list(draft.get("source_refs") or []),
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def confirm_plan(
    settings: Settings,
    user_id: str,
    plan_id: str,
    *,
    analysis_is_fresh: Any,
) -> dict[str, Any]:
    """Confirm a draft plan and enqueue the graph resume, atomically.

    ``analysis_is_fresh`` is a callable receiving the plan's analysis_id and
    returning True when the pinned analysis is still usable; it is evaluated
    right before the transaction so a stale source fails as ANALYSIS_STALE.
    Idempotent: confirming an already-confirmed plan returns the stored
    outcome without new tasks, inputs or dispatch rows.
    """
    plan = get_plan(settings, user_id, plan_id)
    if plan is None:
        raise PlanStateError("PLAN_NOT_FOUND", "计划不存在或不属于当前用户。")
    if plan["status"] == "confirmed":
        return _confirmed_result(settings, user_id, plan, resumed=False)
    if not plan.get("draft_tasks"):
        raise PlanStateError("PLAN_DRAFT_NOT_READY", "计划草稿尚未生成，请等待任务就绪。")
    if not analysis_is_fresh(plan["analysis_id"]):
        raise PlanStaleError(plan["analysis_id"])
    validate_schedule(
        plan["draft_tasks"],
        deadline=date.fromisoformat(plan["deadline"]),
        timezone=plan["timezone"],
        daily_minutes=plan["daily_minutes"],
    )
    now = datetime.now(UTC)
    dispatch_id: str | None = None
    try:
        with transactional_connection(settings) as (database, session):
            execution = database.executions.find_one(
                {"id": plan["execution_id"], "user_id": user_id}, {"_id": 0}, session=session
            )
            if execution is None:
                raise PlanStateError("PLAN_EXECUTION_MISSING", "计划关联的执行不存在。")
            pending = execution.get("pending_input") or {}
            waiting = (
                execution.get("status") == "waiting_for_input"
                and pending.get("kind") == "plan_confirmation"
                and pending.get("plan_id") == plan_id
            )
            if not waiting:
                raise PlanStateError(
                    "PLAN_NOT_WAITING_CONFIRMATION", "任务当前不在等待计划确认状态。"
                )
            claimed = database.plans.update_one(
                {"id": plan_id, "user_id": user_id, "status": "draft"},
                {"$set": {"status": "confirmed", "confirmed_at": now, "updated_at": now}},
                session=session,
            )
            if claimed.matched_count != 1:
                # Raising aborts the transaction; the winner's result is replayed.
                raise _ConcurrentConfirm()
            task_rows = _task_rows(plan, now)
            for row in task_rows:
                database.plan_tasks.update_one(
                    {"id": row["id"], "user_id": user_id},
                    {"$setOnInsert": row},
                    upsert=True,
                    session=session,
                )
            input_id = record_execution_input(
                database,
                user_id=user_id,
                execution_id=execution["id"],
                interrupt_id=pending["interrupt_id"],
                checkpoint_id=pending["checkpoint_id"],
                kind="plan_confirmation",
                answer={"confirmed": True, "plan_id": plan_id},
                session=session,
            )
            resumed = database.executions.update_one(
                {
                    "id": execution["id"],
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
                raise PlanStateError("PLAN_STATE_CONFLICT", "任务状态已变化，请刷新后重试。")
            dispatch_id = enqueue_dispatch(
                database,
                user_id=user_id,
                execution_id=execution["id"],
                command="resume",
                expected_state_version=execution["state_version"] + 1,
                input_ref=input_id,
                session=session,
            )
    except _ConcurrentConfirm:
        stored = get_plan(settings, user_id, plan_id)
        if stored is not None and stored["status"] == "confirmed":
            return _confirmed_result(settings, user_id, stored, resumed=False)
        raise PlanStateError("PLAN_STATE_CONFLICT", "计划状态已变化，请刷新后重试。") from None
    stored = get_plan(settings, user_id, plan_id)
    if stored is None:  # pragma: no cover - the transaction just wrote it
        raise PlanStateError("PLAN_NOT_FOUND", "计划不存在或不属于当前用户。")
    result = _confirmed_result(settings, user_id, stored, resumed=True)
    if dispatch_id is not None:
        # Best effort: the row is durable, so the sweeper republishes on failure.
        publish_dispatch(settings, dispatch_id)
    return result


def _confirmed_result(
    settings: Settings, user_id: str, plan: dict[str, Any], *, resumed: bool
) -> dict[str, Any]:
    """Assemble the confirm response: confirmed plan, tasks and execution id."""
    return {
        "plan": plan,
        "tasks": list_plan_tasks(settings, user_id, plan["id"]),
        "execution_id": plan["execution_id"],
        "resumed": resumed,
    }


def update_plan_task_status(
    settings: Settings,
    user_id: str,
    plan_id: str,
    task_id: str,
    *,
    status: str,
) -> dict[str, Any] | None:
    """Move one confirmed task; returns the updated row, or None when missing."""
    if status not in TASK_STATUSES:
        raise ValueError(f"非法任务状态：{status}")
    with database_connection(settings) as database:
        _ensure_indexes(database)
        plan = database.plans.find_one(
            {"id": plan_id, "user_id": user_id, "status": "confirmed"}, {"_id": 0, "id": 1}
        )
        if plan is None:
            return None
        database.plan_tasks.update_one(
            {"id": task_id, "plan_id": plan_id, "user_id": user_id},
            {"$set": {"status": status, "updated_at": datetime.now(UTC)}},
        )
        row = database.plan_tasks.find_one(
            {"id": task_id, "plan_id": plan_id, "user_id": user_id}, {"_id": 0}
        )
    return _public_task(row) if row is not None else None


__all__ = [
    "InputConflictError",
    "PlanInfeasibleError",
    "PlanStaleError",
    "PlanStateError",
    "confirm_plan",
    "create_plan_record",
    "get_plan",
    "get_plan_by_execution",
    "list_plan_tasks",
    "save_plan_draft",
    "update_plan_task_status",
    "validate_schedule",
    "validate_timezone",
]
