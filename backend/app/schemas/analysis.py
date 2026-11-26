from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import ContractModel


class DocumentVersion(ContractModel):
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class EvidenceRef(DocumentVersion):
    chunk_id: str = Field(min_length=1)


class TextLocator(ContractModel):
    """文本定位：行号是权威，字符区间是可选精化。

    ``char_start``/``char_end`` 相对解析后的归一化全文（LF 换行），0 起始、半开区间，
    语义见 dev/19。M2 的 chunk 等于整行区间，因此这两个字段可以省略；一旦出现就必须
    同时出现，并且能精确切出 quote。缺省时消费方退回行号，不允许按比例换算。
    """

    kind: Literal["text"]
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_lines(self):
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class PdfLocator(ContractModel):
    """PDF 定位：页码是权威，字符区间是可选精化。

    ``char_start``/``char_end`` 相对**该页**提取文本（LF 归一化），0 起始、半开区间，
    语义见 dev/19。刻意用页内偏移而不是全文偏移：全文偏移要跨越空白页记账，解析器一改
    （例如 OCR 版本）就整体漂移。缺省时消费方只按页码定位，不允许按比例换算。
    """

    kind: Literal["pdf"]
    page: int = Field(ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_span(self):
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


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
