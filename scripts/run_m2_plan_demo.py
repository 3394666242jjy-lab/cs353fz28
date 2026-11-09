"""合成计划演示：显式允许后调用真实模型，业务确认仍为替身。Refs #26。"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.checkpoints import CheckpointStore  # noqa: E402
from app.agent.config import AgentSettings  # noqa: E402
from app.agent.plan import persistent_plan_draft  # noqa: E402
from app.agent.provider import ChatCompletionModel  # noqa: E402
from app.agent.resume import confirm_saved_plan  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    if not args.allow_live:
        parser.error("真实模型会消耗额度；需显式指定 --allow-live")
    directory = ROOT / "storage/agent" / f"m2-plan-{uuid4().hex}"
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    plan = dict(
        id="synthetic-plan",
        execution_id="synthetic-execution",
        user_id="synthetic-user",
        analysis_id="synthetic-analysis",
        status="draft",
        timezone="Asia/Shanghai",
        deadline=(today + timedelta(days=6)).isoformat(),
        daily_minutes=60,
    )
    analysis = dict(
        id="synthetic-analysis",
        user_id="synthetic-user",
        status="ready",
        document_versions=[],
        matches=[
            dict(
                requirement_id="req_001",
                requirement="能够解释数据库索引的使用与取舍",
                category="required",
                status="not_evidenced",
                evidence_refs=[],
                explanation="当前合成资料未体现",
                missing_information=["可核验的练习记录"],
            )
        ],
    )
    model = ChatCompletionModel(AgentSettings())
    draft = await persistent_plan_draft(
        user_id="synthetic-user",
        plan=plan,
        analysis=analysis,
        model=model,
        agent_settings=model.settings,
        store=CheckpointStore(directory),
    )
    # 重新打开磁盘存储；这里模拟 B 已确认记录，不代表真实数据库事务验证。
    store = CheckpointStore(directory)
    before = store.load("synthetic-user", "synthetic-execution")
    confirmed = {**plan, "status": "confirmed", "draft_tasks": draft.draft_tasks}
    resume_input = dict(
        kind="plan_confirmation",
        interrupt_id=draft.interrupt_id,
        checkpoint_id=draft.checkpoint_id,
        answer={"confirmed": True, "plan_id": plan["id"]},
    )
    outcomes = [
        confirm_saved_plan(
            user_id="synthetic-user",
            execution_id=plan["execution_id"],
            resume_input=resume_input,
            confirmed_plan=confirmed,
            store=store,
        )
        for _ in range(2)
    ]
    after = store.load("synthetic-user", "synthetic-execution")
    assert outcomes[0] == outcomes[1]
    assert before["budget"] == after["budget"]
    report = dict(
        real_model=True,
        model=model.settings.model,
        real_checkpoint_storage=True,
        real_business_database=False,
        full_end_to_end=False,
        notice="合成资料；B 的确认记录为替身；未进行网页、队列或真实数据库联调。",
        draft_tasks=draft.draft_tasks,
        resume_status=outcomes[0].status,
        duplicate_resume_same_result=True,
        budget_unchanged=True,
    )
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            dict(
                task_count=len(draft.draft_tasks),
                resume_status=outcomes[0].status,
                real_model=True,
                full_end_to_end=False,
                report=str(directory / "report.json"),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
