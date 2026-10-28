"""Owner-scoped MongoDB persistence for analysis records (dev/03).

The analyses collection holds D's requirement snapshot and match results; B
owns the record lifecycle (create on request, fill on worker success, mark
failed) and assembles the public AnalysisResult contract on read.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from app.config import Settings
from app.schemas.analysis import AnalysisResult, DocumentVersion, Match
from app.schemas.resources import JobResult
from app.services.database import database_connection

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create M1 indexes per dev/03; safe to repeat on every call."""
    database.analyses.create_index("id", unique=True)
    database.analyses.create_index([("user_id", 1), ("created_at", -1)])
    database.analyses.create_index([("job_id", 1), ("user_id", 1)])


def create_analysis(
    settings: Settings,
    user_id: str,
    *,
    job_id: str,
    job_version: int,
    document_versions: list[DocumentVersion],
    job_snapshot: JobResult | None = None,
) -> str:
    """Create a pending analysis pinned to the job and document versions in use."""
    if job_snapshot is not None and (
        job_snapshot.id != job_id or job_snapshot.version != job_version
    ):
        raise ValueError("Job snapshot does not match the pinned version")
    analysis_id = f"analysis_{uuid4().hex}"
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        database.analyses.insert_one(
            {
                "id": analysis_id,
                "user_id": user_id,
                "job_id": job_id,
                "job_version": job_version,
                "job_snapshot": job_snapshot.model_dump() if job_snapshot is not None else None,
                "document_versions": [item.model_dump() for item in document_versions],
                "requirement_snapshot": [],
                "matches": [],
                "warnings": [],
                "stale": False,
                "status": STATUS_PENDING,
                "error": None,
                "trace_id": None,
                "prompt_version": None,
                "retrieval_config_version": None,
                "model_version": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    return analysis_id


def get_analysis(settings: Settings, user_id: str, analysis_id: str) -> dict[str, Any] | None:
    """Return one analysis row owned by the user, or None."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        row = database.analyses.find_one({"id": analysis_id, "user_id": user_id}, {"_id": 0})
    return row


def requirement_snapshot_from_matches(matches: list[Match]) -> list[dict[str, Any]]:
    """Derive D's requirement snapshot from the validated match contract."""
    return [
        {"id": match.requirement_id, "text": match.requirement, "category": match.category}
        for match in matches
    ]


def save_analysis_result(
    settings: Settings, user_id: str, analysis_id: str, result: AnalysisResult
) -> bool:
    """Persist a validated AnalysisResult and flip the record to ready."""
    result = AnalysisResult.model_validate(result)
    with database_connection(settings) as database:
        _ensure_indexes(database)
        updated = database.analyses.update_one(
            {
                "id": analysis_id,
                "user_id": user_id,
                "status": STATUS_PENDING,
                "job_id": result.job_id,
                "job_version": result.job_version,
                "document_versions": [item.model_dump() for item in result.document_versions],
            },
            {
                "$set": {
                    "requirement_snapshot": requirement_snapshot_from_matches(result.matches),
                    "matches": [match.model_dump() for match in result.matches],
                    "warnings": result.warnings,
                    "stale": False,
                    "status": STATUS_READY,
                    "error": None,
                    "trace_id": result.trace_id,
                    "prompt_version": result.prompt_version,
                    "retrieval_config_version": result.retrieval_config_version,
                    "model_version": result.model_version,
                    "updated_at": datetime.now(UTC),
                }
            },
        )
    return updated.matched_count == 1


def set_analysis_failed(settings: Settings, user_id: str, analysis_id: str, *, error: str) -> bool:
    """Mark a pending analysis failed with the owner-visible reason."""
    with database_connection(settings) as database:
        _ensure_indexes(database)
        updated = database.analyses.update_one(
            {"id": analysis_id, "user_id": user_id, "status": STATUS_PENDING},
            {"$set": {"status": STATUS_FAILED, "error": error, "updated_at": datetime.now(UTC)}},
        )
    return updated.matched_count == 1
