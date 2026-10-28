from typing import Protocol

from app.schemas.analysis import DocumentVersion, Evidence


class EvidenceRetriever(Protocol):
    """联调期证据检索契约：返回含 quote/locator 的完整证据记录。

    由成员 B 在接上 MongoDB chunk 查询后组装实现；成员 C 的
    app.rag.retrieval 提供候选元数据（不含原文），本协议面向 D 的 Agent。
    """

    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion],
        top_k: int = 5,
    ) -> list[Evidence]:
        """返回按用户与版本快照过滤的真实个人证据；检索不到时不得编造。"""
        ...
