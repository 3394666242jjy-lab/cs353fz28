"""M3 context boundary prototype; kept separate from the M2 production graph."""

from typing import Literal

from pydantic import Field

from app.rag.knowledge import KnowledgeReference, Role
from app.schemas.analysis import Evidence, EvidenceRef
from app.schemas.common import ContractModel

Intent = Literal["match_personal_experience", "learn_topic", "prepare_for_role"]


class RetrievalPlan(ContractModel):
    role: Role
    personal: bool
    public: bool
    max_retrieval_rounds: int = 2
    max_reference_chars: int = Field(default=6000, ge=1, le=16000)


def route_retrieval(role: Role, intent: Intent) -> RetrievalPlan:
    if intent not in {"match_personal_experience", "learn_topic", "prepare_for_role"}:
        raise ValueError("Unknown task intent")
    return RetrievalPlan(
        role=role, personal=intent != "learn_topic", public=intent != "match_personal_experience"
    )


def validate_personal_citations(refs: list[EvidenceRef], returned: list[Evidence]) -> None:
    allowed = {
        (e.document_id, e.version, e.chunk_id) for e in returned if e.source_type == "personal"
    }
    if any((r.document_id, r.version, r.chunk_id) not in allowed for r in refs):
        raise ValueError("Only live personal retrieval evidence may support a personal claim")


class ResearchContext(ContractModel):
    role: Role
    personal_evidence: list[Evidence] = Field(default_factory=list)
    reference_knowledge: list[KnowledgeReference] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    notice: str = "公共资料仅供知识解释与学习建议，不能证明用户具有某项经历。"


def compose_context(
    plan: RetrievalPlan, *, personal: list[Evidence], knowledge: list[KnowledgeReference]
) -> ResearchContext:
    if personal and not plan.personal or knowledge and not plan.public:
        raise ValueError("Retrieved corpus is not allowed for this task intent")
    if any(e.source_type != "personal" for e in personal):
        raise ValueError("Public material must not enter personal evidence")
    if any(plan.role not in item.roles for item in knowledge):
        raise ValueError("Reference does not match the requested role")
    if sum(len(item.summary) for item in knowledge) > plan.max_reference_chars:
        raise ValueError("Reference context exceeds its budget")
    missing = ["个人资料中尚未检索到相关证据"] if plan.personal and not personal else []
    return ResearchContext(
        role=plan.role,
        personal_evidence=personal,
        reference_knowledge=knowledge,
        missing_information=missing,
    )
