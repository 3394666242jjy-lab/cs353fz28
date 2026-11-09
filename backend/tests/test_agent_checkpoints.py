"""真实磁盘快照及重复恢复；业务查询和模型明确使用替身。"""

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agent.checkpoints import CheckpointStore
from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.agent.models import ModelCompletion
from app.agent.plan import persistent_plan_draft
from app.agent.resume import confirm_saved_plan


class Model:
    calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        return ModelCompletion(
            model_version="declared-test-double",
            content=json.dumps(
                {
                    "draft_tasks": [
                        {
                            "title": "完成数据库练习并提交说明",
                            "requirement_ids": ["r"],
                            "scheduled_date": "2099-01-01",
                            "estimated_minutes": 20,
                        }
                    ]
                }
            ),
        )


@pytest.fixture
def run(tmp_path):
    model = Model()
    plan = dict(
        id="p",
        user_id="u",
        execution_id="e",
        analysis_id="a",
        status="draft",
        deadline="2099-01-02",
        timezone="Asia/Shanghai",
        daily_minutes=30,
    )
    analysis = dict(
        id="a",
        user_id="u",
        status="ready",
        document_versions=[],
        matches=[
            dict(
                requirement_id="r",
                requirement="数据库",
                category="required",
                status="not_evidenced",
                evidence_refs=[],
                explanation="资料未体现",
                missing_information=[],
            )
        ],
    )
    args = dict(
        user_id="u",
        plan=plan,
        analysis=analysis,
        model=model,
        agent_settings=AgentSettings(_env_file=None),
        store=CheckpointStore(tmp_path),
    )
    draft = asyncio.run(persistent_plan_draft(**args))
    resume = dict(
        kind="plan_confirmation",
        interrupt_id=draft.interrupt_id,
        checkpoint_id=draft.checkpoint_id,
        answer={"confirmed": True, "plan_id": "p"},
    )
    confirmed = {**plan, "status": "confirmed", "draft_tasks": draft.draft_tasks}
    return tmp_path, args, resume, confirmed


def test_restart_replays_draft_without_model_and_keeps_budget(run):
    path, args, _, _ = run
    before = args["store"].load("u", "e")
    args["store"] = CheckpointStore(path)
    repeated = asyncio.run(persistent_plan_draft(**args))
    assert repeated.checkpoint_id == before["checkpoint_id"]
    assert args["model"].calls == 1
    assert args["store"].load("u", "e")["budget"] == before["budget"]


def test_parallel_confirmation_is_idempotent_and_does_not_mutate_tasks(run):
    path, args, resume, confirmed = run

    def confirm(_):
        return confirm_saved_plan(
            user_id="u",
            execution_id="e",
            resume_input=resume,
            confirmed_plan=confirmed,
            store=CheckpointStore(path),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(confirm, range(8)))
    assert all(r.status == "succeeded" and r.result_id == "p" for r in outcomes)
    assert args["model"].calls == 1
    assert confirmed["draft_tasks"] == args["store"].load("u", "e")["draft_tasks"]


@pytest.mark.parametrize("field", ["checkpoint_id", "interrupt_id"])
def test_stale_identifiers_rejected(run, field):
    _, args, resume, confirmed = run
    resume[field] = "old"
    with pytest.raises(AgentError, match="当前等待确认"):
        confirm_saved_plan(
            user_id="u",
            execution_id="e",
            resume_input=resume,
            confirmed_plan=confirmed,
            store=args["store"],
        )


def test_other_user_cannot_load_or_replay(run):
    _, args, _, _ = run
    with pytest.raises(AgentError) as error:
        args["store"].load("other", "e")
    assert error.value.code == "CHECKPOINT_MISSING"


@pytest.mark.parametrize("change", ["status", "tasks", "owner"])
def test_business_confirmation_must_match_saved_draft(run, change):
    _, args, resume, confirmed = run
    if change == "status":
        confirmed["status"] = "draft"
    elif change == "owner":
        confirmed["user_id"] = "other"
    else:
        confirmed["draft_tasks"] = []
    with pytest.raises(AgentError, match="未确认或内容"):
        confirm_saved_plan(
            user_id="u",
            execution_id="e",
            resume_input=resume,
            confirmed_plan=confirmed,
            store=args["store"],
        )


def test_interrupted_generation_never_silently_restarts_or_resets_budget(tmp_path):
    store = CheckpointStore(tmp_path)
    params = dict(
        user_id="u",
        execution_id="e",
        fingerprint="f",
        plan_id="p",
        budget={"model_requests_reserved": 2},
    )
    store.claim(**params)
    with pytest.raises(AgentError) as error:
        CheckpointStore(tmp_path).claim(**params)
    assert error.value.code == "CHECKPOINT_INCOMPLETE"


def test_changed_sources_rejected_before_another_model_call(run):
    _, args, _, _ = run
    args["plan"]["daily_minutes"] = 40
    with pytest.raises(AgentError) as error:
        asyncio.run(persistent_plan_draft(**args))
    assert error.value.code == "STALE_CHECKPOINT"
    assert args["model"].calls == 1


def test_fresh_python_process_restores_saved_snapshot(run):
    path, args, resume, confirmed = run
    program = """
import json, sys
from pathlib import Path
sys.path[:0] = json.loads(sys.argv[1])
from app.agent.checkpoints import CheckpointStore
from app.agent.resume import confirm_saved_plan
store = CheckpointStore(Path(sys.argv[2]))
outcome = confirm_saved_plan(user_id='u', execution_id='e',
    resume_input=json.loads(sys.argv[3]), confirmed_plan=json.loads(sys.argv[4]), store=store)
print(outcome.result_id)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            json.dumps(sys.path),
            str(path),
            json.dumps(resume),
            json.dumps(confirmed),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.strip() == "p"
    assert args["store"].load("u", "e")["status"] == "succeeded"


def test_truthy_number_is_not_explicit_confirmation(run):
    _, args, resume, confirmed = run
    resume["answer"]["confirmed"] = 1
    with pytest.raises(AgentError):
        confirm_saved_plan(
            user_id="u",
            execution_id="e",
            resume_input=resume,
            confirmed_plan=confirmed,
            store=args["store"],
        )
