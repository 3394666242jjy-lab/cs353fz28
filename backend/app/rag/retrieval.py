"""证据检索（RAG 模块，成员 C，issue #8 / #25）。

本模块把查询向量化、执行按用户隔离的检索，然后返回**后端查询 chunk 原文所需的
元数据**——quote 与 locator 的权威副本在 MongoDB（成员 B 的数据域），因此这里刻意
不做文本拼接：在此放一个替身会把设计明确分开的边界弄浑。检索为空时返回明确原因，
绝不生成假证据（开发规范）。

三档策略（issue #25）：

* ``dense`` —— M1 基线，只用稠密向量；
* ``hybrid`` —— 稠密 + BM25 关键词稀疏，两路各取候选后按通道权重融合；
* ``hybrid_rerank`` —— 在 hybrid 之上再重排，属 S4，未实现前显式报错，绝不静默降级。

契约如何组合：

* 本模块返回 EvidenceQueryResult（候选 + 检索配置版本），后端据此查询 chunks 集合；
* app.rag.contracts.EvidenceRetriever（含 quote/locator 的完整证据）是联调期契约，
  由 B 接上 chunk 查询后组装，D 的 Agent 最终拿到的仍是完整证据记录。
"""

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from app.config import Settings, get_settings
from app.rag.bm25 import Bm25Encoder
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.rerank import RerankProvider, get_rerank_provider
from app.rag.vector_store import SearchHit, VectorStore, get_vector_store
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel

# M1 固定个人语料；公共知识语料在后续阶段引入，届时经同一接口扩展。
CORPUS: Literal["personal"] = "personal"

# 检索策略（issue #25 的三档可复现对比）。dense 是 M1 基线，另两档在 M2 实现。
RetrievalStrategy = Literal["dense", "hybrid", "hybrid_rerank"]


def build_retrieval_config_version(
    *,
    strategy: RetrievalStrategy,
    model_version: str,
    dimension: int,
    top_k: int,
    sparse_weight: float | None = None,
    rerank_model: str | None = None,
) -> str:
    """组装检索配置版本号；任何一项变化都产生新版本，旧结果不得被覆盖。

    格式在此冻结（S0 决策），评测比较必须连同该版本号一起记录，否则无法说明
    "三档是在同一条件下比出来的"：

    * ``dense/text-embedding-v4@1024/top5``
    * ``hybrid/w0.50/text-embedding-v4@1024/top5``
    * ``hybrid_rerank/gte-rerank/w0.50/text-embedding-v4@1024/top5``

    权重按两位小数归一（``w0.50``），避免浮点尾数让同一配置产生两个版本号。
    """
    core = f"{model_version}@{dimension}/top{top_k}"
    if strategy == "dense":
        return f"dense/{core}"
    if sparse_weight is None:
        raise ValueError(f"策略 {strategy} 必须给出稀疏通道权重")
    weight = f"w{sparse_weight:.2f}"
    if strategy == "hybrid":
        return f"hybrid/{weight}/{core}"
    if not rerank_model:
        raise ValueError("hybrid_rerank 必须给出 rerank 模型名，否则无法区分是否真的重排过")
    return f"hybrid_rerank/{rerank_model}/{weight}/{core}"


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


# 候选原文解析器：给定 chunk_id 返回原文。原文权威副本在 MongoDB（成员 B 的数据域），
# 因此由调用方注入而不是 C 自己查库——C 只持有 chunk_id，重排却需要文本。
TextResolver = Callable[[list[str]], Awaitable[dict[str, str]]]


def _normalise(scores: dict[str, float]) -> dict[str, float]:
    """把一路通道的分数 min-max 归一到 [0, 1]。

    稠密余弦分与 BM25 分数量级不同，直接加权求和无异于让一路通吃。通道内只有一个
    候选（或分数全相同）时统一给 1.0，避免把唯一候选压成 0。
    """
    if not scores:
        return {}
    low, high = min(scores.values()), max(scores.values())
    if high - low < 1e-12:
        return {chunk_id: 1.0 for chunk_id in scores}
    return {chunk_id: (score - low) / (high - low) for chunk_id, score in scores.items()}


