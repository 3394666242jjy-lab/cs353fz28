from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import TypeAdapter
from pymongo.errors import PyMongoError

from app.api.dependencies import current_user, database_unavailable, request_settings, unauthorized
from app.config import Settings
from app.schemas.analysis import (
    AnalysisCreate,
    AnalysisResult,
    DocumentVersion,
    Evidence,
    PdfLocator,
    TextLocator,
)
from app.schemas.common import Envelope, ErrorEnvelope
from app.schemas.resources import (
    DocumentAccepted,
    DocumentPage,
    DocumentResult,
    ExecutionAccepted,
    ExecutionError,
    ExecutionResult,
    JobCreate,
    JobCreated,
    JobResult,
    LoginRequest,
    TokenResult,
    UserResult,
)
from app.services.analyses import STATUS_FAILED, STATUS_READY, set_analysis_failed
from app.services.analyses import create_analysis as analyses_create
from app.services.analyses import get_analysis as analyses_get
from app.services.auth import login_user
from app.services.documents import create_document, set_document_status
from app.services.documents import get_chunk as chunks_get
from app.services.documents import get_document as documents_get
from app.services.documents import list_documents as documents_list
from app.services.executions import ExecutionCreate, create_execution, transition_execution
from app.services.executions import get_execution as executions_get
from app.services.jobs import load_job, save_job
from app.services.storage import remove_upload, save_upload
from app.services.tasks import dispatch_analysis_task, dispatch_index_task

router = APIRouter(
    prefix="/api/v1",
    responses={501: {"model": ErrorEnvelope, "description": "Business module not implemented"}},
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
LocatorAdapter: TypeAdapter[TextLocator | PdfLocator] = TypeAdapter(TextLocator | PdfLocator)


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


UPLOAD_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "Invalid or missing bearer token"},
    413: {"model": ErrorEnvelope, "description": "File exceeds the 10 MiB limit"},
    415: {"model": ErrorEnvelope, "description": "Unsupported file type"},
    422: {"model": ErrorEnvelope, "description": "Invalid request parameters"},
    503: {"model": ErrorEnvelope, "description": "Database unavailable or task dispatch failed"},
}


@router.post(
    "/documents",
    response_model=Envelope[DocumentAccepted],
    status_code=202,
    tags=["documents"],
    responses=UPLOAD_ERRORS,
)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["resume", "project"], Form()],
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    raw = await file.read()
    await file.close()
    if not raw:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_INPUT", "message": "文件内容为空。"},
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "FILE_TOO_LARGE", "message": "文件超过 10 MiB 限制。"},
        )
    filename = Path(file.filename or "").name
    media_type = UPLOAD_MEDIA_TYPES.get(Path(filename).suffix.lower())
    if media_type is None:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_FILE_TYPE",
                "message": "仅支持有文本层的 PDF 或 UTF-8 TXT/Markdown 文件。",
            },
        )
    storage_key = save_upload(settings, user.id, filename, raw)
    try:
        document_id = create_document(
            settings,
            user.id,
            kind=kind,
            filename=filename,
            media_type=media_type,
            size_bytes=len(raw),
            storage_key=storage_key,
        )
        execution_id = create_execution(
            settings, user.id, ExecutionCreate(type="index", resource_id=document_id)
        )
    except PyMongoError as exc:
        remove_upload(settings, storage_key)
        raise database_unavailable() from exc
    try:
        dispatch_index_task(
            settings,
            storage_key=storage_key,
            user_id=user.id,
            document_id=document_id,
            version=1,
            execution_id=execution_id,
        )
    except Exception as exc:
        # dev/08: a dispatch failure must fail the execution explicitly,
        # never leave it queued forever.
        transition_execution(
            settings,
            user.id,
            execution_id,
            expected_state_version=1,
            status="failed",
            error=ExecutionError(code="TASK_DISPATCH_FAILED", message="索引任务投递失败，请重试。"),
        )
        set_document_status(
            settings, user.id, document_id, status="failed", error="索引任务投递失败，请重试。"
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "TASK_DISPATCH_FAILED", "message": "索引任务投递失败，请重试。"},
        ) from exc
    return Envelope(
        data=DocumentAccepted(document_id=document_id, execution_id=execution_id),
        request_id=request.state.request_id,
    )


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


ANALYSIS_WRITE_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "Invalid or missing bearer token"},
    404: {"model": ErrorEnvelope, "description": "Job or document not found"},
    409: {"model": ErrorEnvelope, "description": "A referenced document is not ready"},
    503: {"model": ErrorEnvelope, "description": "Database unavailable or task dispatch failed"},
}

ANALYSIS_READ_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "Invalid or missing bearer token"},
    404: {"model": ErrorEnvelope, "description": "Analysis not found"},
    409: {"model": ErrorEnvelope, "description": "Analysis is not in a terminal ready state"},
    503: {"model": ErrorEnvelope, "description": "Database unavailable"},
}


