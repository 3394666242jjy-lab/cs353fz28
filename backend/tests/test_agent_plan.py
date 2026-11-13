"""计划约束的离线验证；替身不代表真实模型或 B Worker 联调。"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.agent.models import ModelCompletion
from app.agent.plan import build_plan_draft

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def inputs():
    return dict(
        user_id="u",
        now=NOW,
        agent_settings=AgentSettings(_env_file=None),
        plan={
            "id": "p",
            "analysis_id": "a",
            "timezone": "Asia/Shanghai",
            "deadline": "2026-11-10",
            "daily_minutes": 30,
        },
        analysis={
            "id": "a",
            "status": "ready",
            "document_versions": [],
            "matches": [
                {
                    "requirement_id": "r1",
                    "requirement": "数据库",
                    "category": "required",
                    "status": "not_evidenced",
                    "evidence_refs": [],
                    "explanation": "资料未体现",
                    "missing_information": [],
                }
            ],
        },
    )


def task(**changes):
    return {
        "title": "练习索引并提交执行计划截图",
        "requirement_ids": ["r1"],
        "scheduled_date": "2026-11-09",
        "estimated_minutes": 20,
        **changes,
    }


class FakeModel:
    def __init__(self, tasks):
        self.tasks = tasks
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        return ModelCompletion(
            content=json.dumps({"draft_tasks": self.tasks}), model_version="fake"
        )


def test_draft_validates_without_creating_formal_tasks():
    args = inputs()
    model = FakeModel([task()])
    result = asyncio.run(build_plan_draft(**args, model=model))
    assert result.draft_tasks[0]["estimated_minutes"] == 20
    assert json.loads(json.dumps(result.draft_tasks))[0]["scheduled_date"] == "2026-11-09"
    assert args["plan"] == inputs()["plan"]
    assert model.calls == 1


@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [task(estimated_minutes=31)],
        [task(), task()],
        [task(scheduled_date="2026-11-08")],
        [task(scheduled_date="2026-11-11")],
        [task(requirement_ids=["invented"])],
        [task(requirement_ids=["r1", "r1"])],
        [task(estimated_minutes=True)],
        [task(source_refs=[{"document_id": "fake", "version": 1, "chunk_id": "fake"}])],
    ],
)
def test_invalid_model_plans_fail(tasks):
    with pytest.raises(AgentError, match="计划草稿"):
        asyncio.run(build_plan_draft(**inputs(), model=FakeModel(tasks)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "confirmed"),
        ("daily_minutes", True),
        ("timezone", "wrong/zone"),
        ("deadline", "2026-11-08"),
        ("analysis_id", "other"),
        ("user_id", "other"),
    ],
)
def test_invalid_inputs_do_not_call_model(field, value):
    args = inputs()
    args["plan"][field] = value
    model = FakeModel([task()])
    with pytest.raises(AgentError):
        asyncio.run(build_plan_draft(**args, model=model))
    assert model.calls == 0


def test_model_timeout_is_explicit():
    class Slow:
        async def complete(self, **kwargs):
            await asyncio.sleep(1)

    args = inputs()
    args["agent_settings"].model_timeout_seconds = 0.001
    with pytest.raises(AgentError) as error:
        asyncio.run(build_plan_draft(**args, model=Slow()))
    assert error.value.code == "MODEL_TIMEOUT"
