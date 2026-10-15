"""Celery worker heartbeat persisted for cross-process health checks."""

import logging
import os
import socket
import threading

from celery.signals import worker_ready, worker_shutdown

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.job_observability import record_service_heartbeat

logger = logging.getLogger("fastapi-production-api.worker_heartbeat")
_stop = threading.Event()
_thread: threading.Thread | None = None


def _heartbeat_loop(instance_id: str) -> None:
    while not _stop.is_set():
        try:
            record_service_heartbeat(
                SessionLocal,
                service="worker",
                instance_id=instance_id,
            )
        except Exception:
            logger.warning(
                "worker_heartbeat_write_failed",
                extra={"job_event": "worker_heartbeat_failed"},
            )
        _stop.wait(settings.JOB_WORKER_HEARTBEAT_INTERVAL_SECONDS)


@worker_ready.connect(weak=False)
def start_worker_heartbeat(sender=None, **_kwargs) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    instance_id = f"{socket.gethostname()}:{os.getpid()}"[:128]
    _thread = threading.Thread(
        target=_heartbeat_loop,
        args=(instance_id,),
        name="job-worker-heartbeat",
        daemon=True,
    )
    _thread.start()
    logger.info(
        "worker_heartbeat_started",
        extra={"job_event": "worker_heartbeat_started"},
    )


@worker_shutdown.connect(weak=False)
def stop_worker_heartbeat(**_kwargs) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=settings.JOB_WORKER_HEARTBEAT_INTERVAL_SECONDS + 1)
