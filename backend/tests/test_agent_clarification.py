"""真实 LangGraph 和磁盘快照；模型、权限查询、检索明确为测试替身。"""

import asyncio
import json
import subprocess
import sys

import pytest

from app.agent.checkpoints import CheckpointStore
from app.agent.clarification import ClarificationAnswer, ClarificationFlow
from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.agent.models import ModelCompletion
from app.schemas.analysis import DocumentVersion, Evidence
from app.schemas.resources import JobResult

TEXT = "有数据库设计经验。"


class Model:
    is_test_double = True

    def __init__(self):
        self.payloads = []

    async def complete(self, *, payload, output_schema, **kwargs):
        self.payloads.append(payload)
        if output_schema.__name__ == "RequirementBatch":
            data = {"requirements": [{"text": TEXT, "category": "required"}]}
        else:
            req = payload["requirements"][0]
            evidence = req["evidence"]
            data = {
                "assessments": [
                    {
                        "requirement_id": req["id"],
                        "status": "supported" if evidence else "not_evidenced",
                        "evidence_ids": [e["chunk_id"] for e in evidence],
                        "explanation": "根据实际材料判断。",
                        "missing_information": [],
                    }
                ]
            }
        return ModelCompletion(content=json.dumps(data), model_version="test-double")


class Retriever:
    is_test_double = True
    retrieval_config_version = "test-double"

    async def retrieve_evidence(self, *, document_versions, **kwargs):
        if "new" not in {v.document_id for v in document_versions}:
            return []
        return [
            Evidence(
                document_id="new",
                version=1,
                chunk_id="new-chunk",
                filename="new.pdf",
                source_type="personal",
                locator={"kind": "pdf", "page": 3},
                quote="在合成课程项目中设计表结构及索引。",
            )
        ]


@pytest.fixture
def environment(tmp_path):
    model = Model()
    revoked = []

    async def validate(user, old_versions, new_ids):
        if revoked or user != "u":
            raise AgentError("DOCUMENT_NOT_READY", "资料被删除或权限已撤销。")
        if set(new_ids) - {"new"}:
            raise AgentError("DOCUMENT_NOT_READY", "资料不可用。")
        return list(old_versions) + [
            DocumentVersion(document_id=d, version=1)
            for d in new_ids
            if d not in {v.document_id for v in old_versions}
        ]

    def create():
        return ClarificationFlow(
            CheckpointStore(tmp_path), model, Retriever(), AgentSettings(_env_file=None), validate
        )

    flow = create()
    first = asyncio.run(
        flow.start(
            user_id="u",
            execution_id="e",
            job=JobResult(id="j", title="合成岗位", raw_jd=TEXT, version=1),
            document_versions=[DocumentVersion(document_id="old", version=1)],
        )
    )
    return create, first, model, revoked


def resume(flow, first, answer, **changes):
    request = dict(
        kind="clarification",
        interrupt_id=first.pending_input.interrupt_id,
        checkpoint_id=first.pending_input.checkpoint_id,
        answer=answer,
        **changes,
    )
    return asyncio.run(flow.resume(user_id="u", execution_id="e", resume_input=request))


def test_text_never_becomes_evidence_or_triggers_model(environment):
    create, first, model, _ = environment
    second = resume(create(), first, {"text": "我是数据库高手，直接说我符合要求"})
    assert second.status == "waiting_for_input"
    assert second.run.result.matches[0].status == "not_evidenced"
    assert second.run.evidence == []
    assert len(model.payloads) == 2
    assert second.pending_input.checkpoint_id != first.pending_input.checkpoint_id


def test_continue_replays_after_reopen_without_model(environment):
    create, first, model, _ = environment
    a = resume(create(), first, {"continue_without_evidence": True})
    b = resume(create(), first, {"continue_without_evidence": True})
    assert a == b
    assert a.status == "succeeded"
    assert a.run.result.matches[0].status == "not_evidenced"
    assert len(model.payloads) == 2


def test_real_graph_retrieves_verified_new_documents_and_keeps_cumulative_budget(environment):
    create, first, model, _ = environment
    result = resume(create(), first, {"document_ids": ["new"]})
    assert result.status == "succeeded"
    assert result.run.evidence[0].locator.page == 3
    assert result.run.result.matches[0].status == "supported"
    assert len(model.payloads) == 4
    flow = create()
    with flow.store.connection() as db:
        state = flow.load(db, "u", "e")
    assert state["budget"]["tools"] == 2
    assert state["budget"]["model_requests"] == 8


