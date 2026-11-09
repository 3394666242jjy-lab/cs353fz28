"""验证真实 D 模块与 B Worker 的交接；仅外部存储/检索/模型使用替身。"""

import asyncio
from types import SimpleNamespace

import pytest
from test_agent_clarification import TEXT, Model, Retriever

from app import worker
from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.config import Settings
from app.schemas.analysis import DocumentVersion
from app.schemas.resources import JobResult
from app.services import agent_bridge


def test_worker_pauses_then_resume_saves_result_before_success(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, app_env="test", local_storage_dir=tmp_path)
    agent_settings = AgentSettings(_env_file=None)
    model = Model()
    job = JobResult(id="j", title="合成岗位", raw_jd=TEXT, version=1)
    row = dict(
        id="a",
        user_id="u",
        job_id="j",
        job_version=1,
        status="pending",
        job_snapshot=job.model_dump(),
        document_versions=[{"document_id": "old", "version": 1}],
    )
    monkeypatch.setattr(worker, "get_analysis", lambda *a: row)
    monkeypatch.setattr(
        worker, "_load_agent", lambda: agent_bridge.M2AnalysisAgent(model, agent_settings, settings)
    )
    monkeypatch.setattr(worker, "_build_retriever", lambda *a: Retriever())
    monkeypatch.setattr(agent_bridge, "validate_sources", lambda s, u, pinned, new, **kw: pinned)
    monkeypatch.setattr(worker, "transition_execution", lambda *a, **kw: True)
    parked = []
    monkeypatch.setattr(worker, "transition_to_waiting", lambda *a, **kw: parked.append(kw) or True)
    monkeypatch.setattr(
        worker, "save_analysis_result", lambda *a: pytest.fail("暂停前不应保存成功分析")
    )
    worker.run_analysis(settings, "a", "u", "e")
    assert parked[0]["pending_input"].kind == "clarification"
    pending = parked[0]["pending_input"]
    stored = dict(
        id="input",
        user_id="u",
        execution_id="e",
        kind="clarification",
        interrupt_id=pending.interrupt_id,
        checkpoint_id=pending.checkpoint_id,
        answer={"continue_without_evidence": True},
        document_versions=[],
    )
    monkeypatch.setattr("app.services.inputs.load_input_by_id", lambda *a: stored)
    monkeypatch.setattr("app.services.analyses.get_analysis", lambda *a: row)
    monkeypatch.setattr("app.services.jobs.load_job", lambda *a: job)
    monkeypatch.setattr("app.agent.provider.ChatCompletionModel", lambda *a: model)
    saved = []

    def save(*args, **kwargs):
        saved.append(kwargs)
        assert kwargs["expected_document_versions"] == row["document_versions"]
        return True

    monkeypatch.setattr("app.services.analyses.save_analysis_result", save)
    from app.agent.resume import resume_execution

    result = asyncio.run(
        resume_execution(user_id="u", execution_id="e", resume_input=stored, settings=settings)
    )
    assert result.status == "succeeded" and result.result_id == "a"
    assert len(saved) == 1
    assert len(model.payloads) == 2


@pytest.mark.parametrize("change", ["owner", "version", "not_ready", "input_version"])
def test_authoritative_version_validation_rejects_unusable_sources(monkeypatch, change):
    version = 2 if change in {"version", "input_version"} else 1
    monkeypatch.setattr(
        agent_bridge,
        "get_document",
        lambda *a: (
            None
            if change == "owner"
            else SimpleNamespace(
                status="processing" if change == "not_ready" else "ready", current_version=version
            )
        ),
    )
    monkeypatch.setattr(
        agent_bridge, "list_document_versions", lambda *a: [{"version": version, "status": "ready"}]
    )
    kwargs = (
        {"input_versions": [{"document_id": "d", "version": 1}]}
        if change == "input_version"
        else {}
    )
    with pytest.raises(AgentError):
        agent_bridge.validate_sources(
            Settings(_env_file=None),
            "u",
            [DocumentVersion(document_id="d", version=1)],
            ["d"] if change == "input_version" else [],
            **kwargs,
        )


def test_saved_input_hash_includes_pinned_versions(monkeypatch):
    from unittest.mock import MagicMock

    from app.services.inputs import answer_content_hash, record_execution_input

    db = MagicMock()
    answer = {"text": "补充", "document_ids": ["new"], "continue_without_evidence": False}
    versions = [{"document_id": "new", "version": 1}]
    record_execution_input(
        db,
        user_id="u",
        execution_id="e",
        interrupt_id="i",
        checkpoint_id="c",
        kind="clarification",
        answer=answer,
        document_versions=versions,
    )
    stored = db.execution_inputs.insert_one.call_args.args[0]
    assert stored["content_hash"] == answer_content_hash({**answer, "document_versions": versions})
