from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.schemas.analysis import AnalysisCreate, AnalysisResult, Evidence
from app.schemas.common import Envelope, ErrorEnvelope
from app.schemas.resources import (
    DocumentAccepted,
    DocumentPage,
    DocumentResult,
    ExecutionAccepted,
    ExecutionResult,
    JobCreate,
    JobCreated,
    JobResult,
    LoginRequest,
    TokenResult,
    UserResult,
)

router = APIRouter(
    prefix="/api/v1",
    responses={501: {"model": ErrorEnvelope, "description": "Business module not implemented"}},
)


def not_implemented(owner: str) -> NoReturn:
    raise HTTPException(
        status_code=501,
        detail={"code": "NOT_IMPLEMENTED", "message": f"骨架接口，业务待 {owner} 实现。"},
    )


@router.post("/auth/login", response_model=Envelope[TokenResult], tags=["auth"])
async def login(payload: LoginRequest):
    not_implemented("B（#7）")


@router.get("/me", response_model=Envelope[UserResult], tags=["auth"])
async def get_me():
    not_implemented("B（#7）")


@router.post(
    "/documents", response_model=Envelope[DocumentAccepted], status_code=202, tags=["documents"]
)
async def upload_document(
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["resume", "project"], Form()],
):
    await file.close()
    not_implemented("B/C（#7、#8）")


@router.get("/documents", response_model=Envelope[DocumentPage], tags=["documents"])
async def list_documents(
    cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20
):
    not_implemented("B（#7）")


@router.get("/documents/{document_id}", response_model=Envelope[DocumentResult], tags=["documents"])
async def get_document(document_id: str):
    not_implemented("B（#7）")


@router.post("/jobs", response_model=Envelope[JobCreated], status_code=201, tags=["jobs"])
async def create_job(payload: JobCreate):
    not_implemented("B（#7）")


@router.get("/jobs/{job_id}", response_model=Envelope[JobResult], tags=["jobs"])
async def get_job(job_id: str):
    not_implemented("B（#7）")


@router.post(
    "/analyses", response_model=Envelope[ExecutionAccepted], status_code=202, tags=["analysis"]
)
async def create_analysis(payload: AnalysisCreate):
    not_implemented("B/D（#7、#9）")


@router.get("/analyses/{analysis_id}", response_model=Envelope[AnalysisResult], tags=["analysis"])
async def get_analysis(analysis_id: str):
    not_implemented("B/D（#7、#9）")


@router.get(
    "/executions/{execution_id}", response_model=Envelope[ExecutionResult], tags=["execution"]
)
async def get_execution(execution_id: str):
    not_implemented("B（#7）")


@router.get("/evidence/{chunk_id}", response_model=Envelope[Evidence], tags=["evidence"])
async def get_evidence(chunk_id: str, version: Annotated[int, Query(ge=1)]):
    not_implemented("B/C（#7、#8）")
