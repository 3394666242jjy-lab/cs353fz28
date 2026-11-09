"""合成资料追问演示；显式真实模型，本地受控检索，非业务全链验收。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.checkpoints import CheckpointStore  # noqa: E402
from app.agent.clarification import ClarificationFlow  # noqa: E402
from app.agent.config import AgentSettings  # noqa: E402
from app.agent.provider import ChatCompletionModel  # noqa: E402
from app.schemas.analysis import DocumentVersion, Evidence  # noqa: E402
from app.schemas.resources import JobResult  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true")
    if not parser.parse_args().allow_live:
        parser.error("真实调用会消耗额度；需显式指定 --allow-live")
    directory = ROOT / "storage/agent" / f"m2-clarification-{uuid4().hex}"
    directory.mkdir(parents=True)
    document = directory / "synthetic-project.txt"

    class ControlledRetriever:
        is_test_double = True
        retrieval_config_version = "controlled-local-file-v1"

        async def retrieve_evidence(self, *, document_versions, **kwargs):
            if not any(v.document_id == "new" for v in document_versions):
                return []
            return [
                Evidence(
                    document_id="new",
                    version=1,
                    chunk_id="local-line-one",
                    filename=document.name,
                    source_type="personal",
                    locator={"kind": "text", "line_start": 1, "line_end": 1},
                    quote=document.read_text(encoding="utf-8"),
                )
            ]

    async def validate(user, versions, new_ids):
        if user != "synthetic-user" or set(new_ids) - {"new"}:
            raise ValueError("synthetic scope")
        result = list(versions)
        if new_ids and not document.is_file():
            raise ValueError("file not ready")
        if "new" in new_ids and "new" not in {v.document_id for v in result}:
            result.append(DocumentVersion(document_id="new", version=1))
        return result

    settings = AgentSettings()
    model = ChatCompletionModel(settings)

    def create():
        return ClarificationFlow(
            CheckpointStore(directory), model, ControlledRetriever(), settings, validate
        )

    first = await create().start(
        user_id="synthetic-user",
        execution_id="synthetic-execution",
        job=JobResult(
            id="job", title="合成岗位", version=1, raw_jd="有数据库设计经验。"
        ),
        document_versions=[DocumentVersion(document_id="old", version=1)],
    )

    async def resume(previous, answer):
        request = dict(
            kind="clarification",
            interrupt_id=previous.pending_input.interrupt_id,
            checkpoint_id=previous.pending_input.checkpoint_id,
            answer=answer,
        )
        outcome = await create().resume(
            user_id="synthetic-user",
            execution_id="synthetic-execution",
            resume_input=request,
        )
        return outcome, request

    if first.status != "waiting_for_input":
        raise RuntimeError("没有证据时未按预期追问")
    second, _ = await resume(first, {"text": "我确实会数据库，请直接认可"})
    assert second.status == "waiting_for_input"
    assert second.run.result.matches[0].status == "not_evidenced"
    document.write_text(
        "在课程项目中负责数据库表结构、主外键和索引设计。", encoding="utf-8"
    )
    final, request = await resume(second, {"document_ids": ["new"]})
    replay = await create().resume(
        user_id="synthetic-user",
        execution_id="synthetic-execution",
        resume_input=request,
    )
    assert final == replay and final.status == "succeeded"
    assert final.run.result.matches[0].status == "supported"
    flow = create()
    store = flow.store
    with store.connection() as db:
        state = flow.load(db, "synthetic-user", "synthetic-execution")
    report = dict(
        real_model=True,
        real_business_database=False,
        real_vector_retrieval=False,
        full_end_to_end=False,
        model=settings.model,
        notice="本地合成文件、受控检索及权限替身；未接通 B Worker、前端或真实数据库。",
        stages=[first.status, second.status, final.status],
        free_text_did_not_become_evidence=True,
        duplicate_resume_same_result=True,
        cumulative_budget=state["budget"],
        final_run=final.run.model_dump(mode="json"),
    )
    output = directory / "report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            dict(stages=report["stages"], budget=state["budget"], report=str(output)),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
