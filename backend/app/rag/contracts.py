from typing import Protocol

from app.schemas.analysis import DocumentVersion, Evidence


class EvidenceRetriever(Protocol):
    async def retrieve_evidence(
        self,
        *,
        user_id: str,
        query: str,
        document_versions: list[DocumentVersion],
        top_k: int = 5,
    ) -> list[Evidence]:
        """Return real personal evidence filtered to this user and version snapshot."""
        ...
