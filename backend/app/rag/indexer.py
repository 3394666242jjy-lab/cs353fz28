"""文档入库管线（RAG 模块，成员 C，issue #8）。

编排架构文档定义的 M1 入库流程：解析 → 切分 → 向量化 → 写入 Qdrant，并返回
成员 B 落库到 document_versions 所需的版本元数据，以及 chunks 集合的 chunk 记录。
C 拥有这条管线；B 调用它并保存结果。

对同一 (document, version) 重复执行 index_document 是幂等的：chunk_id 与
Qdrant point id 都是确定性派生，且存储层按版本整体替换而非追加，因此半失败后的
重试、以及换了切分参数的重索引，都不会留下过期向量。
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

# M1 语料固定为个人经历（架构文档）；公共知识语料在后续阶段引入。
CORPUS = "personal"


class IndexedChunk(ContractModel):
    """一个已索引 chunk：B 落 chunks 集合所需的全部字段（含 Qdrant point id）。"""

    chunk_id: str
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    locator: TextLocator
    vector_id: str


class IndexedDocument(ContractModel):
    """一次索引构建的结果；版本元数据字段与 document_versions 一一对应。"""

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
    """由 (文档, 版本, 序号) 确定性派生 chunk_id，同版本重索引复用同一记录。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{version}:{ordinal}"))


def build_index_version(
    *,
    parser_version: str,
    chunking_config_version: str,
    embedding_model: str,
    embedding_dimension: int,
) -> str:
    """组合索引形态标签：管线任一环节变化都会得到新的 index_version。"""
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
    """把一个文档版本入库：解析、切分、向量化、整体替换写入，返回版本元数据。

    执行顺序刻意安排为"先向量化、后写存储"：模型调用失败时旧索引原样保留，
    不会把一个好版本打成空；依赖（embedder/store/settings）均可注入以便测试。
    """
    settings = settings or get_settings()
    embedder = embedder or get_embedding_provider(settings)
    store = store or get_vector_store(settings)

    parsed = parse_document(raw)
    chunks = chunk_document(parsed.lines, max_chars=settings.chunk_max_chars)
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
