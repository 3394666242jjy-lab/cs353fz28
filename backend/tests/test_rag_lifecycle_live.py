"""Opt-in real Mongo/Qdrant/Embedding/Rerank checks with a stopped purge queue.

RUN_RAG_M2_TESTS=1 enables paid model calls on synthetic text only. All resources
use random isolated names. Queue delivery is controlled to test deletion before
physical cleanup; storage, HTTP handlers and retrieval providers are real.
"""

import asyncio
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import worker
from app.config import Settings
from app.main import create_app
from app.rag.vector_store import get_vector_store
from app.schemas.analysis import DocumentVersion
from app.services.database import database_connection
from app.services.documents import get_deleted_document
from app.services.users import seed_local_user


@pytest.mark.skipif(os.environ.get("RUN_RAG_M2_TESTS") != "1", reason="Opt-in real model calls")
def test_live_update_delete_and_all_retrieval_strategies(tmp_path, monkeypatch):
    suffix = uuid4().hex
    settings = Settings().model_copy(
        update={
            "mongodb_database": f"careerpilot_rag_live_{suffix}",
            "vector_store_collection": f"careerpilot_rag_live_{suffix}",
            "local_storage_dir": tmp_path,
            "retrieval_strategy": "hybrid_rerank",
        }
    )
    password = secrets.token_urlsafe(24)
    settings.seed_email = f"rag-{suffix}@example.invalid"
    seed_local_user(settings, password)
    client = TestClient(create_app(settings))
    token = client.post(
        "/api/v1/auth/login", json={"email": settings.seed_email, "password": password}
    ).json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_id = client.get("/api/v1/me", headers=headers).json()["data"]["id"]

    def index(config, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(worker.run_indexing, config, **kwargs).result()

    monkeypatch.setattr("app.api.routes.dispatch_index_task", index)
    # Explicit queue fault: do not run purge until we prove immediate blocking.
    monkeypatch.setattr("app.api.routes.dispatch_purge_task", lambda *a, **kw: None)
    try:
        uploaded = client.post(
            "/api/v1/documents",
            headers=headers,
            data={"kind": "project"},
            files={
                "file": (
                    "synthetic.md",
                    b"Synthetic project: designed MongoDB collections and Redis cache.",
                    "text/markdown",
                )
            },
        )
        assert uploaded.status_code == 202
        doc_id = uploaded.json()["data"]["document_id"]
        assert (
            client.get(f"/api/v1/documents/{doc_id}", headers=headers).json()["data"]["status"]
            == "ready"
        )

        async def retrieve(strategy, version=1, owner=user_id):
            return await worker._build_retriever(
                settings.model_copy(update={"retrieval_strategy": strategy})
            ).retrieve_evidence(
                user_id=owner,
                query="MongoDB Redis",
                document_versions=[DocumentVersion(document_id=doc_id, version=version)],
            )

        for strategy in ("dense", "hybrid", "hybrid_rerank"):
            hits = asyncio.run(retrieve(strategy))
            assert hits and "MongoDB" in hits[0].quote
            assert hits[0].locator.kind == "text"
            assert asyncio.run(retrieve(strategy, owner="another-user")) == []
        updated = client.put(
            f"/api/v1/documents/{doc_id}/text",
            headers=headers,
            json={
                "expected_version": 1,
                "extracted_text": "Synthetic v2: MongoDB schema and Redis cache with expiry.",
            },
        )
        assert updated.status_code == 202
        for strategy in ("dense", "hybrid", "hybrid_rerank"):
            assert asyncio.run(retrieve(strategy)) == []
            hits = asyncio.run(retrieve(strategy, 2))
            assert hits and all(hit.version == 2 for hit in hits)
        chunk_id = hits[0].chunk_id
        with database_connection(settings) as database:
            keys = [
                row["source_key"]
                for row in database.document_versions.find({"document_id": doc_id})
            ]
        deleted = client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        assert deleted.status_code == 202
        assert get_deleted_document(settings, user_id, doc_id)["purge_status"] == "pending"
        # Vectors still exist: blocking cannot be credited to eventual purge.
        store = get_vector_store(settings)
        from app.rag.embeddings import get_embedding_provider

        vector = asyncio.run(get_embedding_provider(settings).embed_texts(["MongoDB"]))[0]
        assert asyncio.run(
            store.search(
                user_id=user_id,
                vector=vector,
                document_versions=[DocumentVersion(document_id=doc_id, version=2)],
            )
        )
        for strategy in ("dense", "hybrid", "hybrid_rerank"):
            assert asyncio.run(retrieve(strategy, 2)) == []
        assert client.get(f"/api/v1/documents/{doc_id}", headers=headers).status_code == 404
        assert (
            client.get(f"/api/v1/evidence/{chunk_id}?version=2", headers=headers).status_code == 404
        )
        worker._run_purge(settings, user_id, doc_id)
        worker._run_purge(settings, user_id, doc_id)
        assert get_deleted_document(settings, user_id, doc_id)["purge_status"] == "done"
        assert not asyncio.run(store.search(user_id=user_id, vector=vector))
        assert all(not (tmp_path / key).exists() for key in keys)
    finally:
        with database_connection(settings) as database:
            database.client.drop_database(settings.mongodb_database)
        with httpx.Client(trust_env=False) as cleanup:
            key = settings.vector_store_api_key
            cleanup.delete(
                f"{settings.vector_store_url}/collections/{settings.vector_store_collection}",
                headers={"api-key": key.get_secret_value()} if key else {},
            )
