"""Exercise the application retrieval boundary, including pre-rerank privacy."""

import asyncio
from types import SimpleNamespace

import pytest

from app import worker
from app.config import Settings
from app.rag.vector_store import SearchHit
from app.schemas.analysis import DocumentVersion


@pytest.mark.parametrize("strategy", ["dense", "hybrid", "hybrid_rerank"])
def test_worker_wires_all_strategies_and_scopes_rerank_text(monkeypatch, strategy):
    seen = []

    class Embedder:
        model_version = "test-embedding"
        dimension = 64

        async def embed_texts(self, texts):
            return [[0.1] * 64 for _ in texts]

    class Store:
        async def search(self, **kwargs):
            assert kwargs["user_id"] == "owner"
            return [
                SearchHit(
                    chunk_id="live", document_id="d", version=2, corpus="personal", score=0.9
                ),
                SearchHit(
                    chunk_id="gone", document_id="deleted", version=1, corpus="personal", score=0.8
                ),
                SearchHit(chunk_id="old", document_id="d", version=1, corpus="personal", score=0.7),
            ]

        async def search_sparse(self, **kwargs):
            assert len(kwargs["sparse_vector"]) > 0
            return await self.search(**kwargs)

    class Reranker:
        model_version = "test-rerank"

        async def rerank(self, *, query, documents, top_n):
            seen.extend(documents)
            assert documents == ["live text"]
            return [(0, 1.0)]

    monkeypatch.setattr("app.rag.retrieval.get_embedding_provider", lambda s: Embedder())
    monkeypatch.setattr("app.rag.retrieval.get_vector_store", lambda s: Store())
    monkeypatch.setattr("app.rag.retrieval.get_rerank_provider", lambda s: Reranker())
    rows = [
        {
            "id": "live",
            "document_id": "d",
            "version": 2,
            "text": "live text",
            "locator": {"kind": "pdf", "page": 3},
        },
        {"id": "gone", "document_id": "deleted", "version": 1, "text": "deleted text"},
        {"id": "old", "document_id": "d", "version": 1, "text": "old text"},
    ]
    monkeypatch.setattr(
        worker, "list_chunks_by_ids", lambda s, u, ids: [r for r in rows if r["id"] in ids]
    )
    monkeypatch.setattr(
        worker,
        "list_document_versions",
        lambda *a: [{"version": 2, "index_version": "test/sparse-fixed"}],
    )
    monkeypatch.setattr(
        worker,
        "get_document",
        lambda s, u, d: (
            SimpleNamespace(current_version=2, status="ready", filename="source.pdf")
            if d == "d"
            else None
        ),
    )
    retriever = worker._build_retriever(Settings(_env_file=None, retrieval_strategy=strategy))
    result = asyncio.run(
        retriever.retrieve_evidence(
            user_id="owner",
            query="live",
            document_versions=[
                DocumentVersion(document_id="d", version=2),
                DocumentVersion(document_id="deleted", version=1),
            ],
        )
    )
    assert [item.chunk_id for item in result] == ["live"]
    assert result[0].locator.page == 3
    assert strategy in retriever.retrieval_config_version
    assert bool(seen) == (strategy == "hybrid_rerank")


def test_deleted_metrics_only_valid_after_real_delete_was_recorded():
    from app.rag.eval_metrics import QueryResult, summarize

    result = QueryResult("deleted", "deleted", "empty", (), (), (), 1, 1, 1, deletion_verified=True)
    assert summarize([result])["negatives"]["deleted"] == {"count": 1, "violations": 0, "rate": 0.0}


def test_empty_rerank_result_does_not_require_candidate_text():
    from app.rag.bm25 import application_encoder
    from app.rag.retrieval import QdrantEvidenceSource

    class Embedder:
        model_version = "test"
        dimension = 64

        async def embed_texts(self, texts):
            return [[0.0] * 64]

    class Store:
        async def search(self, **kw):
            return []

        search_sparse = search

    async def resolve(ids):
        pytest.fail("Empty retrieval must not request any candidate text")

    source = QdrantEvidenceSource(
        Settings(_env_file=None, retrieval_strategy="hybrid_rerank"),
        embedder=Embedder(),
        store=Store(),
        sparse_encoder=application_encoder(),
        rerank_provider=SimpleNamespace(model_version="test-rerank"),
        text_resolver=resolve,
    )
    result = asyncio.run(source.retrieve_evidence(user_id="owner", query="missing"))
    assert result.evidence == []
    assert result.warnings
