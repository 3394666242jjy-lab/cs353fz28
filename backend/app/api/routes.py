import asyncio
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from app.api.deps import get_current_user
from app.config import get_settings
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
from app.security import create_access_token
from app.services.users import authenticate_user, public_user

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
async def login(request: Request, payload: LoginRequest):
    user = await asyncio.to_thread(authenticate_user, payload.email, payload.password)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "邮箱或密码不正确。"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(get_settings(), user["id"], user["email"])
    return Envelope(
        data=TokenResult(access_token=token, expires_in=expires_in),
        request_id=request.state.request_id,
    )


@router.get("/me", response_model=Envelope[UserResult], tags=["auth"])
async def get_me(request: Request, current_user: Annotated[dict, Depends(get_current_user)]):
    return Envelope(
        data=UserResult(**public_user(current_user)),
        request_id=request.state.request_id,
    )


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
