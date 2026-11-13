import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
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
    DocumentDeleted,
    DocumentExtraction,
    DocumentPage,
    DocumentResult,
    DocumentTextUpdate,
    DocumentVersionAccepted,
    ExecutionAccepted,
    ExecutionError,
    ExecutionResult,
    ExpectedVersion,
    JobCreate,
    JobCreated,
    JobResult,
    JobUpdate,
    LoginRequest,
    PlanConfirmed,
    PlanCreate,
    PlanDraftTask,
    PlanResult,
    PlanTask,
    PlanTaskStatusUpdate,
    ResumeRequest,
    TokenResult,
    UserResult,
)
from app.services.analyses import STATUS_FAILED, STATUS_READY, set_analysis_failed
from app.services.analyses import create_analysis as analyses_create
from app.services.analyses import get_analysis as analyses_get
from app.services.auth import login_user
from app.services.database import transactional_connection
from app.services.dispatch import enqueue_dispatch, ensure_dispatch_indexes, publish_dispatch
from app.services.documents import (
    create_document,
    create_text_version,
    find_active_index_execution,
    get_document_source_key,
    get_version_row,
    load_current_extraction,
    set_document_status,
    set_version_failed,
    soft_delete_document,
)
from app.services.documents import get_chunk as chunks_get
from app.services.documents import get_document as documents_get
from app.services.documents import list_documents as documents_list
from app.services.events import get_event_sequence, list_events
from app.services.executions import (
    ExecutionCreate,
    create_execution,
    ensure_execution_indexes,
    insert_execution,
    recover_document_execution,
    transition_execution,
)
from app.services.executions import get_execution as executions_get
from app.services.inputs import InputConflictError
from app.services.jobs import load_job, save_job, update_job
from app.services.plans import (
    PlanInfeasibleError,
    PlanStaleError,
    PlanStateError,
    attach_plan_execution,
    confirm_plan,
    create_plan_record,
    ensure_plan_indexes,
    get_plan,
    list_plan_tasks,
    update_plan_task_status,
    validate_timezone,
)
from app.services.resume import ResumeError
from app.services.resume import resume_execution as resume_execution_service
from app.services.storage import remove_upload, save_upload
from app.services.tasks import dispatch_analysis_task, dispatch_index_task, dispatch_purge_task

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
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
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
    suffix = Path(filename).suffix.lower()
    valid_format = (
        raw.startswith(b"%PDF-")
        if suffix == ".pdf"
        else not (raw.startswith((b"%PDF-", b"PK\x03\x04")) or b"\x00" in raw)
    )
    if suffix != ".pdf" and valid_format:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            valid_format = False
    if not valid_format:
        raise HTTPException(
            status_code=415,
            detail={"code": "UNSUPPORTED_FILE_TYPE", "message": "文件内容与所选格式不符。"},
        )
    storage_key = save_upload(settings, user.id, filename, raw)
    document_id = None
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
            settings,
            user.id,
            ExecutionCreate(type="index", resource_id=document_id, resource_version=1),
        )
    except PyMongoError as exc:
        if document_id is None:
            remove_upload(settings, storage_key)
        else:
            try:
                if set_document_status(
                    settings,
                    user.id,
                    document_id,
                    status="failed",
                    error="创建索引任务失败，请重试。",
                ):
                    remove_upload(settings, storage_key)
            except PyMongoError:
                pass
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
        if document is not None:
            recover_document_execution(settings, user.id, document_id)
            document = documents_get(settings, user.id, document_id)
            if document is not None and document.status == "ready":
                extraction = load_current_extraction(settings, user.id, document_id)
                if extraction is not None:
                    document = document.model_copy(
                        update={"extraction": DocumentExtraction.model_validate(extraction)}
                    )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if document is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "资料不存在或不属于当前用户。"},
        )
    return Envelope(data=document, request_id=request.state.request_id)


