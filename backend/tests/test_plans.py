"""Tests for the plan service: scheduling validation and transactional confirm (issue #24)."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.plans import (
    PlanInfeasibleError,
    PlanStaleError,
    PlanStateError,
    confirm_plan,
    update_plan_task_status,
    validate_schedule,
    validate_timezone,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


def _draft(**overrides):
    task = {
        "title": "复习数据库索引",
        "requirement_ids": ["req_01"],
        "scheduled_date": "2026-11-06",
        "estimated_minutes": 60,
        "source_refs": [],
    }
    task.update(overrides)
    return task


def test_validate_timezone_rejects_unknown_identifier():
    with pytest.raises(ValueError, match="未知时区"):
        validate_timezone("Mars/Olympus")
    assert validate_timezone("Asia/Shanghai").key == "Asia/Shanghai"


def test_validate_schedule_accepts_feasible_draft():
    validate_schedule(
        [_draft(), _draft(title="模拟面试", scheduled_date="2026-11-06", estimated_minutes=45)],
        deadline=date(2026, 11, 10),
        timezone="Asia/Shanghai",
        daily_minutes=120,
        today=date(2026, 11, 5),
    )


@pytest.mark.parametrize(
    ("overrides", "message_part"),
    [
        ({"scheduled_date": "not-a-date"}, "日期无效"),
        ({"estimated_minutes": 0}, "工时无效"),
        ({"scheduled_date": "2026-11-04"}, "排在今天之前"),
        ({"scheduled_date": "2026-11-11"}, "超出截止日期"),
        ({"estimated_minutes": 200}, "超过每日可用"),
    ],
)
def test_validate_schedule_reports_every_violation(overrides, message_part):
    with pytest.raises(PlanInfeasibleError) as excinfo:
        validate_schedule(
            [_draft(**overrides)],
            deadline=date(2026, 11, 10),
            timezone="Asia/Shanghai",
            daily_minutes=120,
            today=date(2026, 11, 5),
        )
    assert any(message_part in detail for detail in excinfo.value.details)


def _plan_row(**overrides):
    row = {
        "id": "plan_1",
        "user_id": "user_one",
        "analysis_id": "analysis_1",
        "execution_id": "exec_1",
        "deadline": "2026-11-10",
        "timezone": "Asia/Shanghai",
        "daily_minutes": 120,
        "status": "draft",
        "draft_tasks": [_draft()],
        "confirmed_at": None,
        "created_at": MagicMock(),
        "updated_at": MagicMock(),
    }
    row.update(overrides)
    return row


def _waiting_execution(**overrides):
    row = {
        "id": "exec_1",
        "user_id": "user_one",
        "status": "waiting_for_input",
        "state_version": 3,
        "pending_input": {
            "kind": "plan_confirmation",
            "interrupt_id": "int_1",
            "checkpoint_id": "chk_1",
            "plan_id": "plan_1",
        },
    }
    row.update(overrides)
    return row


@pytest.fixture
def confirm_mocks(settings, monkeypatch):
    """Mock the DB boundary of confirm_plan; returns (db, published)."""
    connection = MagicMock()
    session = MagicMock(name="session")
    connection.return_value.__enter__.return_value = (db := MagicMock(), session)
    monkeypatch.setattr("app.services.plans.transactional_connection", connection)
    db.executions.find_one.return_value = _waiting_execution()
    db.plans.update_one.return_value.matched_count = 1
    db.executions.update_one.return_value.matched_count = 1
    monkeypatch.setattr("app.services.plans.record_execution_input", lambda *a, **k: "inp_1")
    monkeypatch.setattr("app.services.plans.enqueue_dispatch", lambda *a, **k: "disp_1")
    published = []
    monkeypatch.setattr(
        "app.services.plans.publish_dispatch",
        lambda settings, dispatch_id: published.append(dispatch_id) or True,
    )
    monkeypatch.setattr(
        "app.services.plans.list_plan_tasks",
        lambda settings, user_id, plan_id: [{"id": f"{plan_id}_t001"}],
    )
    return db, published


def test_confirm_plan_commits_tasks_input_and_dispatch_atomically(
    settings, confirm_mocks, monkeypatch
):
    db, published = confirm_mocks
    monkeypatch.setattr(
        "app.services.plans.get_plan",
        lambda settings, user_id, plan_id: _plan_row(),
    )
    outcome = confirm_plan(settings, "user_one", "plan_1", analysis_is_fresh=lambda aid: True)
    assert outcome["resumed"] is True
    assert outcome["execution_id"] == "exec_1"
    assert outcome["tasks"] == [{"id": "plan_1_t001"}]
    # Plan guard, stable-id task upsert, execution resume and dispatch.
    plan_query = db.plans.update_one.call_args_list[0].args[0]
    assert plan_query["status"] == "draft"
    task_upsert = db.plan_tasks.update_one.call_args
    assert task_upsert.args[0] == {"id": "plan_1_t001", "user_id": "user_one"}
    assert task_upsert.kwargs["upsert"] is True
    resume_query = db.executions.update_one.call_args.args[0]
    assert resume_query["status"] == "waiting_for_input"
    assert resume_query["state_version"] == 3
    assert published == ["disp_1"]


def test_confirm_plan_replay_returns_stored_result_without_side_effects(
    settings, confirm_mocks, monkeypatch
):
    db, published = confirm_mocks
    monkeypatch.setattr(
        "app.services.plans.get_plan",
        lambda settings, user_id, plan_id: _plan_row(status="confirmed"),
    )
    outcome = confirm_plan(settings, "user_one", "plan_1", analysis_is_fresh=lambda aid: True)
    assert outcome["resumed"] is False
    db.plans.update_one.assert_not_called()
    assert published == []


def test_confirm_plan_concurrent_loser_replays_winner_result(settings, confirm_mocks, monkeypatch):
    db, published = confirm_mocks
    db.plans.update_one.return_value.matched_count = 0
    states = iter([_plan_row(), _plan_row(status="confirmed")])
    monkeypatch.setattr(
        "app.services.plans.get_plan",
        lambda settings, user_id, plan_id: next(states),
    )
    outcome = confirm_plan(settings, "user_one", "plan_1", analysis_is_fresh=lambda aid: True)
    assert outcome["resumed"] is False
    assert published == []


def test_confirm_plan_rejects_stale_source(settings, confirm_mocks, monkeypatch):
    monkeypatch.setattr(
        "app.services.plans.get_plan",
        lambda settings, user_id, plan_id: _plan_row(),
    )
    with pytest.raises(PlanStaleError):
        confirm_plan(settings, "user_one", "plan_1", analysis_is_fresh=lambda aid: False)


def test_confirm_plan_requires_generated_draft(settings, confirm_mocks, monkeypatch):
    monkeypatch.setattr(
        "app.services.plans.get_plan",
        lambda settings, user_id, plan_id: _plan_row(draft_tasks=[]),
    )
    with pytest.raises(PlanStateError, match="草稿尚未生成"):
        confirm_plan(settings, "user_one", "plan_1", analysis_is_fresh=lambda aid: True)


def test_confirm_plan_requires_waiting_execution(settings, confirm_mocks, monkeypatch):
    db, _ = confirm_mocks
    db.executions.find_one.return_value = _waiting_execution(status="running")
    monkeypatch.setattr(
        "app.services.plans.get_plan",
        lambda settings, user_id, plan_id: _plan_row(),
    )
    with pytest.raises(PlanStateError, match="不在等待计划确认"):
        confirm_plan(settings, "user_one", "plan_1", analysis_is_fresh=lambda aid: True)


def test_update_plan_task_status_requires_confirmed_plan(settings, monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.plans.database_connection", connection)
    db = connection.return_value.__enter__.return_value
    db.plans.find_one.return_value = None
    assert (
        update_plan_task_status(settings, "user_one", "plan_1", "plan_1_t001", status="done")
        is None
    )
    db.plan_tasks.update_one.assert_not_called()
    db.plans.find_one.return_value = {"id": "plan_1"}
    db.plan_tasks.find_one.return_value = {
        "id": "plan_1_t001",
        "user_id": "user_one",
        "plan_id": "plan_1",
        "title": "复习索引",
        "requirement_ids": [],
        "scheduled_date": "2099-01-01",
        "estimated_minutes": 45,
        "status": "done",
        "source_refs": [],
    }
    row = update_plan_task_status(settings, "user_one", "plan_1", "plan_1_t001", status="done")
    assert row["status"] == "done"
    assert "user_id" not in row
    query, update = db.plan_tasks.update_one.call_args.args
    assert query == {"id": "plan_1_t001", "plan_id": "plan_1", "user_id": "user_one"}
    assert update["$set"]["status"] == "done"
    with pytest.raises(ValueError, match="非法任务状态"):
        update_plan_task_status(settings, "user_one", "plan_1", "plan_1_t001", status="archived")