def fuse_hits(
    dense_hits: list[SearchHit],
    sparse_hits: list[SearchHit],
    *,
    sparse_weight: float,
) -> list[SearchHit]:
    """按权重融合稠密与稀疏两路候选。

    权重是**通道**权重，不是原始分量级权重：两路先各自归一，再按
    (1 - w) * dense + w * sparse 合成。只在某一通道出现的候选，另一通道记 0。

    排序在分数相同时按 chunk_id 决定，保证同一输入两次运行结果完全一致。
    """
    if not 0 < sparse_weight < 1:
        raise ValueError(f"稀疏权重必须落在 (0, 1) 开区间，收到 {sparse_weight}")
    dense_scores = _normalise({hit.chunk_id: hit.score for hit in dense_hits})
    sparse_scores = _normalise({hit.chunk_id: hit.score for hit in sparse_hits})
    by_id: dict[str, SearchHit] = {}
    for hit in [*dense_hits, *sparse_hits]:
        by_id.setdefault(hit.chunk_id, hit)
    fused = [
        hit.model_copy(
            update={
                "score": (1 - sparse_weight) * dense_scores.get(chunk_id, 0.0)
                + sparse_weight * sparse_scores.get(chunk_id, 0.0)
            }
        )
        for chunk_id, hit in by_id.items()
    ]
    fused.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    return fused