@router.delete(
    "/documents/{document_id}",
    response_model=Envelope[DocumentDeleted],
    status_code=202,
    tags=["documents"],
    responses=AUTH_ERRORS | {404: {"model": ErrorEnvelope, "description": "Document not found"}},
)
def delete_document(
    document_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Soft-delete a document: it becomes immediately unreadable and unsearchable.

    The write blocks every read and retrieval path first; chunks, vectors and
    files are then purged asynchronously with retry (dev/03 lifecycle #3).
    Deleting an already-deleted document is a 404, per the shared API rules.
    """
    try:
        row = soft_delete_document(settings, user.id, document_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "资料不存在或不属于当前用户。"},
        )
    try:
        dispatch_purge_task(settings, user_id=user.id, document_id=document_id)
    except Exception:  # noqa: BLE001 - the purge sweeper re-dispatches
        logging.getLogger(__name__).exception(
            "Could not dispatch purge for %s; leaving it to the sweeper", document_id
        )
    return Envelope(
        data=DocumentDeleted(document_id=document_id, purge_status="pending"),
        request_id=request.state.request_id,
    )


DOCUMENT_WRITE_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "Invalid or missing bearer token"},
    404: {"model": ErrorEnvelope, "description": "Document not found"},
    409: {"model": ErrorEnvelope, "description": "Version conflict or document not ready"},
    503: {"model": ErrorEnvelope, "description": "Database unavailable or task dispatch failed"},
}


@router.put(
    "/documents/{document_id}/text",
    response_model=Envelope[DocumentVersionAccepted],
    status_code=202,
    tags=["documents"],
    responses=DOCUMENT_WRITE_ERRORS,
)
def update_document_text(
    document_id: str,
    payload: DocumentTextUpdate,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Edit the extracted text and build the next immutable version.

    The published version keeps serving until the new one is fully indexed
    (dev/03 lifecycle #1); analyses pinned to the old version show stale once
    current_version advances. expected_version is optimistic concurrency
    control: a stale editor gets 409 VERSION_CONFLICT.
    """
    try:
        document = documents_get(settings, user.id, document_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
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
                "message": "资料尚未索引完成，不能编辑文本。",
            },
        )
    if payload.expected_version != document.current_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "资料版本已变化，请刷新后基于最新版本编辑。",
            },
        )
    next_version = document.current_version + 1
    source_key = save_upload(
        settings,
        user.id,
        f"{document_id}-v{next_version}.txt",
        payload.extracted_text.encode("utf-8"),
    )
    try:
        new_version = create_text_version(
            settings,
            user.id,
            document_id,
            expected_version=payload.expected_version,
            source_key=source_key,
        )
        if new_version is None:
            remove_upload(settings, source_key)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VERSION_CONFLICT",
                    "message": "资料版本已变化，请刷新后基于最新版本编辑。",
                },
            )
        execution_id = create_execution(
            settings,
            user.id,
            ExecutionCreate(type="index", resource_id=document_id, resource_version=new_version),
        )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    try:
        dispatch_index_task(
            settings,
            storage_key=source_key,
            user_id=user.id,
            document_id=document_id,
            version=new_version,
            execution_id=execution_id,
        )
    except Exception as exc:
        # dev/08: a dispatch failure must fail the execution explicitly.
        transition_execution(
            settings,
            user.id,
            execution_id,
            expected_state_version=1,
            status="failed",
            error=ExecutionError(code="TASK_DISPATCH_FAILED", message="索引任务投递失败，请重试。"),
        )
        set_version_failed(
            settings, user.id, document_id, new_version, error="索引任务投递失败，请重试。"
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "TASK_DISPATCH_FAILED", "message": "索引任务投递失败，请重试。"},
        ) from exc
    return Envelope(
        data=DocumentVersionAccepted(
            document_id=document_id, version=new_version, execution_id=execution_id
        ),
        request_id=request.state.request_id,
    )


