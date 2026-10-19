from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import ContractModel


class DocumentVersion(ContractModel):
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class EvidenceRef(DocumentVersion):
    chunk_id: str = Field(min_length=1)


class TextLocator(ContractModel):
    kind: Literal["text"]
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_lines(self):
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class PdfLocator(ContractModel):
    kind: Literal["pdf"]
    page: int = Field(ge=1)


class Evidence(EvidenceRef):
    filename: str
    source_type: Literal["personal", "public"]
    locator: TextLocator | PdfLocator = Field(discriminator="kind")
    quote: str = Field(min_length=1)


class Match(ContractModel):
    requirement_id: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    category: Literal["required", "preferred"]
    status: Literal["supported", "partial", "not_evidenced"]
    evidence_refs: list[EvidenceRef]
    explanation: str = Field(min_length=1)
    missing_information: list[str]

    @model_validator(mode="after")
    def validate_evidence(self):
        if self.status == "not_evidenced" and self.evidence_refs:
            raise ValueError("not_evidenced cannot reference evidence")
        if self.status != "not_evidenced" and not self.evidence_refs:
            raise ValueError("supported/partial requires evidence")
        return self


class AnalysisCreate(ContractModel):
    job_id: str = Field(min_length=1)
    document_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_documents(self):
        if any(not item.strip() for item in self.document_ids):
            raise ValueError("document IDs cannot be blank")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("document IDs must be unique")
        return self


class AnalysisResult(ContractModel):
    id: str
    job_id: str
    job_version: int = Field(ge=1)
    document_versions: list[DocumentVersion]
    stale: bool
    matches: list[Match]
    warnings: list[str]
    trace_id: str | None = None
    prompt_version: str | None = None
    retrieval_config_version: str | None = None
    model_version: str | None = None

    @model_validator(mode="after")
    def validate_references(self):
        versions = {(item.document_id, item.version) for item in self.document_versions}
        ids = [match.requirement_id for match in self.matches]
        if len(set(ids)) != len(ids):
            raise ValueError("requirement IDs must be unique")
        for match in self.matches:
            for ref in match.evidence_refs:
                if (ref.document_id, ref.version) not in versions:
                    raise ValueError("evidence must reference the input document versions")
        return self
