"""Real MongoDB checks; each run uses and removes only its own temporary database."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.schemas.analysis import AnalysisResult, DocumentVersion, TextLocator
from app.schemas.resources import JobResult
from app.services.analyses import create_analysis, get_analysis, save_analysis_result
from app.services.documents import create_document, save_indexed_version
from app.services.executions import (
    ExecutionCreate,
    create_execution,
    transition_execution,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MONGO_STORAGE_TESTS") != "1",
    reason="real MongoDB test is opt-in",
)


@pytest.fixture
def mongo_settings():
    database_name = f"careerpilot_storage_test_{uuid4().hex}"
    settings = Settings(_env_file=None, app_env="test", mongodb_database=database_name)
    client = MongoClient(settings.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
        yield settings
    finally:
        client.drop_database(database_name)
        client.close()


def chunk(chunk_id):
    return SimpleNamespace(
        chunk_id=chunk_id,
        text="Original text",
        vector_id=f"vec-{chunk_id}",
        locator=TextLocator(kind="text", line_start=1, line_end=1),
    )


def payload(version):
    return {
        "version": version,
        "extracted_text_key": "private/text",
        "content_hash": "sha256:test",
        "parser_version": "p1",
        "chunking_config_version": "c1",
        "embedding_model": "e1",
        "embedding_dimension": 2,
        "index_version": "i1",
    }


def test_mongo_version_publication_and_execution_state(mongo_settings):
    settings = mongo_settings
    user = "synthetic-user"
    document_id = create_document(
        settings,
        user,
        kind="resume",
        filename="resume.txt",
        media_type="text/plain",
        size_bytes=13,
        storage_key="private/input",
    )
    save_indexed_version(settings, user, document_id, payload(1), [chunk("chunk-v1")])
    with pytest.raises(DuplicateKeyError):
        save_indexed_version(settings, user, document_id, payload(1), [chunk("replacement")])
    save_indexed_version(settings, user, document_id, payload(2), [chunk("chunk-v2")])
    client = MongoClient(settings.mongodb_uri.get_secret_value())
    try:
        db = client[settings.mongodb_database]
        versions = list(db.document_versions.find({"document_id": document_id}).sort("version"))
        assert len(versions) == 2 and len({row["id"] for row in versions}) == 2
        assert [row["status"] for row in versions] == ["ready", "ready"]
        assert {row["id"] for row in db.chunks.find({"document_id": document_id})} == {
            "chunk-v1",
            "chunk-v2",
        }
        assert db.documents.find_one({"id": document_id})["current_version"] == 2
    finally:
        client.close()
    execution = create_execution(
        settings, user, ExecutionCreate(type="index", resource_id=document_id)
    )
    assert transition_execution(
        settings, user, execution, expected_state_version=1, status="running"
    )
    assert transition_execution(
        settings,
        user,
        execution,
        expected_state_version=2,
        status="succeeded",
        result_type="document",
        result_id=document_id,
    )
    assert not transition_execution(
        settings, user, execution, expected_state_version=3, status="running"
    )


def test_mongo_preserves_analysis_snapshot(mongo_settings):
    settings = mongo_settings
    job = JobResult(id="job_1", title="Developer", raw_jd="Original JD", version=1)
    versions = [DocumentVersion(document_id="doc_1", version=1)]
    analysis_id = create_analysis(
        settings,
        "user",
        job_id=job.id,
        job_version=job.version,
        job_snapshot=job,
        document_versions=versions,
    )
    result = AnalysisResult(
        id="generated",
        job_id=job.id,
        job_version=1,
        document_versions=versions,
        stale=False,
        matches=[],
        warnings=[],
    )
    assert save_analysis_result(settings, "user", analysis_id, result)
    row = get_analysis(settings, "user", analysis_id)
    assert row["job_snapshot"]["raw_jd"] == "Original JD"
    assert row["job_version"] == 1 and row["status"] == "ready"


def test_pdf_index_and_analysis_with_real_mongo_and_qdrant(mongo_settings, tmp_path, monkeypatch):
    import json

    import httpx
    from test_pdf_indexer import pdf_bytes

    from app import worker
    from app.agent.config import AgentSettings
    from app.agent.models import ModelCompletion
    from app.agent.pipeline import LangGraphAnalysisAgent
    from app.rag import indexer, retrieval
    from app.services.documents import get_chunk, get_document
    from app.services.executions import get_execution
    from app.services.storage import save_upload

    collection = f"careerpilot_integration_{uuid4().hex}"
    settings = mongo_settings.model_copy(
        update={
            "vector_store_collection": collection,
            "embedding_dimension": 64,
            "local_storage_dir": tmp_path,
        }
    )

    class Embedder:
        model_version = "test-embedding"
        dimension = 64

        async def embed_texts(self, texts):
            return [
                [1.0 if "Python" in text else 0.0, 0.0 if "Python" in text else 1.0] + [0.0] * 62
                for text in texts
            ]

    class Model:
        is_test_double = True

        async def complete(self, *, system, payload, output_schema):
            if output_schema.__name__ == "RequirementBatch":
                data = {"requirements": [{"text": "Python development", "category": "required"}]}
            else:
                requirement = payload["requirements"][0]
                data = {
                    "assessments": [
                        {
                            "requirement_id": requirement["id"],
                            "status": "supported",
                            "evidence_ids": [requirement["evidence"][0]["chunk_id"]],
                            "explanation": "The PDF supports the requirement.",
                            "missing_information": [],
                        }
                    ]
                }
            return ModelCompletion(content=json.dumps(data), model_version="test-model")

    monkeypatch.setattr(indexer, "get_embedding_provider", lambda settings: Embedder())
    monkeypatch.setattr(retrieval, "get_embedding_provider", lambda settings: Embedder())
    monkeypatch.setattr(
        worker,
        "_load_agent",
        lambda: LangGraphAnalysisAgent(Model(), AgentSettings(_env_file=None)),
    )
    user = "integration-user"
    try:
        raw = pdf_bytes("Python development", "Other projects")
        storage_key = save_upload(settings, user, "resume.pdf", raw)
        document_id = create_document(
            settings,
            user,
            kind="resume",
            filename="resume.pdf",
            media_type="application/pdf",
            size_bytes=len(raw),
            storage_key=storage_key,
        )
        execution_id = create_execution(
            settings, user, ExecutionCreate(type="index", resource_id=document_id)
        )
        worker.run_indexing(settings, storage_key, user, document_id, 1, execution_id)
        index_execution = get_execution(settings, user, execution_id)
        assert index_execution.status == "succeeded", index_execution.error
        assert get_document(settings, user, document_id).status == "ready"
        job = JobResult(
            id="job_integration", title="Developer", raw_jd="Python development", version=1
        )
        versions = [DocumentVersion(document_id=document_id, version=1)]
        analysis_id = create_analysis(
            settings,
            user,
            job_id=job.id,
            job_version=1,
            job_snapshot=job,
            document_versions=versions,
        )
        analysis_execution_id = create_execution(
            settings, user, ExecutionCreate(type="analysis", resource_id=analysis_id)
        )
        worker.run_analysis(settings, analysis_id, user, analysis_execution_id)
        execution = get_execution(settings, user, analysis_execution_id)
        assert execution.status == "succeeded", execution.error
        result = get_analysis(settings, user, analysis_id)
        assert result["status"] == "ready" and result["matches"][0]["status"] == "supported"
        cited = result["matches"][0]["evidence_refs"][0]
        evidence = get_chunk(settings, user, cited["chunk_id"], 1)
        assert evidence["locator"] == {"kind": "pdf", "page": 1}
        assert evidence["text"] == "Python development"
    finally:
        headers = (
            {"api-key": settings.vector_store_api_key.get_secret_value()}
            if settings.vector_store_api_key
            else {}
        )
        httpx.delete(
            f"{settings.vector_store_url.rstrip('/')}/collections/{collection}",
            headers=headers,
            timeout=10,
        )