@router.post(
    "/documents/{document_id}/reindex",
    response_model=Envelope[DocumentVersionAccepted],
    status_code=202,
    tags=["documents"],
    responses=DOCUMENT_WRITE_ERRORS,
)
def reindex_document(
    document_id: str,
    payload: ExpectedVersion,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Retry indexing of the current version after a failure.

    An already-running build is reused (same execution returned); a published
    version is immutable and never rebuilt in place.
    """
    try:
        document = documents_get(settings, user.id, document_id)
        if document is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "资料不存在或不属于当前用户。"},
            )
        if payload.expected_version != document.current_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VERSION_CONFLICT",
                    "message": "资料版本已变化，请刷新后重试。",
                },
            )
        active = find_active_index_execution(
            settings, user.id, document_id, document.current_version
        )
        if active is not None:
            return Envelope(
                data=DocumentVersionAccepted(
                    document_id=document_id,
                    version=document.current_version,
                    execution_id=active,
                ),
                request_id=request.state.request_id,
            )
        version_row = get_version_row(settings, user.id, document_id, document.current_version)
        if version_row is None or version_row.get("status") == "ready":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REINDEX_NOT_REQUIRED",
                    "message": "当前版本已就绪，无需重建；如需更新请编辑文本生成新版本。",
                },
            )
        source_key = version_row.get("source_key") or get_document_source_key(
            settings, user.id, document_id
        )
        if not source_key:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REINDEX_SOURCE_MISSING",
                    "message": "原始文件已不可用，请重新上传。",
                },
            )
        execution_id = create_execution(
            settings,
            user.id,
            ExecutionCreate(
                type="index", resource_id=document_id, resource_version=document.current_version
            ),
        )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    try:
        dispatch_index_task(
            settings,
            storage_key=source_key,
            user_id=user.id,
            document_id=document_id,
            version=document.current_version,
            execution_id=execution_id,
        )
    except Exception as exc:
        transition_execution(
            settings,
            user.id,
            execution_id,
            expected_state_version=1,
            status="failed",
            error=ExecutionError(code="TASK_DISPATCH_FAILED", message="索引任务投递失败，请重试。"),
        )
        set_version_failed(
            settings,
            user.id,
            document_id,
            document.current_version,
            error="索引任务投递失败，请重试。",
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "TASK_DISPATCH_FAILED", "message": "索引任务投递失败，请重试。"},
        ) from exc
    return Envelope(
        data=DocumentVersionAccepted(
            document_id=document_id,
            version=document.current_version,
            execution_id=execution_id,
        ),
        request_id=request.state.request_id,
    )


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


@router.patch(
    "/jobs/{job_id}",
    response_model=Envelope[JobResult],
    tags=["jobs"],
    responses={**AUTH_ERRORS, 409: {"model": ErrorEnvelope, "description": "JD version conflict"}},
)
def patch_job(
    job_id: str,
    payload: JobUpdate,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    try:
        result = update_job(settings, user.id, job_id, payload)
        if result is None:
            if load_job(settings, user.id, job_id) is None:
                raise HTTPException(
                    404, detail={"code": "RESOURCE_NOT_FOUND", "message": "资源不存在。"}
                )
            raise HTTPException(
                409,
                detail={"code": "VERSION_CONFLICT", "message": "岗位版本已变化，请刷新后重试。"},
            )
    except PyMongoError as exc:
        raise database_unavailable() from exc
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
    analysis_id = None
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
            job_snapshot=job,
            document_versions=document_versions,
        )
        execution_id = create_execution(
            settings, user.id, ExecutionCreate(type="analysis", resource_id=analysis_id)
        )
    except PyMongoError as exc:
        if analysis_id is not None:
            try:
                set_analysis_failed(
                    settings, user.id, analysis_id, error="创建分析任务失败，请重试。"
                )
            except PyMongoError:
                pass
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


def analysis_is_stale(settings: Settings, user_id: str, row: dict) -> bool:
    """Compare the pinned versions against current job and document state.

    Staleness is derived at read time (dev/03 lifecycle #2/#3): a JD edit,
    a new document version or a soft delete flips every analysis pinned to
    the old source, without a fan-out write.
    """
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
            stale = analysis_is_stale(settings, user.id, row)
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
    "/executions/{execution_id}/events",
    tags=["execution"],
    responses=AUTH_ERRORS
    | {
        400: {"model": ErrorEnvelope, "description": "Conflicting replay cursors"},
        404: {"model": ErrorEnvelope, "description": "Execution not found"},
        410: {"model": ErrorEnvelope, "description": "Event history expired"},
    },
)
def stream_execution_events(
    execution_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
    after_event_id: Annotated[str | None, Query()] = None,
):
    """Authenticated SSE stream with sequence-based replay (dev/04 M2).

    Replay starts after the Last-Event-ID header or the after_event_id query
    parameter (both set and different is a 400). A cursor that no longer
    exists in the retained history is a 410: the client then reads the
    execution snapshot (latest_event_id) before reconnecting. Live events are
    polled from the durable execution_events collection by sequence, so the
    replay-to-subscribe window cannot lose events; heartbeats are SSE
    comments and the stream closes after a terminal event.
    """
    try:
        execution = executions_get(settings, user.id, execution_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if execution is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "任务不存在或不属于当前用户。"},
        )
    header_cursor = request.headers.get("last-event-id")
    if after_event_id and header_cursor and after_event_id != header_cursor:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SSE_CURSOR_CONFLICT",
                "message": "Last-Event-ID 与 after_event_id 不一致。",
            },
        )
    cursor = after_event_id or header_cursor
    after_sequence = 0
    if cursor:
        try:
            anchor = get_event_sequence(settings, user.id, execution_id, cursor)
        except PyMongoError as exc:
            raise database_unavailable() from exc
        if anchor is None:
            raise HTTPException(
                status_code=410,
                detail={
                    "code": "EVENT_HISTORY_EXPIRED",
                    "message": "事件游标已超出保留窗口，请读取任务快照后重新订阅。",
                },
            )
        after_sequence = anchor

    def _format(row: dict) -> str:
        data = dict(row.get("payload") or {})
        data.setdefault("execution_id", execution_id)
        data.setdefault("status", row.get("status"))
        return (
            f"id: {row['id']}\n"
            f"event: {row['event_type']}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        )

    async def event_generator():
        last_sequence = after_sequence
        terminal_types = {"completed", "failed"}
        terminal_statuses = {"succeeded", "failed"}
        heartbeat_at = monotonic() + settings.sse_heartbeat_seconds
        while True:
            if await request.is_disconnected():
                return
            try:
                rows = list_events(
                    settings,
                    user.id,
                    execution_id,
                    after_sequence=last_sequence,
                    limit=settings.event_history_retention_count,
                )
            except PyMongoError:
                # Transient outage: keep the stream alive and retry next round;
                # replay by sequence makes this gapless once Mongo returns.
                rows = []
            for row in rows:
                last_sequence = row["sequence"]
                yield _format(row)
                if row["event_type"] in terminal_types:
                    return
            if not rows:
                try:
                    current = executions_get(settings, user.id, execution_id)
                except PyMongoError:
                    current = None
                if current is not None and current.status in terminal_statuses:
                    # Terminal snapshot fallback: the execution ended while its
                    # events were already pruned (or predate M2), so emit one
                    # synthetic terminal event from the authoritative snapshot.
                    payload: dict = {
                        "execution_id": execution_id,
                        "status": current.status,
                        "result_type": current.result_type,
                        "result_id": current.result_id,
                        "error": current.error.model_dump() if current.error else None,
                        "snapshot": True,
                    }
                    event_name = "completed" if current.status == "succeeded" else "failed"
                    event_data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {event_name}\ndata: {event_data}\n\n"
                    return
                now = monotonic()
                if now >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = now + settings.sse_heartbeat_seconds
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/executions/{execution_id}/resume",
    response_model=Envelope[ExecutionAccepted],
    status_code=202,
    tags=["execution"],
    responses=AUTH_ERRORS
    | {
        404: {"model": ErrorEnvelope, "description": "Execution or document not found"},
        409: {"model": ErrorEnvelope, "description": "State, checkpoint or input conflict"},
        422: {"model": ErrorEnvelope, "description": "Invalid resume answer"},
    },
)
def resume_execution(
    execution_id: str,
    payload: ResumeRequest,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Resume a clarification pause with the user's answer (dev/04 M2).

    New documents must already be uploaded and indexed; their current versions
    are pinned into the recorded input. A replayed identical request returns
    the same acceptance; the same interrupt with a different answer is a 409
    INPUT_CONFLICT, and a stale interrupt/checkpoint pair is a 409
    STALE_CHECKPOINT. Plan confirmations use POST /plans/{id}/confirm instead.
    """
    document_versions: list[dict] = []
    try:
        for document_id in payload.answer.document_ids:
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
                {"document_id": document_id, "version": document.current_version}
            )
        resume_execution_service(
            settings,
            user.id,
            execution_id,
            interrupt_id=payload.interrupt_id,
            checkpoint_id=payload.checkpoint_id,
            answer=payload.answer,
            document_versions=document_versions,
        )
    except InputConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INPUT_CONFLICT",
                "message": "同一交互已记录过不同的回答，请刷新任务状态。",
            },
        ) from exc
    except ResumeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except PyMongoError as exc:
        raise database_unavailable() from exc
    return Envelope(
        data=ExecutionAccepted(execution_id=execution_id),
        request_id=request.state.request_id,
    )


