from typing import Protocol

from app.rag.contracts import EvidenceRetriever
from app.schemas.analysis import AnalysisResult, DocumentVersion
from app.schemas.resources import JobResult


class AnalysisAgent(Protocol):
    async def run_analysis(
        self,
        *,
        user_id: str,
        job: JobResult,
        document_versions: list[DocumentVersion],
        retriever: EvidenceRetriever,
    ) -> AnalysisResult:
        """Member D supplies the real LangGraph implementation; never return fixtures."""
        ...
