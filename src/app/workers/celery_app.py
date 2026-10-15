from celery import Celery

from app.core.config import settings

broker_url = settings.CELERY_BROKER_URL.get_secret_value()

celery_app = Celery("cloudops_hub", broker=broker_url)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    result_backend=None,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
    broker_connection_max_retries=0,
    task_publish_retry=False,
    task_routes={"app.jobs.process": {"queue": "jobs"}},
    task_default_queue="jobs",
)
celery_app.conf.imports = ("app.workers.jobs",)
