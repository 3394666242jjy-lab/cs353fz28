"""Document indexing pipeline for the RAG module (member C, issue #8).

Orchestrates the M1 ingestion flow from the architecture document:
parse -> chunk -> embed -> write to Qdrant, returning the version metadata
member B persists into ``document_versions`` and the chunk records for
``chunks``. C owns this pipeline; B calls it and stores the results.

Re-running ``index_document`` for the same (document, version) is idempotent:
chunk ids and Qdrant point ids are deterministic, and the store replaces the
whole version instead of appending, so retries after a partial failure and
re-indexes with new chunking parameters leave no stale points behind.
"""

import uuid
from typing import Literal

from pydantic import Field

from app.config import Settings, get_settings
from app.rag.chunking import (
    PARSER_VERSION,
    chunk_document,
    chunking_config_version,
    parse_document,
)
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.vector_store import (
    DENSE_VECTOR_NAME,
    ChunkPoint,
    VectorStore,
    get_vector_store,
    point_id,
)
from app.schemas.analysis import DocumentVersion, TextLocator
from app.schemas.common import ContractModel

CORPUS = "personal"


class IndexedChunk(ContractModel):
    """One indexed chunk with everything member B needs for the chunks collection."""

    chunk_id: str
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    locator: TextLocator
    vector_id: str


class IndexedDocument(ContractModel):
    """Result of one index build; the metadata mirrors document_versions."""

    document_id: str
    version: int = Field(ge=1)
    user_id: str
    corpus: Literal["personal"]
    chunk_count: int = Field(ge=0)
    parser_version: str
    chunking_config_version: str
    embedding_model: str
    embedding_dimension: int
    index_version: str
    chunks: list[IndexedChunk]
    document_versions: list[DocumentVersion]


def make_chunk_id(document_id: str, version: int, ordinal: int) -> str:
    """Deterministic id so a re-index of the same version reuses its records."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{version}:{ordinal}"))


def build_index_version(
    *,
    parser_version: str,
    chunking_config_version: str,
    embedding_model: str,
    embedding_dimension: int,
) -> str:
    """Index shape tag: changes whenever any pipeline component changes."""
    return (
        f"{parser_version}/{chunking_config_version}/{embedding_model}"
        f"@{embedding_dimension}/{DENSE_VECTOR_NAME}-Cosine"
    )


async def index_document(
    raw: bytes,
    *,
    user_id: str,
    document_id: str,
    version: int,
    embedder: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
    settings: Settings | None = None,
) -> IndexedDocument:
    settings = settings or get_settings()
    embedder = embedder or get_embedding_provider(settings)
    store = store or get_vector_store(settings)

    parsed = parse_document(raw)
    chunks = chunk_document(parsed.lines, max_chars=settings.chunk_max_chars)
    # Embed before writing so a model failure leaves the previous index intact.
    vectors = await embedder.embed_texts([chunk.text for chunk in chunks])

    index_version = build_index_version(
        parser_version=PARSER_VERSION,
        chunking_config_version=chunking_config_version(settings.chunk_max_chars),
        embedding_model=embedder.model_version,
        embedding_dimension=embedder.dimension,
    )
    points = [
        ChunkPoint(
            chunk_id=make_chunk_id(document_id, version, chunk.ordinal),
            document_id=document_id,
            version=version,
            corpus=CORPUS,
            user_id=user_id,
            index_version=index_version,
            vector=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    await store.replace_document_points(
        user_id=user_id, document_id=document_id, version=version, points=points
    )
    return IndexedDocument(
        document_id=document_id,
        version=version,
        user_id=user_id,
        corpus=CORPUS,
        chunk_count=len(points),
        parser_version=PARSER_VERSION,
        chunking_config_version=chunking_config_version(settings.chunk_max_chars),
        embedding_model=embedder.model_version,
        embedding_dimension=embedder.dimension,
        index_version=index_version,
        chunks=[
            IndexedChunk(
                chunk_id=point.chunk_id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                locator=chunk.locator,
                vector_id=point_id(point),
            )
            for chunk, point in zip(chunks, points, strict=True)
        ],
        document_versions=[DocumentVersion(document_id=document_id, version=version)],
    )
