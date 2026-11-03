"""Celery workers. MongoDB executions are the authoritative task state (dev/03).

The indexing task orchestrates member C's RAG pipeline (issue #8): C owns
parsing/chunking/embedding/vector storage, B persists the result. The import
is lazy because C's module lands via a different branch; per dev/08 a missing
dependency must fail the execution with the reason, never hang or fake success.
"""

import asyncio
import logging
import re
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
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
from app.services.executions import heartbeat_execution, transition_execution
from app.services.jobs import load_job
from app.services.storage import read_upload, save_extracted_pages, save_extracted_text

LocatorAdapter: TypeAdapter[TextLocator | PdfLocator] = TypeAdapter(TextLocator | PdfLocator)


def _public_index_error(error: Exception) -> str:
    """Return only fixed, explicitly approved user-facing diagnostics."""
    try:
        from app.rag.chunking import DocumentParseError
        from app.rag.embeddings import EmbeddingError
        from app.rag.vector_store import VectorStoreError
    except ImportError:
        return "文档处理失败，请稍后重试或联系管理员。"
    if isinstance(error, DocumentParseError):
        message = str(error)
        allowed = {
            "暂不支持加密 PDF，请上传可直接读取的文件",
            "PDF 文件损坏或无法解析",
            "PDF 没有可提取的文本层；扫描件暂不支持",
            "文档不是有效的 UTF-8 文本",
        }
        if message in allowed:
            return message
        page = re.fullmatch(
            r"PDF 第 (\d+) 页(?:可能含无法提取的扫描内容；请检查并上传有完整文本层的 PDF|"
            r"含无法核验的图形内容（含图片、头像或矢量图）；请上传仅含可复制文字的 PDF，"
            r"或使用 UTF-8 TXT/Markdown)",
            message,
        )
        if page:
            return f"PDF 第 {page.group(1)} 页含无法核验的内容，请上传纯文字 PDF 或 TXT/Markdown。"
        return "文档无法解析，请检查文件格式或联系管理员。"
    if isinstance(error, EmbeddingError):
        return "向量化服务失败，请检查模型配置或稍后重试。"
    if isinstance(error, VectorStoreError):
        return "向量索引服务失败，请检查服务配置或稍后重试。"
    return "文档处理失败，请稍后重试或联系管理员。"


def _public_analysis_error(error: Exception) -> str:
    """Map known agent error codes to fixed messages; never echo exception text."""
    try:
        from app.agent.errors import AgentError
    except ImportError:
        return "分析执行失败，请稍后重试或联系管理员。"
    if isinstance(error, AgentError):
        messages = {
            "MODEL_NOT_CONFIGURED": "模型尚未正确配置，请检查模型地址和密钥。",
            "MODEL_AUTH_FAILED": "模型认证失败，请检查模型密钥。",
            "MODEL_REQUEST_REJECTED": "模型拒绝请求，请检查模型和输出格式配置。",
            "MODEL_RATE_LIMITED": "模型服务请求过于频繁，请稍后重试。",
            "MODEL_TIMEOUT": "模型调用超时，请稍后重试。",
            "MODEL_UNAVAILABLE": "模型服务暂不可用，请稍后重试。",
            "MODEL_OUTPUT_INVALID": "模型输出格式不正确，请重试。",
            "RETRIEVAL_TIMEOUT": "证据检索超时，请稍后重试。",
            "ANALYSIS_TIMEOUT": "分析超时，请稍后重试。",
            "BUDGET_EXCEEDED": "分析超过执行预算，请缩小岗位描述后重试。",
        }
        return messages.get(error.code, "分析未能完成，请检查配置或稍后重试。")
    return "分析执行失败，请稍后重试或联系管理员。"


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


def _save_extraction(
    settings_obj: Any, user_id: str, document_id: str, version: int, raw: bytes, file_type: str
) -> str:
    """Persist PDF extraction with original page numbers; text formats stay plain text."""
    from app.rag.chunking import parse_document

    parsed = parse_document(raw, file_type=file_type)
    if parsed.pages is not None:
        return save_extracted_pages(settings_obj, user_id, document_id, version, parsed.pages)
    return save_extracted_text(settings_obj, user_id, document_id, version, parsed.text)


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


@contextmanager
def _heartbeat(settings_obj: Any, user_id: str, execution_id: str):
    """Renew the worker lease while the actual Celery task is alive."""
    stopped = Event()

    def renew() -> None:
        interval = max(1, settings_obj.execution_lease_seconds // 3)
        while not stopped.wait(interval):
            try:
                heartbeat_execution(settings_obj, user_id, execution_id)
            except Exception:  # noqa: BLE001 - transient DB outage must not kill the worker
                logging.getLogger(__name__).exception("Could not renew execution lease")

    thread = Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


@celery_app.task(name="careerpilot.index_document")
def index_document_task(
    storage_key: str, user_id: str, document_id: str, version: int, execution_id: str
) -> None:
    """Index one uploaded document version and persist the outcome."""
    with _heartbeat(settings, user_id, execution_id):
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
        extracted_text_key = _save_extraction(
            settings_obj, user_id, document_id, version, raw, Path(storage_key).suffix.lower()
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
            _public_index_error(exc),
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
    with _heartbeat(settings, user_id, execution_id):
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
        message = _public_analysis_error(exc)
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