PLAN_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "Invalid or missing bearer token"},
    404: {"model": ErrorEnvelope, "description": "Analysis or plan not found"},
    409: {"model": ErrorEnvelope, "description": "Analysis not ready, stale or state conflict"},
    422: {"model": ErrorEnvelope, "description": "Invalid constraints or infeasible plan"},
    503: {"model": ErrorEnvelope, "description": "Database unavailable"},
}


def _plan_result(plan: dict, tasks: list[dict] | None) -> PlanResult:
    """Assemble the public plan contract; a malformed draft is a 500, not data."""
    return PlanResult(
        id=plan["id"],
        analysis_id=plan["analysis_id"],
        execution_id=plan["execution_id"],
        status=plan["status"],
        deadline=plan["deadline"],
        timezone=plan["timezone"],
        daily_minutes=plan["daily_minutes"],
        draft_tasks=[PlanDraftTask.model_validate(task) for task in plan.get("draft_tasks", [])],
        tasks=[PlanTask.model_validate(task) for task in tasks] if tasks is not None else None,
        confirmed_at=plan.get("confirmed_at"),
        created_at=plan["created_at"],
    )


@router.post(
    "/plans",
    response_model=Envelope[ExecutionAccepted],
    status_code=202,
    tags=["plans"],
    responses=PLAN_ERRORS,
)
def create_plan(
    payload: PlanCreate,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Create a draft plan execution from a ready, fresh analysis.

    The plan row, its execution and the start dispatch commit in one
    transaction (dev/03 lifecycle #4/#5); the worker (member D's planner)
    fills the draft and parks the execution at waiting_for_input, where
    GET /executions/{id} exposes the draft plan via pending_input/result_id.
    """
    try:
        timezone = validate_timezone(payload.timezone)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_INPUT", "message": str(exc)},
        ) from exc
    today = datetime.now(ZoneInfo(payload.timezone)).date()
    if payload.deadline < today:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PLAN_INFEASIBLE",
                "message": "截止日期早于今天，无法排期。",
            },
        )
    _ = timezone
    try:
        analysis = analyses_get(settings, user.id, payload.analysis_id)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if analysis is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "分析不存在或不属于当前用户。"},
        )
    if analysis["status"] == STATUS_FAILED:
        message = analysis.get("error") or "分析执行失败。"
        raise HTTPException(
            status_code=409,
            detail={"code": "ANALYSIS_FAILED", "message": message},
        )
    if analysis["status"] != STATUS_READY:
        raise HTTPException(
            status_code=409,
            detail={"code": "ANALYSIS_NOT_READY", "message": "分析仍在进行中，请稍后重试。"},
        )
    if analysis_is_stale(settings, user.id, analysis):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ANALYSIS_STALE",
                "message": "分析来源已更新或删除，不能用于生成计划，请重新分析。",
            },
        )
    try:
        # Index creation must commit before the transaction starts.
        ensure_plan_indexes(settings)
        ensure_execution_indexes(settings)
        ensure_dispatch_indexes(settings)
        with transactional_connection(settings) as (database, session):
            plan_id = create_plan_record(
                database,
                user_id=user.id,
                analysis_id=payload.analysis_id,
                deadline=payload.deadline,
                timezone=payload.timezone,
                daily_minutes=payload.daily_minutes,
                session=session,
            )
            execution_id = insert_execution(
                database,
                user.id,
                ExecutionCreate(type="plan", resource_id=plan_id),
                session=session,
            )
            attach_plan_execution(database, user.id, plan_id, execution_id, session=session)
            dispatch_id = enqueue_dispatch(
                database,
                user_id=user.id,
                execution_id=execution_id,
                command="start_plan",
                expected_state_version=1,
                session=session,
            )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    # Best effort: the durable outbox row is republished by the sweeper.
    publish_dispatch(settings, dispatch_id)
    return Envelope(
        data=ExecutionAccepted(execution_id=execution_id),
        request_id=request.state.request_id,
    )


@router.get(
    "/plans/{plan_id}",
    response_model=Envelope[PlanResult],
    tags=["plans"],
    responses=PLAN_ERRORS,
)
def read_plan(
    plan_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Return the draft or confirmed plan with its tasks, owner-scoped."""
    try:
        plan = get_plan(settings, user.id, plan_id)
        tasks = (
            list_plan_tasks(settings, user.id, plan_id)
            if plan is not None and plan["status"] == "confirmed"
            else None
        )
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "计划不存在或不属于当前用户。"},
        )
    try:
        result = _plan_result(plan, tasks)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "INTERNAL_ERROR", "message": "计划记录格式异常，请通过请求 ID 排查。"},
        ) from exc
    return Envelope(data=result, request_id=request.state.request_id)


