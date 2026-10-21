"""Evidence retrieval for the RAG module (member C, issue #8).

M1 scope fixed by the architecture document: corpus is personal only and
retrieval is dense-only. This module embeds the query, runs the user-scoped
vector search, and returns the *metadata* the backend needs to fetch chunk
text and locators from MongoDB — the authoritative source for quotes, owned by
member B. It deliberately does not join text itself: faking that join here
would blur a boundary the design keeps explicit, so an empty result returns an
explicit reason instead of fabricated evidence.

How the contracts compose:

* this module returns ``EvidenceQueryResult`` (candidates + config version),
  which the backend uses to query the chunks collection;
* ``app.rag.contracts.EvidenceRetriever`` (full evidence with quote and
  locator) is the integration-time contract B assembles once the chunk lookup
  is wired, so D's agent still receives whole evidence records.
"""

from typing import Literal, Protocol

from app.config import Settings, get_settings
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.vector_store import VectorStore, get_vector_store
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel

CORPUS: Literal["personal"] = "personal"


class EvidenceCandidate(ContractModel):
    """Identity and rank of one candidate chunk; quote/locator come from MongoDB."""

    chunk_id: str
    document_id: str
    version: int
    corpus: Literal["personal", "public"]
    retrieval_score: float


class EvidenceQueryResult(ContractModel):
    query: str
    evidence: list[EvidenceCandidate]
    retrieval_config_version: str
    warnings: list[str]


class EvidenceCandidateSource(Protocol):
    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion] | None = None,
        top_k: int = 5,
    ) -> EvidenceQueryResult:
        """Return personal evidence candidates for this user and version snapshot."""
        ...


class QdrantEvidenceSource:
    def __init__(
        self,
        settings: Settings,
        *,
        embedder: EmbeddingProvider | None = None,
        store: VectorStore | None = None,
    ) -> None:
        self._embedder = embedder or get_embedding_provider(settings)
        self._store = store or get_vector_store(settings)

    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion] | None = None,
        top_k: int = 5,
    ) -> EvidenceQueryResult:
        if not query.strip():
            raise ValueError("query cannot be blank")
        (query_vector,) = await self._embedder.embed_texts([query])
        hits = await self._store.search(
            user_id=user_id,
            vector=query_vector,
            document_versions=document_versions,
            top_k=top_k,
        )
        evidence = [
            EvidenceCandidate(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                version=hit.version,
                corpus=hit.corpus,
                retrieval_score=hit.score,
            )
            for hit in hits
        ]
        warnings = []
        if not evidence:
            warnings.append(
                "no personal evidence matched this query under the requested "
                "user and document version filter"
            )
        return EvidenceQueryResult(
            query=query,
            evidence=evidence,
            retrieval_config_version=(
                f"dense/{self._embedder.model_version}@{self._embedder.dimension}/top{top_k}"
            ),
            warnings=warnings,
        )


def get_evidence_source(
    settings: Settings | None = None,
    *,
    embedder: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
) -> EvidenceCandidateSource:
    settings = settings or get_settings()
    return QdrantEvidenceSource(settings, embedder=embedder, store=store)