@router.post(
    "/analyses",
    response_model=Envelope[ExecutionAccepted],
    status_code=202,
    tags=["analysis"],
    responses=ANALYSIS_WRITE_ERRORS,
)
def create_analysis(
    payload: AnalysisCreate,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        job = load_job(settings, user.id, payload.job_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "岗位不存在。"},
        )
    try:
        document_versions: list[DocumentVersion] = []
        for document_id in payload.document_ids:
            document = documents_get(settings, user.id, document_id)
            if document is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "NOT_FOUND", "message": "资料不存在或不属于当前用户。"},
                )
            if document.status != "ready":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "DOCUMENT_NOT_READY",
                        "message": f"资料 {document_id} 尚未索引完成，请稍后重试。",
                    },
                )
            document_versions.append(
                DocumentVersion(document_id=document.id, version=document.current_version)
            )
        analysis_id = analyses_create(
            settings,
            user.id,
            job_id=job.id,
            job_version=job.version,
            document_versions=document_versions,
        )
        execution_id = create_execution(
            settings, user.id, ExecutionCreate(type="analysis", resource_id=analysis_id)
        )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    try:
        dispatch_analysis_task(
            settings, analysis_id=analysis_id, user_id=user.id, execution_id=execution_id
        )
    except Exception as exc:
        # dev/08: a dispatch failure must fail the execution explicitly.
        transition_execution(
            settings,
            user.id,
            execution_id,
            expected_state_version=1,
            status="failed",
            error=ExecutionError(code="TASK_DISPATCH_FAILED", message="分析任务投递失败，请重试。"),
        )
        set_analysis_failed(settings, user.id, analysis_id, error="分析任务投递失败，请重试。")
        raise HTTPException(
            status_code=503,
            detail={"code": "TASK_DISPATCH_FAILED", "message": "分析任务投递失败，请重试。"},
        ) from exc
    return Envelope(
        data=ExecutionAccepted(execution_id=execution_id),
        request_id=request.state.request_id,
    )


def _analysis_is_stale(settings: Settings, user_id: str, row: dict) -> bool:
    """Compare the pinned versions against current job and document state."""
    job = load_job(settings, user_id, row["job_id"])
    if job is None or job.version != row["job_version"]:
        return True
    for item in row["document_versions"]:
        document = documents_get(settings, user_id, item["document_id"])
        if document is None or document.status == "deleted":
            return True
        if document.current_version != item["version"]:
            return True
    return False


@router.get(
    "/analyses/{analysis_id}",
    response_model=Envelope[AnalysisResult],
    tags=["analysis"],
    responses=ANALYSIS_READ_ERRORS,
)
def get_analysis(
    analysis_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        row = analyses_get(settings, user.id, analysis_id)
        if row is not None and row["status"] == STATUS_READY:
            stale = _analysis_is_stale(settings, user.id, row)
        else:
            stale = False
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "分析不存在或不属于当前用户。"},
        )
    if row["status"] == STATUS_FAILED:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ANALYSIS_FAILED",
                "message": row.get("error") or "分析执行失败。",
            },
        )
    if row["status"] != STATUS_READY:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ANALYSIS_NOT_READY",
                "message": "分析仍在进行中，请轮询执行状态。",
            },
        )
    try:
        result = AnalysisResult(
            id=row["id"],
            job_id=row["job_id"],
            job_version=row["job_version"],
            document_versions=row["document_versions"],
            stale=stale,
            matches=row["matches"],
            warnings=row["warnings"],
            trace_id=row.get("trace_id"),
            prompt_version=row.get("prompt_version"),
            retrieval_config_version=row.get("retrieval_config_version"),
            model_version=row.get("model_version"),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "INTERNAL_ERROR", "message": "分析记录格式异常，请通过请求 ID 排查。"},
        ) from exc
    return Envelope(data=result, request_id=request.state.request_id)


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


@router.get(
    "/evidence/{chunk_id}",
    response_model=Envelope[Evidence],
    tags=["evidence"],
    responses=AUTH_ERRORS
    | {404: {"model": ErrorEnvelope, "description": "Evidence not found or not accessible"}},
)
def get_evidence(
    chunk_id: str,
    request: Request,
    version: Annotated[int, Query(ge=1)],
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        chunk = chunks_get(settings, user.id, chunk_id, version)
        document = (
            documents_get(settings, user.id, chunk["document_id"]) if chunk is not None else None
        )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if chunk is None or document is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "证据不存在或不可用。"},
        )
    try:
        evidence = Evidence(
            chunk_id=chunk["id"],
            document_id=chunk["document_id"],
            version=chunk["version"],
            filename=document.filename,
            source_type="personal" if chunk.get("corpus") == "personal" else "public",
            locator=LocatorAdapter.validate_python(chunk["locator"]),
            quote=chunk["text"],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "INTERNAL_ERROR", "message": "证据记录格式异常，请通过请求 ID 排查。"},
        ) from exc
    return Envelope(data=evidence, request_id=request.state.request_id)
