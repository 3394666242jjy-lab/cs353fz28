from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pymongo.errors import PyMongoError

from app.api.dependencies import current_user, database_unavailable, request_settings, unauthorized
from app.config import Settings
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
from app.services.auth import login_user
from app.services.documents import get_document as documents_get
from app.services.documents import list_documents as documents_list
from app.services.executions import get_execution as executions_get
from app.services.jobs import load_job, save_job

router = APIRouter(
    prefix="/api/v1",
    responses={501: {"model": ErrorEnvelope, "description": "Business module not implemented"}},
)


def not_implemented(owner: str) -> NoReturn:
    raise HTTPException(
        status_code=501,
        detail={"code": "NOT_IMPLEMENTED", "message": f"骨架接口，业务待 {owner} 实现。"},
    )


AUTH_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "Invalid credentials or expired session"},
    503: {"model": ErrorEnvelope, "description": "Database unavailable"},
}


@router.post(
    "/auth/login", response_model=Envelope[TokenResult], tags=["auth"], responses=AUTH_ERRORS
)
def login(
    payload: LoginRequest,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
):
    try:
        token = login_user(settings, payload.email, payload.password)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if token is None:
        raise unauthorized()
    return Envelope(data=token, request_id=request.state.request_id)


@router.get("/me", response_model=Envelope[UserResult], tags=["auth"], responses=AUTH_ERRORS)
def get_me(request: Request, user: Annotated[UserResult, Depends(current_user)]):
    return Envelope(data=user, request_id=request.state.request_id)


@router.post(
    "/documents", response_model=Envelope[DocumentAccepted], status_code=202, tags=["documents"]
)
async def upload_document(
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["resume", "project"], Form()],
):
    await file.close()
    not_implemented("B/C（#7、#8）")


@router.get(
    "/documents",
    response_model=Envelope[DocumentPage],
    tags=["documents"],
    responses=AUTH_ERRORS,
)
def list_documents(
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    try:
        items, next_cursor = documents_list(settings, user.id, cursor=cursor, limit=limit)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    return Envelope(
        data=DocumentPage(items=items, next_cursor=next_cursor),
        request_id=request.state.request_id,
    )


@router.get(
    "/documents/{document_id}",
    response_model=Envelope[DocumentResult],
    tags=["documents"],
    responses=AUTH_ERRORS | {404: {"model": ErrorEnvelope, "description": "Document not found"}},
)
def get_document(
    document_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        document = documents_get(settings, user.id, document_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if document is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "资料不存在或不属于当前用户。"},
        )
    return Envelope(data=document, request_id=request.state.request_id)


@router.post(
    "/jobs",
    response_model=Envelope[JobCreated],
    status_code=201,
    tags=["jobs"],
    responses=AUTH_ERRORS,
)
def create_job(
    payload: JobCreate,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        result = save_job(settings, user.id, payload)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    return Envelope(data=result, request_id=request.state.request_id)


@router.get(
    "/jobs/{job_id}", response_model=Envelope[JobResult], tags=["jobs"], responses=AUTH_ERRORS
)
def get_job(
    job_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        result = load_job(settings, user.id, job_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if result is None:
        raise HTTPException(
            status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "资源不存在。"}
        )
    return Envelope(data=result, request_id=request.state.request_id)


@router.post(
    "/analyses", response_model=Envelope[ExecutionAccepted], status_code=202, tags=["analysis"]
)
async def create_analysis(payload: AnalysisCreate):
    not_implemented("B/D（#7、#9）")


@router.get("/analyses/{analysis_id}", response_model=Envelope[AnalysisResult], tags=["analysis"])
async def get_analysis(analysis_id: str):
    not_implemented("B/D（#7、#9）")


@router.get(
    "/executions/{execution_id}",
    response_model=Envelope[ExecutionResult],
    tags=["execution"],
    responses=AUTH_ERRORS | {404: {"model": ErrorEnvelope, "description": "Execution not found"}},
)
def get_execution(
    execution_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        execution = executions_get(settings, user.id, execution_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if execution is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "任务不存在或不属于当前用户。"},
        )
    return Envelope(data=execution, request_id=request.state.request_id)


@router.get("/evidence/{chunk_id}", response_model=Envelope[Evidence], tags=["evidence"])
async def get_evidence(chunk_id: str, version: Annotated[int, Query(ge=1)]):
    not_implemented("B/C（#7、#8）")
