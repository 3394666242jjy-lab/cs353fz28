"""证据检索（RAG 模块，成员 C，issue #8。

架构文档固定的 M1 范围：语料仅 personal、检索仅稠密向量。本模块把查询向量化、
执行按用户隔离的向量检索，然后返回**后端查询 chunk 原文所需的元数据**——
quote 与 locator 的权威副本在 MongoDB（成员 B 的数据域），因此这里刻意不做文本
拼接：在此放一个替身会把设计明确分开的边界弄浑。检索为空时返回明确原因，
绝不生成假证据（开发规范）。

契约如何组合：

* 本模块返回 EvidenceQueryResult（候选 + 检索配置版本），后端据此查询 chunks 集合；
* app.rag.contracts.EvidenceRetriever（含 quote/locator 的完整证据）是联调期契约，
  由 B 接上 chunk 查询后组装，D 的 Agent 最终拿到的仍是完整证据记录。
"""

from typing import Literal, Protocol

from app.config import Settings, get_settings
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.vector_store import VectorStore, get_vector_store
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel

# M1 固定个人语料；公共知识语料在后续阶段引入，届时经同一接口扩展。
CORPUS: Literal["personal"] = "personal"


class EvidenceCandidate(ContractModel):
    """一个候选 chunk 的身份与排序信息；quote/locator 由后端查 MongoDB 补齐。"""

    chunk_id: str
    document_id: str
    version: int
    corpus: Literal["personal", "public"]
    retrieval_score: float


class EvidenceQueryResult(ContractModel):
    """一次检索的结果：查询原文、候选列表、检索配置版本、以及为空时的原因。"""

    query: str
    evidence: list[EvidenceCandidate]
    retrieval_config_version: str
    warnings: list[str]


class EvidenceCandidateSource(Protocol):
    """证据候选源协议：调用方只依赖接口，不绑定 Qdrant 实现。"""

    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion] | None = None,
        top_k: int | None = None,
    ) -> EvidenceQueryResult:
        """为指定用户与版本快照返回个人证据候选；top_k 缺省取配置值。"""
        ...


class QdrantEvidenceSource:
    """基于向量库的候选源实现：查询向量化 + 用户隔离检索 + 结果组装。"""

    def __init__(
        self,
        settings: Settings,
        *,
        embedder: EmbeddingProvider | None = None,
        store: VectorStore | None = None,
    ) -> None:
        # 依赖均可注入：测试可传 MockTransport 适配器，生产默认从全局配置构建。
        self._settings = settings
        self._embedder = embedder or get_embedding_provider(settings)
        self._store = store or get_vector_store(settings)

    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion] | None = None,
        top_k: int | None = None,
    ) -> EvidenceQueryResult:
        """执行一次证据检索并返回候选元数据（不包含原文文本）。

        空结果不抛异常：按开发规范返回空列表加明确原因，由上层（Agent）判定
        "资料未体现"。检索失败（模型/存储类错误）如实向上传播。
        """
        if not query.strip():
            raise ValueError("查询文本不能为空白")
        top_k = top_k if top_k is not None else self._settings.retrieval_top_k
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
            warnings.append("在请求的用户与文档版本范围内，没有匹配该查询的个人证据")
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
    """工厂：默认从全局配置构建候选源；可显式注入配置与依赖供测试使用。"""
    settings = settings or get_settings()
    return QdrantEvidenceSource(settings, embedder=embedder, store=store)