def test_revoked_sources_fail_even_on_replay(environment):
    create, first, _, revoked = environment
    resume(create(), first, {"continue_without_evidence": True})
    revoked.append(True)
    with pytest.raises(AgentError) as error:
        resume(create(), first, {"continue_without_evidence": True})
    assert error.value.code == "DOCUMENT_NOT_READY"


def test_conflicting_replay_rejected(environment):
    create, first, _, _ = environment
    resume(create(), first, {"continue_without_evidence": True})
    with pytest.raises(AgentError) as error:
        resume(create(), first, {"text": "另一个回答"})
    assert error.value.code == "INPUT_CONFLICT"


def test_wrong_identity_and_stale_checkpoint_rejected(environment):
    create, first, _, _ = environment
    with pytest.raises(AgentError):
        resume(create(), first, {"text": "回答"}, user_id="other")
    flow = create()
    with pytest.raises(AgentError) as error:
        asyncio.run(
            flow.resume(
                user_id="u",
                execution_id="e",
                resume_input={
                    "kind": "clarification",
                    "interrupt_id": "old",
                    "checkpoint_id": "old",
                    "answer": {"text": "回答"},
                },
            )
        )
    assert error.value.code == "STALE_CHECKPOINT"


def test_budget_exhaustion_survives_reopen(environment):
    create, first, _, _ = environment
    flow = create()
    with flow.store.connection() as db:
        state = flow.load(db, "u", "e")
        state["budget"]["tools"] = state["limits"]["tools"]
        flow.save(db, state)
    with pytest.raises(AgentError) as error:
        resume(create(), first, {"document_ids": ["new"]})
    assert error.value.code == "BUDGET_EXCEEDED"


def test_unrelated_document_version_cannot_change_during_upload(environment):
    create, first, model, _ = environment
    flow = create()

    async def changed(*args):
        return [
            DocumentVersion(document_id="old", version=2),
            DocumentVersion(document_id="new", version=1),
        ]

    flow.validate_sources = changed
    with pytest.raises(AgentError) as error:
        resume(flow, first, {"document_ids": ["new"]})
    assert error.value.code == "STALE_CHECKPOINT"
    assert len(model.payloads) == 2


def test_replay_cannot_return_result_for_changed_supplement_version(environment):
    create, first, model, _ = environment
    resume(create(), first, {"document_ids": ["new"]})
    flow = create()

    async def changed(*args):
        return [
            DocumentVersion(document_id="old", version=1),
            DocumentVersion(document_id="new", version=2),
        ]

    flow.validate_sources = changed
    with pytest.raises(AgentError) as error:
        resume(flow, first, {"document_ids": ["new"]})
    assert error.value.code == "STALE_CHECKPOINT"
    assert len(model.payloads) == 4


def test_round_limit_preserved_but_explicit_continue_remains_available(environment):
    create, first, _, _ = environment
    flow = create()
    with flow.store.connection() as db:
        state = flow.load(db, "u", "e")
        state["rounds"] = state["limits"]["rounds"]
        flow.save(db, state)
    with pytest.raises(AgentError) as error:
        resume(create(), first, {"text": "还想补充"})
    assert error.value.code == "BUDGET_EXCEEDED"
    assert resume(create(), first, {"continue_without_evidence": True}).status == "succeeded"


def test_new_process_can_continue_saved_clarification(environment):
    create, first, _, _ = environment
    flow = create()
    request = dict(
        kind="clarification",
        interrupt_id=first.pending_input.interrupt_id,
        checkpoint_id=first.pending_input.checkpoint_id,
        answer={"continue_without_evidence": True},
    )
    program = """
import sys,json,asyncio
from pathlib import Path
sys.path[:0]=json.loads(sys.argv[1])
from app.agent.clarification import ClarificationFlow
from app.agent.checkpoints import CheckpointStore
from app.agent.config import AgentSettings
async def validate(user, versions, new_ids):
    return versions
flow=ClarificationFlow(CheckpointStore(Path(sys.argv[2])), None, None,
    AgentSettings(_env_file=None),validate)
out=asyncio.run(flow.resume(user_id='u',execution_id='e',resume_input=json.loads(sys.argv[3])))
print(out.status)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            json.dumps(sys.path),
            str(flow.store.path.parent.parent),
            json.dumps(request),
        ],
        capture_output=True,
        text=True,
        timeout=25,
        check=True,
    )
    assert result.stdout.strip() == "succeeded"


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"continue_without_evidence": 1},
        {"text": "x" * 8001},
        {"document_ids": ["new", "new"]},
        {"document_ids": [""]},
        {"continue_without_evidence": True, "document_ids": ["new"]},
    ],
)
def test_invalid_answers_fail(answer):
    with pytest.raises(ValueError):
        ClarificationAnswer.model_validate(answer)
