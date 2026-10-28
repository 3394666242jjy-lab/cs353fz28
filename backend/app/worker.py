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
from pydantic import TypeAdapter

from app.config import get_settings
from app.schemas.analysis import DocumentVersion, Evidence, PdfLocator, TextLocator
from app.schemas.resources import ExecutionError, JobResult
from app.services.analyses import (
    get_analysis,
    save_analysis_result,
    set_analysis_failed,
)
from app.services.documents import (
    IndexedVersionPayload,
    get_document,
    list_chunks_by_ids,
    save_indexed_version,
    set_document_status,
)
from app.services.executions import transition_execution
from app.services.jobs import load_job
from app.services.storage import read_upload, save_extracted_text

LocatorAdapter: TypeAdapter[TextLocator | PdfLocator] = TypeAdapter(TextLocator | PdfLocator)

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
    """Import member C's vector store factory lazily; raises when missing."""
    try:
        from app.rag.vector_store import get_vector_store
    except ImportError as exc:
        raise RuntimeError("向量库适配器（app.rag.vector_store，成员 C）尚未集成") from exc
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
) -> bool:
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
    return transitioned


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

        async def _run_pipeline() -> Any:
            # C's adapter signals "call ensure_collection first"; creating the
            # collection is B's orchestration duty, not the pipeline's.
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

        indexed = asyncio.run(_run_pipeline())
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


def _load_agent() -> Any:
    """Import member D's agent lazily; raises when it is not integrated yet."""
    try:
        from app.agent.config import get_agent_settings
        from app.agent.pipeline import LangGraphAnalysisAgent
        from app.agent.provider import ChatCompletionModel
    except ImportError as exc:
        raise RuntimeError("分析 Agent 模块（app.agent，成员 D）尚未集成") from exc
    agent_settings = get_agent_settings()
    agent_settings.check_provider()
    return LangGraphAnalysisAgent(ChatCompletionModel(agent_settings))


class _MongoEvidenceRetriever:
    """Fills member C's candidate metadata with quote/locator/filename from MongoDB.

    C's retrieval returns only identities and scores (see issue #8); assembling
    the full Evidence contract from owner-scoped chunk rows is B's job, which
    is also what GET /evidence/{chunk_id} serves.
    """

    def __init__(self, settings_obj: Any, source: Any) -> None:
        self._settings = settings_obj
        self._source = source
        # D's pipeline stamps this onto the AnalysisResult; fill it from
        # member C's query result instead of inventing a version.
        self.retrieval_config_version: str | None = None

    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion],
        top_k: int = 5,
    ) -> list[Evidence]:
        result = await self._source.retrieve_evidence(
            user_id=user_id,
            query=query,
            document_versions=document_versions,
            top_k=top_k,
        )
        config_version = getattr(result, "retrieval_config_version", None)
        if isinstance(config_version, str) and config_version.strip():
            self.retrieval_config_version = config_version
        candidates = result.evidence
        rows = {
            row["id"]: row
            for row in list_chunks_by_ids(
                self._settings, user_id, [candidate.chunk_id for candidate in candidates]
            )
        }
        evidence: list[Evidence] = []
        for candidate in candidates:
            row = rows.get(candidate.chunk_id)
            if row is None:
                continue
            document = get_document(self._settings, user_id, candidate.document_id)
            if document is None:
                continue
            evidence.append(
                Evidence(
                    chunk_id=candidate.chunk_id,
                    document_id=candidate.document_id,
                    version=candidate.version,
                    filename=document.filename,
                    source_type="personal" if candidate.corpus == "personal" else "public",
                    locator=LocatorAdapter.validate_python(row["locator"]),
                    quote=row["text"],
                )
            )
        return evidence


def _build_retriever(settings_obj: Any) -> _MongoEvidenceRetriever:
    """Wire C's candidate source to B's MongoDB evidence assembly, lazily."""
    try:
        from app.rag.retrieval import QdrantEvidenceSource
    except ImportError as exc:
        raise RuntimeError("证据检索模块（app.rag.retrieval，成员 C）尚未集成") from exc
    return _MongoEvidenceRetriever(settings_obj, QdrantEvidenceSource(settings_obj))


@celery_app.task(name="careerpilot.run_analysis")
def run_analysis_task(analysis_id: str, user_id: str, execution_id: str) -> None:
    """Run one analysis and persist the outcome."""
    run_analysis(settings, analysis_id, user_id, execution_id)


def run_analysis(settings_obj: Any, analysis_id: str, user_id: str, execution_id: str) -> None:
    """Synchronous core of the analysis task, separated for direct testing."""
    if not transition_execution(
        settings_obj, user_id, execution_id, expected_state_version=1, status="running"
    ):
        return
    try:
        row = get_analysis(settings_obj, user_id, analysis_id)
        if row is None:
            _fail(settings_obj, user_id, execution_id, 2, "ANALYSIS_MISSING", "分析记录不存在。")
            return
        job = (
            JobResult.model_validate(row["job_snapshot"])
            if row.get("job_snapshot") is not None
            else load_job(settings_obj, user_id, row["job_id"])
        )
        if job is None:
            message = "岗位不存在，无法执行分析。"
            if _fail(settings_obj, user_id, execution_id, 2, "JOB_MISSING", message):
                set_analysis_failed(settings_obj, user_id, analysis_id, error=message)
            return
        if job.id != row["job_id"] or job.version != row["job_version"]:
            message = "岗位版本已变化，无法安全恢复分析，请重新提交。"
            if _fail(settings_obj, user_id, execution_id, 2, "JOB_VERSION_CHANGED", message):
                set_analysis_failed(settings_obj, user_id, analysis_id, error=message)
            return
        agent = _load_agent()
        retriever = _build_retriever(settings_obj)
        document_versions = [
            DocumentVersion.model_validate(item) for item in row["document_versions"]
        ]
        result = asyncio.run(
            agent.run_analysis(
                user_id=user_id,
                job=job,
                document_versions=document_versions,
                retriever=retriever,
            )
        )
        if not save_analysis_result(settings_obj, user_id, analysis_id, result):
            raise RuntimeError("分析结果保存失败：记录已被并发更新。")
    except Exception as exc:  # noqa: BLE001 - any failure must fail the execution
        message = f"分析执行失败：{exc}"[:500]
        if _fail(settings_obj, user_id, execution_id, 2, "ANALYSIS_FAILED", message):
            set_analysis_failed(settings_obj, user_id, analysis_id, error=message)
        return
    transition_execution(
        settings_obj,
        user_id,
        execution_id,
        expected_state_version=2,
        status="succeeded",
        result_type="analysis",
        result_id=analysis_id,
    )
