"""M1 LangGraph: JD extraction -> trusted retrieval -> assessment -> validated result.

No persistence, checkpoints, uploads, parsing or model-selected tools live in D's module.
"""

import asyncio
import time
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import BaseModel, ValidationError

from app.agent.config import AgentSettings, get_agent_settings
from app.agent.errors import AgentError
from app.agent.models import (
    AnalysisRun,
    Assessment,
    AssessmentBatch,
    ModelCompletion,
    Requirement,
    RequirementBatch,
    StageEvent,
    TokenUsage,
)
from app.agent.prompts import ASSESS_PROMPT, DECOMPOSE_PROMPT, PROMPT_VERSION
from app.agent.provider import AnalysisModel, ChatCompletionModel
from app.rag.contracts import EvidenceRetriever
from app.schemas.analysis import AnalysisResult, DocumentVersion, Evidence, EvidenceRef, Match
from app.schemas.resources import JobResult

ALLOWED_TOOLS = ("retrieve_evidence",)
GRAPH_VERSION = "m1-analysis-graph-v1"


class AnalysisState(TypedDict, total=False):
    requirements: list[Requirement]
    evidence_by_requirement: dict[str, list[Evidence]]
    assessments: list[Assessment]
    result: AnalysisResult


class LangGraphAnalysisAgent:
    def __init__(self, model: AnalysisModel, settings: AgentSettings | None = None):
        self.model = model
        self.settings = (settings or get_agent_settings()).model_copy(deep=True)

    async def run_analysis(
        self,
        *,
        user_id: str,
        job: JobResult,
        document_versions: list[DocumentVersion],
        retriever: EvidenceRetriever,
    ) -> AnalysisResult:
        run = await self.run_analysis_with_details(
            user_id=user_id, job=job, document_versions=document_versions, retriever=retriever
        )
        return run.result

    async def run_analysis_with_details(
        self,
        *,
        user_id: str,
        job: JobResult,
        document_versions: list[DocumentVersion],
        retriever: EvidenceRetriever,
    ) -> AnalysisRun:
        """Optional typed handoff of requirement snapshots, cited evidence and safe events.

        B is responsible for validating user ownership/readiness before calling, rechecking
        source lifecycle before saving, assigning execution state and persisting results.
        """
        settings = self.settings
        trace_id = f"trace_{uuid4().hex}"
        current_stage = "decompose"
        try:
            if not user_id.strip() or len(user_id) > 256:
                raise ValueError("Invalid user identity")
            job = JobResult.model_validate(job.model_dump(), strict=True)
            if not job.id.strip() or not job.raw_jd.strip():
                raise ValueError("Job must contain an identity and nonblank JD")
            if not 1 <= len(document_versions) <= 20:
                raise ValueError("Invalid document version count")
            versions = [
                DocumentVersion.model_validate(v.model_dump(), strict=True)
                for v in document_versions
            ]
            if len({v.document_id for v in versions}) != len(versions):
                raise ValueError("Each document must have exactly one pinned version")
        except (AttributeError, ValueError, TypeError):
            error = AgentError("INVALID_INPUT", "分析需要有效用户、岗位和不重复的固定文档版本。")
            error.trace_id = trace_id
            raise error from None

        allowed_versions = {(v.document_id, v.version) for v in versions}
        catalog: dict[str, Evidence] = {}
        events: list[StageEvent] = []
        usage = TokenUsage()
        tool_calls = 0
        model_requests = 0
        evidence_chars = 0
        actual_model: str | None = None

        def record(stage, started, requirement_id=None):
            events.append(
                StageEvent(
                    stage=stage,
                    requirement_id=requirement_id,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    tool_calls=tool_calls,
                    model_requests=model_requests,
                )
            )

        async def ask(system: str, payload: dict, schema: type[BaseModel]):
            nonlocal actual_model, model_requests
            try:
                async with asyncio.timeout(settings.model_timeout_seconds):
                    completion = await self.model.complete(
                        system=system, payload=payload, output_schema=schema
                    )
            except TimeoutError:
                raise AgentError("MODEL_TIMEOUT", "生成模型调用超时。", retryable=True) from None
            if not isinstance(completion, ModelCompletion):
                raise AgentError("MODEL_OUTPUT_INVALID", "模型适配器未返回约定结构。")
            try:
                completion = ModelCompletion.model_validate(completion.model_dump(), strict=True)
                if len(completion.content.encode("utf-8")) > settings.max_response_bytes:
                    raise ValueError("Oversized JSON")
                result = schema.model_validate_json(completion.content, strict=True)
            except (ValueError, ValidationError):
                raise AgentError(
                    "MODEL_OUTPUT_INVALID", "模型 JSON 不符合结构或三类状态约定。"
                ) from None
            model_requests += completion.attempts
            if model_requests > 2 * settings.max_attempts:
                raise AgentError("BUDGET_EXCEEDED", "模型请求次数超过本轮预算。")
            if actual_model is not None and actual_model != completion.model_version:
                raise AgentError("MODEL_OUTPUT_INVALID", "本轮模型版本发生变化，请重新分析。")
            actual_model = completion.model_version
            usage.input_tokens += completion.usage.input_tokens
            usage.output_tokens += completion.usage.output_tokens
            return result

        async def decompose(state: AnalysisState):
            nonlocal current_stage
            current_stage = "decompose"
            started = time.monotonic()
            draft = await ask(
                DECOMPOSE_PROMPT,
                {
                    "job_title": job.title,
                    "raw_jd": job.raw_jd,
                    "maximum_requirements": settings.max_requirements,
                },
                RequirementBatch,
            )
            if len(draft.requirements) > settings.max_requirements:
                raise AgentError("BUDGET_EXCEEDED", "岗位要求超过本轮上限；请缩小 JD 范围后重试。")
            texts = [requirement.text for requirement in draft.requirements]
            if len(set(texts)) != len(texts) or any(text not in job.raw_jd for text in texts):
                raise AgentError("MODEL_OUTPUT_INVALID", "岗位拆解含重复内容或非 JD 原文要求。")
            requirements = [
                Requirement(id=f"req_{i + 1:03d}", **item.model_dump())
                for i, item in enumerate(draft.requirements)
            ]
            record("decompose", started)
            return {"requirements": requirements}

        async def retrieve(state: AnalysisState):
            nonlocal current_stage, tool_calls, evidence_chars
            current_stage = "retrieve"
            by_requirement = {}
            for requirement in state["requirements"]:
                if tool_calls >= settings.max_tool_calls:
                    raise AgentError("BUDGET_EXCEEDED", "证据检索次数超过本轮预算。")
                started = time.monotonic()
                tool_calls += 1
                try:
                    async with asyncio.timeout(settings.tool_timeout_seconds):
                        found = await retriever.retrieve_evidence(
                            user_id=user_id,
                            query=requirement.text,
                            document_versions=[v.model_copy(deep=True) for v in versions],
                            top_k=settings.top_k,
                        )
                except TimeoutError:
                    raise AgentError(
                        "RETRIEVAL_TIMEOUT", "证据检索超时。", retryable=True
                    ) from None
                except AgentError:
                    raise
                except Exception as exc:
                    source_code = getattr(exc, "code", None)
                    if source_code in {"DOCUMENT_NOT_READY", "DOCUMENT_PARSE_FAILED"}:
                        raise AgentError(
                            source_code, "资料未就绪或解析失败，不能生成成功分析。"
                        ) from exc
                    raise AgentError(
                        "RETRIEVAL_FAILED", "检索服务失败，本轮未生成分析结果。"
                    ) from exc
                if not isinstance(found, list) or len(found) > settings.top_k:
                    raise AgentError("EVIDENCE_INVALID", "检索结果格式或数量不符合证据契约。")
                unique: dict[str, Evidence] = {}
                for item in found:
                    if not isinstance(item, Evidence):
                        raise AgentError(
                            "EVIDENCE_INVALID", "需要 B/C 提供含原文与定位的完整 Evidence。"
                        )
                    try:
                        evidence = Evidence.model_validate(item.model_dump(), strict=True)
                    except ValidationError:
                        raise AgentError("EVIDENCE_INVALID", "证据原文或定位不符合契约。") from None
                    if (
                        evidence.source_type != "personal"
                        or (evidence.document_id, evidence.version) not in allowed_versions
                    ):
                        raise AgentError("EVIDENCE_INVALID", "证据不属于本次个人资料版本快照。")
                    previous = catalog.get(evidence.chunk_id)
                    if previous is not None and previous != evidence:
                        raise AgentError("EVIDENCE_INVALID", "相同片段 ID 对应了不同原文或定位。")
                    catalog[evidence.chunk_id] = evidence
                    unique[evidence.chunk_id] = evidence
                evidence_chars += sum(len(item.quote) for item in unique.values())
                if evidence_chars > settings.max_evidence_chars:
                    raise AgentError("BUDGET_EXCEEDED", "证据上下文超出本轮大小限制。")
                by_requirement[requirement.id] = list(unique.values())
                record("retrieve", started, requirement.id)
            return {"evidence_by_requirement": by_requirement}

        async def assess(state: AnalysisState):
            nonlocal current_stage
            current_stage = "assess"
            started = time.monotonic()
            payload = {
                "requirements": [
                    {
                        **requirement.model_dump(),
                        "evidence": [
                            evidence.model_dump()
                            for evidence in state["evidence_by_requirement"][requirement.id]
                        ],
                    }
                    for requirement in state["requirements"]
                ]
            }
            assessment = await ask(ASSESS_PROMPT, payload, AssessmentBatch)
            expected_ids = {requirement.id for requirement in state["requirements"]}
            returned_ids = [item.requirement_id for item in assessment.assessments]
            if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != expected_ids:
                raise AgentError("MODEL_OUTPUT_INVALID", "模型遗漏、重复或增加了岗位要求。")
            for item in assessment.assessments:
                allowed_ids = {
                    e.chunk_id for e in state["evidence_by_requirement"][item.requirement_id]
                }
                if not set(item.evidence_ids) <= allowed_ids:
                    raise AgentError("EVIDENCE_INVALID", "模型引用了该要求未实际检索到的证据。")
            record("assess", started)
            return {"assessments": assessment.assessments}

        async def assemble(state: AnalysisState):
            nonlocal current_stage
            current_stage = "assemble"
            started = time.monotonic()
            decisions = {item.requirement_id: item for item in state["assessments"]}
            warnings = []
            if getattr(self.model, "is_test_double", False) or getattr(
                retriever, "is_test_double", False
            ):
                warnings.append("包含明确标注的测试替身；本结果不是完整真实模型/RAG 联调结果。")
            retrieval_version = getattr(retriever, "retrieval_config_version", None)
            if not isinstance(retrieval_version, str) or not retrieval_version.strip():
                retrieval_version = None
                warnings.append("检索适配器未提供配置版本；由 B 接入时补齐，不伪造版本。")
            matches = []
            for requirement in state["requirements"]:
                decision = decisions[requirement.id]
                matches.append(
                    Match(
                        requirement_id=requirement.id,
                        requirement=requirement.text,
                        category=requirement.category,
                        status=decision.status,
                        evidence_refs=[
                            EvidenceRef(
                                chunk_id=catalog[cid].chunk_id,
                                document_id=catalog[cid].document_id,
                                version=catalog[cid].version,
                            )
                            for cid in decision.evidence_ids
                        ],
                        explanation=decision.explanation,
                        missing_information=decision.missing_information,
                    )
                )
            result = AnalysisResult(
                id=f"analysis_{uuid4().hex}",
                job_id=job.id,
                job_version=job.version,
                document_versions=[v.model_copy(deep=True) for v in versions],
                stale=False,
                matches=matches,
                warnings=warnings,
                trace_id=trace_id,
                prompt_version=PROMPT_VERSION,
                retrieval_config_version=retrieval_version,
                model_version=actual_model,
            )
            record("assemble", started)
            return {"result": result}

        graph = StateGraph(AnalysisState)
        graph.add_node("decompose_requirements", decompose)
        graph.add_node("retrieve_evidence", retrieve)
        graph.add_node("assess_requirements", assess)
        graph.add_node("assemble_result", assemble)
        graph.add_edge(START, "decompose_requirements")
        graph.add_edge("decompose_requirements", "retrieve_evidence")
        graph.add_edge("retrieve_evidence", "assess_requirements")
        graph.add_edge("assess_requirements", "assemble_result")
        graph.add_edge("assemble_result", END)
        compiled = graph.compile()
        try:
            # Override inherited/global LangSmith settings without changing process environment.
            with tracing_context(enabled=False, parent=False):
                async with asyncio.timeout(settings.total_timeout_seconds):
                    final = await compiled.ainvoke(
                        {}, config={"recursion_limit": 8, "callbacks": []}
                    )
            result = final["result"]
            cited = {ref.chunk_id for match in result.matches for ref in match.evidence_refs}
            return AnalysisRun(
                result=result,
                graph_version=GRAPH_VERSION,
                requirement_snapshot=final["requirements"],
                evidence=[e.model_copy(deep=True) for key, e in catalog.items() if key in cited],
                events=events,
                usage=usage,
            )
        except AgentError as exc:
            exc.trace_id = trace_id
            raise
        except TimeoutError:
            error = AgentError(
                "ANALYSIS_TIMEOUT", "分析超过总执行时限，本轮未生成结果。", retryable=True
            )
            error.trace_id = trace_id
            raise error from None
        except Exception:
            error = AgentError(
                "ANALYSIS_FAILED", f"分析在 {current_stage} 阶段失败，本轮未生成结果。"
            )
            error.trace_id = trace_id
            raise error from None


def get_analysis_agent(settings: AgentSettings | None = None) -> LangGraphAnalysisAgent:
    settings = settings or get_agent_settings()
    return LangGraphAnalysisAgent(ChatCompletionModel(settings), settings)
