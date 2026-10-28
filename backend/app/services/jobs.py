"""Version-one JD persistence, always scoped to the authenticated owner."""

from datetime import UTC, datetime
from uuid import uuid4

from app.config import Settings
from app.schemas.resources import JobCreate, JobCreated, JobResult
from app.services.database import database_connection


def save_job(settings: Settings, user_id: str, payload: JobCreate) -> JobCreated:
    job_id = f"job_{uuid4().hex}"
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        database.jobs.create_index("id", unique=True)
        database.jobs.create_index([("user_id", 1), ("created_at", -1)])
        database.jobs.insert_one(
            {
                **payload.model_dump(),
                "id": job_id,
                "user_id": user_id,
                "version": 1,
                "requirements": [],
                "created_at": now,
                "updated_at": now,
            }
        )
    return JobCreated(job_id=job_id, version=1)


def load_job(settings: Settings, user_id: str, job_id: str) -> JobResult | None:
    with database_connection(settings) as database:
        document = database.jobs.find_one({"id": job_id, "user_id": user_id})
    if document is None:
        return None
    return JobResult(
        id=document["id"],
        title=document["title"],
        company=document.get("company"),
        raw_jd=document["raw_jd"],
        version=document["version"],
    )
