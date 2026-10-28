"""Celery workers. MongoDB executions are the authoritative task state (dev/03).

The indexing task orchestrates member C's RAG pipeline (issue #8): C owns
parsing/chunking/embedding/vector storage, B persists the result. The import
is lazy because C's module lands via a different branch; per dev/08 a missing
dependency must fail the execution with the reason, never hang or fake success.
"""

import asyncio
from hashlib import sha256
from pathlib import Path
from typing import Any

from celery import Celery

from app.config import get_settings
from app.schemas.resources import ExecutionError
from app.services.documents import (
    IndexedVersionPayload,
    save_indexed_version,
    set_document_status,
)
from app.services.executions import transition_execution
from app.services.storage import read_upload, save_extracted_text

settings = get_settings()
celery_app = Celery("careerpilot", broker=settings.celery_broker_url.get_secret_value())
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
)


@celery_app.task(name="careerpilot.infrastructure_ping")
def infrastructure_ping() -> dict[str, str]:
    """Diagnostic only."""
    return {"worker": "ok", "mode": "scaffold"}


def _load_indexer() -> Any:
    """Import member C's pipeline lazily; raises when it is not integrated yet."""
    try:
        from app.rag.indexer import index_document
    except ImportError as exc:
        raise RuntimeError("RAG 索引模块（app.rag.indexer，成员 C）尚未集成") from exc
    return index_document


def _load_vector_store() -> Any:
    try:
        from app.rag.vector_store import get_vector_store
    except ImportError as exc:
        raise RuntimeError("向量库适配器尚未集成") from exc
    return get_vector_store


def _extract_text(raw: bytes, file_type: str) -> str:
    """Extract plain text via member C's parser when available.

    TXT/Markdown uploads are already plain text, so the raw bytes are a valid
    fallback while C's module is not merged.
    """
    try:
        from app.rag.chunking import parse_document
    except ImportError:
        if file_type == ".pdf":
            raise RuntimeError("PDF 解析模块尚未集成") from None
        return raw.decode("utf-8")
    return parse_document(raw, file_type=file_type).text


def _fail(
    settings_obj: Any,
    user_id: str,
    execution_id: str,
    state_version: int,
    code: str,
    message: str,
    document_id: str | None = None,
) -> None:
    """Move the execution to failed from the given state version (best effort)."""
    transitioned = transition_execution(
        settings_obj,
        user_id,
        execution_id,
        expected_state_version=state_version,
        status="failed",
        error=ExecutionError(code=code, message=message),
    )
    if transitioned and document_id is not None:
        set_document_status(settings_obj, user_id, document_id, status="failed", error=message)


@celery_app.task(name="careerpilot.index_document")
def index_document_task(
    storage_key: str, user_id: str, document_id: str, version: int, execution_id: str
) -> None:
    """Index one uploaded document version and persist the outcome."""
    run_indexing(settings, storage_key, user_id, document_id, version, execution_id)


def run_indexing(
    settings_obj: Any,
    storage_key: str,
    user_id: str,
    document_id: str,
    version: int,
    execution_id: str,
) -> None:
    """Synchronous core of the indexing task, separated for direct testing."""
    try:
        raw = read_upload(settings_obj, storage_key)
    except OSError:
        _fail(
            settings_obj,
            user_id,
            execution_id,
            1,
            "FILE_READ_FAILED",
            "无法读取上传文件。",
            document_id,
        )
        return
    if raw is None:
        _fail(
            settings_obj,
            user_id,
            execution_id,
            1,
            "FILE_MISSING",
            "上传文件丢失，请重新上传。",
            document_id,
        )
        return
    if not transition_execution(
        settings_obj, user_id, execution_id, expected_state_version=1, status="running"
    ):
        return
    try:
        index_document = _load_indexer()
        store = _load_vector_store()(settings_obj)

        async def run_pipeline():
            await store.ensure_collection()
            return await index_document(
                raw,
                user_id=user_id,
                document_id=document_id,
                version=version,
                file_type=Path(storage_key).suffix.lower(),
                store=store,
                settings=settings_obj,
            )

        indexed = asyncio.run(run_pipeline())
        extracted_text_key = save_extracted_text(
            settings_obj,
            user_id,
            document_id,
            version,
            _extract_text(raw, Path(storage_key).suffix.lower()),
        )
        save_indexed_version(
            settings_obj,
            user_id,
            document_id,
            IndexedVersionPayload(
                version=version,
                extracted_text_key=extracted_text_key,
                content_hash=f"sha256:{sha256(raw).hexdigest()}",
                parser_version=indexed.parser_version,
                chunking_config_version=indexed.chunking_config_version,
                embedding_model=indexed.embedding_model,
                embedding_dimension=indexed.embedding_dimension,
                index_version=indexed.index_version,
            ),
            indexed.chunks,
        )
    except Exception as exc:  # noqa: BLE001 - any pipeline failure must fail the execution
        _fail(
            settings_obj,
            user_id,
            execution_id,
            2,
            "INDEXING_FAILED",
            f"文档解析或索引失败：{exc}"[:500],
            document_id,
        )
        return
    transition_execution(
        settings_obj,
        user_id,
        execution_id,
        expected_state_version=2,
        status="succeeded",
        result_type="document",
        result_id=document_id,
    )
