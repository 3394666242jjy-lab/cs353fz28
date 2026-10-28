"""Celery dispatch helpers for the API process.

The broker client is imported lazily so the API stays importable (and its
tests fast) without Redis; failures surface at dispatch time and are handled
by the caller per dev/08 (a dispatch failure must fail the execution, never
leave it queued forever).
"""

from app.config import Settings


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
