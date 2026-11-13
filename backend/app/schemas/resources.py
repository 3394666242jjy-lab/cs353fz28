from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.common import ContractModel


class LoginRequest(ContractModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class TokenResult(ContractModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserResult(ContractModel):
    id: str
    email: str
    display_name: str


class ExtractedPage(ContractModel):
    page: int = Field(ge=1)
    text: str


class DocumentExtraction(ContractModel):
    """当前版本的可预览提取内容；PDF 保留原始页码（dev/03）。"""

    kind: Literal["text", "pdf"]
    text: str | None = None
    pages: list[ExtractedPage] | None = None


class DocumentResult(ContractModel):
    id: str
    kind: Literal["resume", "project"]
    filename: str
    current_version: int = Field(ge=1)
    status: Literal["uploaded", "processing", "ready", "failed", "deleted"]
    error: str | None = None
    # GET /documents/{id} 附带当前版本的提取结果（dev/04）；列表不携带。
    extraction: DocumentExtraction | None = None


class DocumentPage(ContractModel):
    items: list[DocumentResult]
    next_cursor: str | None = None


class DocumentAccepted(ContractModel):
    document_id: str
    execution_id: str
    status: Literal["processing"] = "processing"


class DocumentDeleted(ContractModel):
    """DELETE /documents/{id} 的 202 响应：删除已生效，清理异步进行。"""

    document_id: str
    status: Literal["deleted"] = "deleted"
    purge_status: Literal["pending", "done", "failed"]


class DocumentTextUpdate(ContractModel):
    """PUT /documents/{id}/text：编辑提取文本并生成新版本。"""

    extracted_text: str = Field(min_length=1, max_length=10 * 1024 * 1024)
    expected_version: int = Field(ge=1)

    @field_validator("extracted_text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("提取文本不能只包含空白字符")
        return value


class ExpectedVersion(ContractModel):
    expected_version: int = Field(ge=1)


class DocumentVersionAccepted(ContractModel):
    document_id: str
    version: int = Field(ge=1)
    execution_id: str


class JobCreate(ContractModel):
    title: str = Field(min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    raw_jd: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=30000
    )

    @field_validator("raw_jd")
    @classmethod
    def reject_blank_jd(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("JD cannot contain only whitespace")
        return value


class JobCreated(ContractModel):
    job_id: str
    version: int = Field(ge=1)


class JobUpdate(ContractModel):
    expected_version: int = Field(ge=1)
    raw_jd: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=30000
    )

    @field_validator("raw_jd")
    @classmethod
    def reject_blank_jd(cls, value: str) -> str:
        return JobCreate.reject_blank_jd(value)


class JobResult(JobCreate):
    id: str
    version: int = Field(ge=1)


class ExecutionAccepted(ContractModel):
    execution_id: str


class ExecutionError(ContractModel):
    code: str
    message: str


class PendingInput(ContractModel):
    kind: Literal["clarification", "plan_confirmation"]
    interrupt_id: str
    checkpoint_id: str
    question: str | None = None
    plan_id: str | None = None


class ExecutionResult(ContractModel):
    id: str
    type: Literal["index", "analysis", "plan"]
    status: Literal["queued", "running", "waiting_for_input", "succeeded", "failed"]
    state_version: int = Field(ge=1)
    result_type: Literal["document", "analysis", "plan"] | None = None
    result_id: str | None = None
    pending_input: PendingInput | None = None
    error: ExecutionError | None = None
    latest_event_id: str | None = None

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "succeeded" and (self.result_type is None or not self.result_id):
            raise ValueError("succeeded requires a result")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed requires an error")
        if self.status == "waiting_for_input" and self.pending_input is None:
            raise ValueError("waiting_for_input requires pending_input")
        if self.status != "waiting_for_input" and self.pending_input is not None:
            raise ValueError("only waiting_for_input can have pending_input")
        if self.status != "failed" and self.error is not None:
            raise ValueError("only failed can have an error")
        return self


class ResumeAnswer(ContractModel):
    """POST /executions/{id}/resume 的用户回答（dev/04 M2）。

    自由文本属于待核实补充，不自动升级为有文档证据的经历；为空时必须
    显式选择 continue_without_evidence 或提供新上传并就绪的资料。
    """

    text: str = Field(default="", max_length=8000)
    continue_without_evidence: StrictBool = False
    document_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_answer(self):
        if self.continue_without_evidence and self.document_ids:
            raise ValueError("选择按现有资料继续时不能同时提交新资料")
        if any(not item.strip() for item in self.document_ids):
            raise ValueError("document_ids 不能包含空白项")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("document_ids 不能重复")
        if not self.text.strip() and not self.continue_without_evidence and not self.document_ids:
            raise ValueError(
                "回答为空时必须选择 continue_without_evidence 或提供新资料 document_ids"
            )
        return self


class ResumeRequest(ContractModel):
    interrupt_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    answer: ResumeAnswer


class PlanCreate(ContractModel):
    """POST /plans：从一份就绪且未过期的分析生成准备计划草稿。"""

    analysis_id: str = Field(min_length=1)
    deadline: date
    timezone: str = Field(min_length=1, max_length=64)
    daily_minutes: int = Field(ge=1, le=1440, strict=True)


class EvidenceRefLike(ContractModel):
    """草稿任务引用的资料来源（chunk 定位，可选）。"""

    document_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    chunk_id: str | None = None


class PlanDraftTask(ContractModel):
    """计划草稿任务（D 生成，确认前不成为正式任务）。"""

    title: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)
    scheduled_date: date
    estimated_minutes: int = Field(ge=1)
    source_refs: list[EvidenceRefLike] = Field(default_factory=list)


class PlanTask(ContractModel):
    """确认后的正式任务（稳定 ID，幂等写入）。"""

    id: str
    plan_id: str
    title: str
    requirement_ids: list[str]
    scheduled_date: date
    estimated_minutes: int = Field(ge=1)
    status: Literal["todo", "in_progress", "done"]
    source_refs: list[EvidenceRefLike] = Field(default_factory=list)


class PlanResult(ContractModel):
    id: str
    analysis_id: str
    execution_id: str
    status: Literal["draft", "confirmed"]
    deadline: date
    timezone: str
    daily_minutes: int = Field(ge=1, le=1440)
    draft_tasks: list[PlanDraftTask] = Field(default_factory=list)
    tasks: list[PlanTask] | None = None
    confirmed_at: datetime | None = None
    created_at: datetime


class PlanConfirmed(ContractModel):
    """POST /plans/{id}/confirm 的 200 响应；重放返回同一结果。"""

    plan: PlanResult
    execution_id: str
    resumed: bool


class PlanTaskStatusUpdate(ContractModel):
    status: Literal["todo", "in_progress", "done"]
