"""Leased PostgreSQL-to-Celery dispatch for durable job recovery."""

import logging
from datetime import UTC, datetime, timedelta
from math import pow
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.job import Job, JobEvent

logger = logging.getLogger("fastapi-production-api.job_dispatcher")
Enqueue = Callable[[str], object]


def dispatch_queued_jobs(
    session_factory: sessionmaker,
    enqueue: Enqueue,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    job_ids: list[UUID] | None = None,
) -> int:
    """Lease a bounded set of queued jobs, publish them, and persist outcomes.

    The lease recovers dispatcher crashes. A crash after broker acceptance but
    before recording success can produce a duplicate message; worker state locks
    make delivery at-least-once and prevent terminal jobs from being claimed.
    """
    current = now or datetime.now(UTC)
    batch_limit = limit or settings.JOB_DISPATCH_BATCH_SIZE
    lease_until = current + timedelta(seconds=settings.JOB_DISPATCH_LEASE_SECONDS)
    with session_factory() as db:
        query = db.query(Job).filter(
            Job.status == "queued",
            Job.dispatched_at.is_(None),
            Job.next_dispatch_at <= current,
            or_(
                Job.dispatch_lease_expires_at.is_(None),
                Job.dispatch_lease_expires_at <= current,
            ),
        )
        if job_ids is not None:
            query = query.filter(Job.id.in_(job_ids))
        selected = (
            query.order_by(Job.created_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(batch_limit)
            .all()
        )
        leases = []
        for job in selected:
            token = uuid4()
            job.dispatch_lease_token = token
            job.dispatch_lease_expires_at = lease_until
            job.dispatch_attempt_count += 1
            leases.append((job.id, token))
        db.commit()

    published = 0
    for job_id, token in leases:
        outcome = _publish_leased(session_factory, enqueue, job_id, token, current)
        if outcome == "failed":
            logger.warning(
                "job_dispatch_deferred",
                extra={"job_id": str(job_id), "job_event": "dispatch_deferred"},
            )
            continue
        if outcome == "published":
            published += 1
    return published


def _publish_leased(
    session_factory: sessionmaker,
    enqueue: Enqueue,
    job_id: UUID,
    token: UUID,
    now: datetime,
) -> str:
    with session_factory() as db:
        job = (
            db.query(Job)
            .filter(
                Job.id == job_id,
                Job.status == "queued",
                Job.dispatch_lease_token == token,
            )
            .with_for_update()
            .first()
        )
        if job is None:
            return "skipped"
        try:
            enqueue(str(job_id))
        except Exception:
            _defer_locked_job(job, now, db)
            db.commit()
            return "failed"
        job.dispatched_at = now
        job.dispatch_lease_token = None
        job.dispatch_lease_expires_at = None
        db.commit()
        return "published"


def _defer_locked_job(job: Job, now: datetime, db: Session) -> None:
    delay = min(
        settings.JOB_DISPATCH_BACKOFF_SECONDS
        * pow(2, max(job.dispatch_attempt_count - 1, 0)),
        settings.JOB_DISPATCH_BACKOFF_MAX_SECONDS,
    )
    job.next_dispatch_at = now + timedelta(seconds=delay)
    job.dispatch_lease_token = None
    job.dispatch_lease_expires_at = None
    db.add(
        JobEvent(
            job_id=job.id,
            event_type="dispatch_deferred",
            message="Broker publish failed; dispatch will be retried",
        )
    )


def dispatcher_loop() -> None:
    """Run the standalone recovery dispatcher until the process is stopped."""
    import time

    from app.db.session import SessionLocal
    from app.workers.jobs import process_job

    logger.info("job_dispatcher_started", extra={"job_event": "dispatcher_started"})
    while True:
        try:
            dispatch_queued_jobs(
                SessionLocal,
                lambda job_id: process_job.apply_async(args=[job_id], queue="jobs"),
            )
        except Exception:
            logger.exception(
                "job_dispatcher_poll_failed",
                extra={"job_event": "dispatcher_poll_failed"},
            )
        time.sleep(settings.JOB_DISPATCH_POLL_SECONDS)


if __name__ == "__main__":
    dispatcher_loop()
