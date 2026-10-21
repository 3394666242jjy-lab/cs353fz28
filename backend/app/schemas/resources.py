from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

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


class DocumentResult(ContractModel):
    id: str
    kind: Literal["resume", "project"]
    filename: str
    current_version: int = Field(ge=1)
    status: Literal["uploaded", "processing", "ready", "failed", "deleted"]
    error: str | None = None


class DocumentPage(ContractModel):
    items: list[DocumentResult]
    next_cursor: str | None = None


class DocumentAccepted(ContractModel):
    document_id: str
    execution_id: str
    status: Literal["processing"] = "processing"


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