@router.post(
    "/plans/{plan_id}/confirm",
    response_model=Envelope[PlanConfirmed],
    tags=["plans"],
    responses=PLAN_ERRORS,
)
def confirm_plan_route(
    plan_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Confirm the draft: persist formal tasks and enqueue the graph resume.

    Permission, source freshness and the schedule are re-validated at confirm
    time; tasks, the confirmation input and the resume dispatch commit in one
    transaction (dev/03 lifecycle #4). Replays return the same confirmed plan
    without duplicating tasks or resume messages.
    """

    def _fresh(analysis_id: str) -> bool:
        row = analyses_get(settings, user.id, analysis_id)
        return (
            row is not None
            and row.get("status") == STATUS_READY
            and not analysis_is_stale(settings, user.id, row)
        )

    try:
        outcome = confirm_plan(settings, user.id, plan_id, analysis_is_fresh=_fresh)
    except PlanStaleError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ANALYSIS_STALE",
                "message": "分析来源已更新或删除，不能确认旧计划，请重新生成。",
            },
        ) from exc
    except PlanInfeasibleError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PLAN_INFEASIBLE",
                "message": "计划不满足排期约束，请调整目标日期或每日可用时间。",
            },
        ) from exc
    except PlanStateError as exc:
        status_code = 404 if exc.code == "PLAN_NOT_FOUND" else 409
        code = "NOT_FOUND" if exc.code == "PLAN_NOT_FOUND" else exc.code
        raise HTTPException(
            status_code=status_code, detail={"code": code, "message": exc.message}
        ) from exc
    except InputConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INPUT_CONFLICT", "message": "同一交互已记录过不同的回答。"},
        ) from exc
    except PyMongoError as exc:
        raise database_unavailable() from exc
    try:
        result = _plan_result(outcome["plan"], outcome["tasks"])
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "INTERNAL_ERROR", "message": "计划记录格式异常，请通过请求 ID 排查。"},
        ) from exc
    return Envelope(
        data=PlanConfirmed(
            plan=result, execution_id=outcome["execution_id"], resumed=outcome["resumed"]
        ),
        request_id=request.state.request_id,
    )


@router.patch(
    "/plans/{plan_id}/tasks/{task_id}",
    response_model=Envelope[PlanTask],
    tags=["plans"],
    responses=PLAN_ERRORS,
)
def update_task_status(
    plan_id: str,
    task_id: str,
    payload: PlanTaskStatusUpdate,
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    user: Annotated[UserResult, Depends(current_user)],
):
    """Update one confirmed task's status (todo/in_progress/done)."""
    try:
        row = update_plan_task_status(settings, user.id, plan_id, task_id, status=payload.status)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "任务不存在或不属于当前用户。"},
        )
    return Envelope(data=PlanTask.model_validate(row), request_id=request.state.request_id)


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
