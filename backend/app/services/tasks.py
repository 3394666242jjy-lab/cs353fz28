"""Celery dispatch helpers for the API process.

The broker client is imported lazily so the API stays importable (and its
tests fast) without Redis; failures surface at dispatch time and are handled
by the caller per dev/08 (a dispatch failure must fail the execution, never
leave it queued forever).
"""

import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.services.documents import list_purge_candidates

logger = logging.getLogger(__name__)


def dispatch_index_task(
    settings: Settings,
    *,
    storage_key: str,
    user_id: str,
    document_id: str,
    version: int,
    execution_id: str,
) -> None:
    """Enqueue the indexing task; raises when the broker is unreachable."""
    from app.worker import celery_app

    celery_app.send_task(
        "careerpilot.index_document",
        args=[storage_key, user_id, document_id, version, execution_id],
    )


def dispatch_analysis_task(
    settings: Settings,
    *,
    analysis_id: str,
    user_id: str,
    execution_id: str,
) -> None:
    """Enqueue the analysis task; raises when the broker is unreachable."""
    from app.worker import celery_app

    celery_app.send_task(
        "careerpilot.run_analysis",
        args=[analysis_id, user_id, execution_id],
    )


def dispatch_purge_task(
    settings: Settings,
    *,
    user_id: str,
    document_id: str,
) -> None:
    """Enqueue asynchronous purge of chunks, vectors and files for a deleted document."""
    from app.worker import celery_app

    celery_app.send_task(
        "careerpilot.purge_document",
        args=[user_id, document_id],
    )


def sweep_pending_purges(settings: Settings, *, limit: int = 20) -> int:
    """Re-dispatch purge for deleted documents stuck at pending/failed.

    A purge task retries itself with backoff; this sweeper covers the worse
    case where the task never started (broker down at DELETE time) or died
    permanently. The 60-second quiet window avoids racing an active retry.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=60)
    candidates = list_purge_candidates(settings, before=cutoff, limit=limit)
    dispatched = 0
    for candidate in candidates:
        try:
            dispatch_purge_task(settings, user_id=candidate["user_id"], document_id=candidate["id"])
        except Exception:  # noqa: BLE001 - next sweep round retries
            logger.exception("Could not re-dispatch purge for %s", candidate["id"])
            continue
        dispatched += 1
    return dispatched
