import asyncio
import json

import pytest
from langsmith import get_tracing_context, tracing_context

from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.agent.models import ModelCompletion, TokenUsage
from app.agent.pipeline import ALLOWED_TOOLS, LangGraphAnalysisAgent
from app.schemas.analysis import DocumentVersion, Evidence
from app.schemas.resources import JobResult

JD = "有数据库设计经验。\n有缓存优化和测量经验。\n有 Docker 部署经验。"
TEXTS = JD.splitlines()


class ScriptedModel:
    is_test_double = True

    def __init__(self, requirements=None, assessments=None, model_version="scripted-offline"):
        self.requirements = requirements
        self.assessments = assessments
        self.model_version = model_version
        self.calls = []

    async def complete(self, *, system, payload, output_schema):
        self.calls.append((system, payload, output_schema))
        if output_schema.__name__ == "RequirementBatch":
            data = {
                "requirements": self.requirements
                if self.requirements is not None
                else [{"text": text, "category": "required"} for text in TEXTS]
            }
        else:
            decisions = self.assessments
            if decisions is None:
                statuses = ["supported", "partial", "not_evidenced"]
                decisions = [
                    decision(
                        req["id"],
                        statuses[i],
                        ["chunk_db"] if i == 0 else ["chunk_cache"] if i == 1 else [],
                    )
                    for i, req in enumerate(payload["requirements"])
                ]
            data = {"assessments": decisions}
        return ModelCompletion(
            content=json.dumps(data, ensure_ascii=False),
            model_version=self.model_version,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


def decision(requirement_id, status, evidence_ids):
    return {
        "requirement_id": requirement_id,
        "status": status,
        "evidence_ids": evidence_ids,
        "explanation": "原文支持完整要求。"
        if status == "supported"
        else "资料只体现部分或未体现该要求。",
        "missing_information": [] if status == "supported" else ["请补充相关记录。"],
    }


def evidence(
    chunk_id="chunk_db", *, document_id="doc_one", version=2, source_type="personal", pdf=False
):
    return Evidence(
        chunk_id=chunk_id,
        document_id=document_id,
        version=version,
        filename="synthetic.pdf" if pdf else "synthetic.md",
        source_type=source_type,
        quote="负责设计数据表和索引。" if chunk_id == "chunk_db" else "使用缓存，未提供测量结果。",
        locator={"kind": "pdf", "page": 3}
        if pdf
        else {"kind": "text", "line_start": 11, "line_end": 11},
    )


class EvidenceDouble:
    is_test_double = True
    retrieval_config_version = "synthetic-retriever-v1"

    def __init__(self, rows=None, error=None, delay=0):
        self.rows = rows
        self.error = error
        self.delay = delay
        self.calls = []

    async def retrieve_evidence(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.rows is not None:
            return self.rows
        query = kwargs["query"]
        if query == TEXTS[0]:
            return [evidence()]
        if query == TEXTS[1]:
            return [evidence("chunk_cache", pdf=True)]
        return []


def run(model=None, retriever=None, settings=None, **overrides):
    kwargs = {
        "user_id": "user_one",
        "job": JobResult(id="job_one", title="合成岗位", raw_jd=JD, version=4),
        "document_versions": [DocumentVersion(document_id="doc_one", version=2)],
        "retriever": retriever or EvidenceDouble(),
        **overrides,
    }
    agent = LangGraphAnalysisAgent(
        model or ScriptedModel(), settings or AgentSettings(_env_file=None)
    )
    return asyncio.run(agent.run_analysis_with_details(**kwargs))


def test_real_langgraph_runs_three_statuses_and_preserves_pdf_and_text_locators():
    retriever = EvidenceDouble()
    details = run(retriever=retriever)
    result = details.result
    assert [match.status for match in result.matches] == ["supported", "partial", "not_evidenced"]
    assert result.job_id == "job_one" and result.job_version == 4
    assert result.id.startswith("analysis_") and result.id != "analysis_demo_001"
    assert result.document_versions == [DocumentVersion(document_id="doc_one", version=2)]
    assert result.matches[2].evidence_refs == []
    assert details.evidence[0].locator.line_start == 11
    assert details.evidence[1].locator.page == 3
    assert details.evidence[1].quote == evidence("chunk_cache", pdf=True).quote
    assert details.requirement_snapshot[0].text == TEXTS[0]
    assert details.graph_version == "m1-analysis-graph-v1"
    assert details.usage.input_tokens == 20 and details.usage.output_tokens == 10
    assert len(details.events) == 6
    assert details.events[-1].tool_calls == 3
    assert details.events[-1].model_requests == 2
    assert all(call["user_id"] == "user_one" and call["top_k"] == 5 for call in retriever.calls)
    assert all(call["document_versions"] == result.document_versions for call in retriever.calls)
    assert result.prompt_version == "m1-analysis-v1"
    assert result.model_version == "scripted-offline"
    assert any("测试替身" in warning for warning in result.warnings)
    assert ALLOWED_TOOLS == ("retrieve_evidence",)


def test_standard_contract_returns_analysis_result_and_runs_are_isolated():
    agent = LangGraphAnalysisAgent(ScriptedModel(), AgentSettings(_env_file=None))

    async def concurrent_runs():
        return await asyncio.gather(
            *[
                agent.run_analysis(
                    user_id=f"user_{i}",
                    job=JobResult(id=f"job_{i}", title="Demo", raw_jd=JD, version=1),
                    document_versions=[DocumentVersion(document_id="doc_one", version=2)],
                    retriever=EvidenceDouble(),
                )
                for i in range(2)
            ]
        )

    first, second = asyncio.run(concurrent_runs())
    assert first.job_id == "job_0" and second.job_id == "job_1"
    assert first.id != second.id and first.trace_id != second.trace_id


@pytest.mark.parametrize(
    "requirements",
    [
        [{"text": "JD 从未写过的要求", "category": "required"}],
        [{"text": TEXTS[0], "category": "required"}] * 2,
        [],
    ],
)
def test_hallucinated_duplicate_or_empty_requirements_fail_before_retrieval(requirements):
    retriever = EvidenceDouble()
    with pytest.raises(AgentError) as caught:
        run(model=ScriptedModel(requirements=requirements), retriever=retriever)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert retriever.calls == []


def test_requirement_overflow_fails_instead_of_dropping_items():
    with pytest.raises(AgentError) as caught:
        run(settings=AgentSettings(_env_file=None, max_requirements=2))
    assert caught.value.code == "BUDGET_EXCEEDED"


@pytest.mark.parametrize(
    "rows",
    [
        [evidence(version=1)],
        [evidence(document_id="other_document")],
        [evidence(source_type="public")],
        [{"chunk_id": "metadata_without_quote"}],
        [evidence(), evidence(pdf=True)],
        [evidence()] * 6,
    ],
)
def test_untrusted_or_conflicting_evidence_is_rejected(rows):
    model = ScriptedModel()
    with pytest.raises(AgentError) as caught:
        run(model=model, retriever=EvidenceDouble(rows=rows))
    assert caught.value.code == "EVIDENCE_INVALID"
    assert len(model.calls) == 1


def test_fabricated_model_citation_is_rejected():
    decisions = [
        decision("req_001", "supported", ["invented_chunk"]),
        decision("req_002", "partial", ["chunk_cache"]),
        decision("req_003", "not_evidenced", []),
    ]
    with pytest.raises(AgentError) as caught:
        run(model=ScriptedModel(assessments=decisions))
    assert caught.value.code == "EVIDENCE_INVALID"


def test_cannot_borrow_another_requirement_evidence():
    decisions = [
        decision("req_001", "supported", ["chunk_cache"]),
        decision("req_002", "partial", ["chunk_cache"]),
        decision("req_003", "not_evidenced", []),
    ]
    with pytest.raises(AgentError) as caught:
        run(model=ScriptedModel(assessments=decisions))
    assert caught.value.code == "EVIDENCE_INVALID"


@pytest.mark.parametrize(
    "decisions",
    [
        [decision("req_001", "supported", ["chunk_db"])],
        [decision("req_001", "supported", ["chunk_db"])] * 3,
        [
            decision("req_001", "not_evidenced", ["chunk_db"]),
            decision("req_002", "partial", ["chunk_cache"]),
            decision("req_003", "not_evidenced", []),
        ],
        [
            decision("req_001", "supported", []),
            decision("req_002", "partial", ["chunk_cache"]),
            decision("req_003", "not_evidenced", []),
        ],
    ],
)
def test_incomplete_or_inconsistent_decisions_fail(decisions):
    with pytest.raises(AgentError) as caught:
        run(model=ScriptedModel(assessments=decisions))
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


def test_valid_empty_retrieval_can_be_not_evidenced_but_is_not_an_error():
    model = ScriptedModel(
        assessments=[decision(f"req_{i + 1:03d}", "not_evidenced", []) for i in range(3)]
    )
    details = run(model=model, retriever=EvidenceDouble(rows=[]))
    assert details.evidence == []
    assert all(match.status == "not_evidenced" for match in details.result.matches)


@pytest.mark.parametrize("source_code", ["DOCUMENT_NOT_READY", "DOCUMENT_PARSE_FAILED", "OTHER"])
def test_failed_retrieval_never_becomes_successful_not_evidenced(source_code):
    class SourceFailure(Exception):
        code = source_code

    model = ScriptedModel()
    with pytest.raises(AgentError) as caught:
        run(model=model, retriever=EvidenceDouble(error=SourceFailure("PRIVATE-SECRET")))
    assert caught.value.code == (source_code if source_code != "OTHER" else "RETRIEVAL_FAILED")
    assert "PRIVATE-SECRET" not in str(caught.value)
    assert caught.value.trace_id.startswith("trace_")
    assert len(model.calls) == 1


def test_retrieval_timeout_and_context_budget():
    with pytest.raises(AgentError) as caught:
        run(
            retriever=EvidenceDouble(delay=0.1),
            settings=AgentSettings(_env_file=None, tool_timeout_seconds=0.01),
        )
    assert caught.value.code == "RETRIEVAL_TIMEOUT"
    with pytest.raises(AgentError) as caught:
        run(settings=AgentSettings(_env_file=None, max_evidence_chars=1))
    assert caught.value.code == "BUDGET_EXCEEDED"


def test_model_timeout_and_total_timeout_are_distinct():
    class SlowModel(ScriptedModel):
        async def complete(self, **kwargs):
            await asyncio.sleep(0.1)
            return await super().complete(**kwargs)

    with pytest.raises(AgentError) as caught:
        run(model=SlowModel(), settings=AgentSettings(_env_file=None, model_timeout_seconds=0.01))
    assert caught.value.code == "MODEL_TIMEOUT"
    with pytest.raises(AgentError) as caught:
        run(model=SlowModel(), settings=AgentSettings(_env_file=None, total_timeout_seconds=0.01))
    assert caught.value.code == "ANALYSIS_TIMEOUT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_id": " "},
        {"document_versions": []},
        {
            "document_versions": [
                DocumentVersion(document_id="doc_one", version=1),
                DocumentVersion(document_id="doc_one", version=2),
            ]
        },
    ],
)
def test_invalid_input_fails_without_model_calls(overrides):
    model = ScriptedModel()
    with pytest.raises(AgentError) as caught:
        run(model=model, **overrides)
    assert caught.value.code == "INVALID_INPUT"
    assert model.calls == []


def test_schema_and_prompts_do_not_allow_user_identity_or_tools_to_be_model_controlled():
    model = ScriptedModel()
    run(model=model)
    for system, payload, schema in model.calls:
        assert "不可信数据" in system and "JSON" in system
        assert "user_id" not in payload and "tools" not in payload
        assert schema.model_json_schema()["additionalProperties"] is False
    assessment_payload = model.calls[1][1]
    assert assessment_payload["requirements"][1]["evidence"][0]["locator"] == {
        "kind": "pdf",
        "page": 3,
    }


def test_unknown_retrieval_version_is_left_unset():
    retriever = EvidenceDouble()
    retriever.retrieval_config_version = None
    details = run(retriever=retriever)
    assert details.result.retrieval_config_version is None
    assert any("未提供配置版本" in message for message in details.result.warnings)


def test_graph_disables_inherited_external_tracing_and_restores_parent_context():
    class TraceCheckingModel(ScriptedModel):
        async def complete(self, **kwargs):
            assert get_tracing_context()["enabled"] is False
            return await super().complete(**kwargs)

    with tracing_context(enabled=True):
        run(model=TraceCheckingModel())
        assert get_tracing_context()["enabled"] is True
