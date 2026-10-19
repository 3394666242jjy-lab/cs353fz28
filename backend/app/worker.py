from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery("careerpilot", broker=settings.celery_broker_url.get_secret_value())
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
)


@celery_app.task(name="careerpilot.infrastructure_ping")
def infrastructure_ping() -> dict[str, str]:
    """Diagnostic only. No document or analysis task is implemented in this scaffold."""
    return {"worker": "ok", "mode": "scaffold"}