class QdrantEvidenceSource:
    """基于向量库的候选源实现：查询向量化 + 用户隔离检索 + 结果组装。"""

    def __init__(
        self,
        settings: Settings,
        *,
        embedder: EmbeddingProvider | None = None,
        store: VectorStore | None = None,
        sparse_encoder: Bm25Encoder | None = None,
        rerank_provider: RerankProvider | None = None,
        text_resolver: TextResolver | None = None,
    ) -> None:
        # 依赖均可注入：测试可传 MockTransport 适配器，生产默认从全局配置构建。
        self._settings = settings
        self._embedder = embedder or get_embedding_provider(settings)
        self._store = store or get_vector_store(settings)
        self._sparse_encoder = sparse_encoder
        self._rerank = (
            rerank_provider if rerank_provider is not None else get_rerank_provider(settings)
        )
        self._text_resolver = text_resolver

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
        strategy = self._settings.retrieval_strategy
        (query_vector,) = await self._embedder.embed_texts([query])
        warnings: list[str] = []

        if strategy == "dense":
            hits = await self._store.search(
                user_id=user_id,
                vector=query_vector,
                document_versions=document_versions,
                top_k=top_k,
            )
            config_version = build_retrieval_config_version(
                strategy="dense",
                model_version=self._embedder.model_version,
                dimension=self._embedder.dimension,
                top_k=top_k,
            )
        elif strategy == "hybrid":
            hits, config_version = await self._hybrid_hits(
                user_id=user_id,
                query=query,
                query_vector=query_vector,
                document_versions=document_versions,
                top_k=top_k,
            )
        elif strategy == "hybrid_rerank":
            hits, config_version, warnings = await self._hybrid_rerank_hits(
                user_id=user_id,
                query=query,
                query_vector=query_vector,
                document_versions=document_versions,
                top_k=top_k,
            )
        else:
            raise RuntimeError(
                f"检索策略 {strategy} 尚未实现（Rerank 适配器属 S4）；"
                "不得静默降级为未重排结果，否则评测结论会被污染"
            )

        evidence = [
            EvidenceCandidate(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                version=hit.version,
                corpus=hit.corpus,
                retrieval_score=hit.score,
            )
            for hit in hits[:top_k]
        ]
        if not evidence:
            warnings.append("在请求的用户与文档版本范围内，没有匹配该查询的个人证据")
        return EvidenceQueryResult(
            query=query,
            evidence=evidence,
            retrieval_config_version=config_version,
            warnings=warnings,
        )

    async def _hybrid_hits(
        self,
        *,
        user_id: str,
        query: str,
        query_vector: list[float],
        document_versions: list[DocumentVersion] | None,
        top_k: int,
    ) -> tuple[list[SearchHit], str]:
        """两路各取候选后按权重融合；缺编码器必须报错而不是退回稠密。"""
        if self._sparse_encoder is None:
            raise RuntimeError(
                "混合检索需要 BM25 编码器（含 IDF 语料统计）；未提供时不得降级为稠密检索"
            )
        sparse_weight = self._settings.hybrid_sparse_weight
        per_channel = max(top_k, self._settings.hybrid_candidates_per_channel)
        dense_hits = await self._store.search(
            user_id=user_id,
            vector=query_vector,
            document_versions=document_versions,
            top_k=per_channel,
        )
        sparse_hits = await self._store.search_sparse(
            user_id=user_id,
            sparse_vector=self._sparse_encoder.encode_query(query),
            document_versions=document_versions,
            top_k=per_channel,
        )
        config_version = build_retrieval_config_version(
            strategy="hybrid",
            model_version=self._embedder.model_version,
            dimension=self._embedder.dimension,
            top_k=top_k,
            sparse_weight=sparse_weight,
        )
        return fuse_hits(dense_hits, sparse_hits, sparse_weight=sparse_weight), config_version

    async def _hybrid_rerank_hits(
        self,
        *,
        user_id: str,
        query: str,
        query_vector: list[float],
        document_versions: list[DocumentVersion] | None,
        top_k: int,
    ) -> tuple[list[SearchHit], str, list[str]]:
        """在 hybrid 候选上做重排。

        重排需要候选原文，而原文在 MongoDB：拿不到解析器或某个候选缺原文时，都不允许
        悄悄跳过——跳过会让"未重排"的结果被当成"已重排"交付。缺原文的候选被排除并
        记录告警；整批都缺则报错。
        """
        if self._rerank is None:
            raise RuntimeError(
                "hybrid_rerank 需要 Rerank provider（RERANK_MODEL / 密钥）；"
                "未配置时不得静默降级为未重排结果"
            )
        if self._text_resolver is None:
            raise RuntimeError(
                "hybrid_rerank 需要候选原文解析器（chunk 原文权威副本在 MongoDB，"
                "由调用方注入）；未提供时无法重排"
            )
        fused, _ = await self._hybrid_hits(
            user_id=user_id,
            query=query,
            query_vector=query_vector,
            document_versions=document_versions,
            top_k=top_k,
        )
        candidates = fused[: max(top_k, self._settings.hybrid_candidates_per_channel)]
        texts = await self._text_resolver([hit.chunk_id for hit in candidates])
        scored_pool = [hit for hit in candidates if texts.get(hit.chunk_id)]
        warnings: list[str] = []
        missing = len(candidates) - len(scored_pool)
        if missing:
            warnings.append(f"{missing} 个候选缺少原文，未参与重排并被排除")
        if not scored_pool:
            raise RuntimeError("候选全部缺少原文，无法重排；请检查候选原文解析器")

        # rerank 按输入顺序给出 (下标, 分数)，这里按分数降序取前 top_k。
        scored = await self._rerank.rerank(
            query=query,
            documents=[texts[hit.chunk_id] for hit in scored_pool],
            top_n=top_k,
        )
        reranked = [
            scored_pool[index].model_copy(update={"score": score}) for index, score in scored
        ]
        config_version = build_retrieval_config_version(
            strategy="hybrid_rerank",
            model_version=self._embedder.model_version,
            dimension=self._embedder.dimension,
            top_k=top_k,
            sparse_weight=self._settings.hybrid_sparse_weight,
            rerank_model=self._rerank.model_version,
        )
        return reranked, config_version, warnings


def get_evidence_source(
    settings: Settings | None = None,
    *,
    embedder: EmbeddingProvider | None = None,
    store: VectorStore | None = None,
    sparse_encoder: Bm25Encoder | None = None,
    rerank_provider: RerankProvider | None = None,
    text_resolver: TextResolver | None = None,
) -> EvidenceCandidateSource:
    """工厂：默认从全局配置构建候选源；可显式注入配置与依赖供测试使用。"""
    settings = settings or get_settings()
    return QdrantEvidenceSource(
        settings,
        embedder=embedder,
        store=store,
        sparse_encoder=sparse_encoder,
        rerank_provider=rerank_provider,
        text_resolver=text_resolver,
    )
