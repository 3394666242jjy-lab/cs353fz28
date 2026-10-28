"""Internal structured model output and the optional typed handoff to B."""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.analysis import AnalysisResult, Evidence
from app.schemas.common import ContractModel


class RequirementDraft(ContractModel):
    text: str = Field(min_length=1, max_length=2000)
    category: Literal["required", "preferred"]


class RequirementBatch(ContractModel):
    # Permit overflow in the schema so code can explicitly fail, rather than silently drop items.
    requirements: list[RequirementDraft] = Field(min_length=1, max_length=64)


class Requirement(RequirementDraft):
    id: str = Field(min_length=1)


class Assessment(ContractModel):
    requirement_id: str = Field(min_length=1)
    status: Literal["supported", "partial", "not_evidenced"]
    evidence_ids: list[str] = Field(max_length=10)
    explanation: str = Field(min_length=1, max_length=3000)
    missing_information: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_semantics(self):
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Evidence IDs must be unique")
        if any(not item.strip() or len(item) > 1000 for item in self.missing_information):
            raise ValueError("Missing-information entries must be nonblank and bounded")
        if self.status == "not_evidenced" and self.evidence_ids:
            raise ValueError("not_evidenced cannot cite evidence")
        if self.status in {"supported", "partial"} and not self.evidence_ids:
            raise ValueError("supported/partial must cite evidence")
        if self.status == "supported" and self.missing_information:
            raise ValueError("supported cannot have unresolved missing information")
        if self.status == "partial" and not self.missing_information:
            raise ValueError("partial must explain what is missing")
        return self


class AssessmentBatch(ContractModel):
    assessments: list[Assessment] = Field(min_length=1, max_length=8)


class TokenUsage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelCompletion(ContractModel):
    content: str = Field(repr=False, min_length=1)
    model_version: str = Field(min_length=1, max_length=200)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    attempts: int = Field(default=1, ge=1, le=2)


class StageEvent(ContractModel):
    stage: Literal["decompose", "retrieve", "assess", "assemble"]
    requirement_id: str | None = None
    duration_ms: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    model_requests: int = Field(ge=0)


class AnalysisRun(ContractModel):
    """Optional handoff; B still revalidates ownership/readiness before persistence."""

    result: AnalysisResult
    graph_version: str = Field(min_length=1)
    requirement_snapshot: list[Requirement]
    evidence: list[Evidence]
    events: list[StageEvent]
    usage: TokenUsage
