from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import pow
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.job import Job, JobEvent


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    job_type: str
    input_payload: dict[str, Any]
    attempt_count: int


def claim_job(
    session_factory: sessionmaker,
    job_id: UUID,
    *,
    now: datetime | None = None,
    redelivered: bool = False,
) -> ClaimedJob | None:
    started_at = now or datetime.now(UTC)
    stale_before = started_at - timedelta(seconds=settings.JOB_STALE_RUNNING_SECONDS)
    with session_factory() as db:
        job = db.query(Job).filter(Job.id == job_id).with_for_update().first()
        if job is None:
            return None
        next_dispatch = job.next_dispatch_at
        if next_dispatch.tzinfo is None:
            next_dispatch = next_dispatch.replace(tzinfo=UTC)
        if job.status == "queued" and next_dispatch > started_at:
            return None
        recovered = job.status == "running" and job.started_at is not None
        previous_start = job.started_at
        if previous_start is not None and previous_start.tzinfo is None:
            previous_start = previous_start.replace(tzinfo=UTC)
        stale = recovered and previous_start <= stale_before
        if job.status != "queued" and not stale:
            return None

        if stale:
            db.add(
                JobEvent(
                    job_id=job.id,
                    event_type="recovered",
                    message="Stale running job recovered by worker",
                )
            )

        job.status = "running"
        job.dispatch_lease_token = None
        job.dispatch_lease_expires_at = None
        job.started_at = started_at
        job.completed_at = None
        job.error_message = None
        job.attempt_count += 1
        db.add(
            JobEvent(
                job_id=job.id,
                event_type="started",
                message=f"Job execution attempt {job.attempt_count} started",
            )
        )
        claim = ClaimedJob(
            id=job.id,
            job_type=job.job_type,
            input_payload=job.input_payload,
            attempt_count=job.attempt_count,
        )
        db.commit()
        return claim


def mark_job_succeeded(
    session_factory: sessionmaker,
    job_id: UUID,
    output: dict[str, Any],
    *,
    now: datetime | None = None,
    expected_attempt_count: int,
) -> bool:
    completed_at = now or datetime.now(UTC)
    with session_factory() as db:
        job = _locked_running_job(db, job_id, expected_attempt_count)
        if job is None:
            return False
        job.status = "succeeded"
        job.retryable = False
        job.output_payload = output
        job.error_message = None
        job.completed_at = completed_at
        db.add(
            JobEvent(
                job_id=job.id,
                event_type="succeeded",
                message="Job completed successfully",
            )
        )
        db.commit()
        return True


def schedule_job_retry(
    session_factory: sessionmaker,
    job_id: UUID,
    safe_error: str,
    *,
    expected_attempt_count: int,
) -> int | None:
    with session_factory() as db:
        job = _locked_running_job(db, job_id, expected_attempt_count)
        if job is None or job.retry_count >= settings.JOB_MAX_RETRIES:
            return None
        job.retry_count += 1
        job.status = "queued"
        job.dispatched_at = None
        job.dispatch_lease_token = None
        job.dispatch_lease_expires_at = None
        delay_seconds = min(
            settings.JOB_RETRY_BACKOFF_SECONDS * pow(2, job.retry_count - 1),
            settings.JOB_RETRY_BACKOFF_MAX_SECONDS,
        )
        job.next_dispatch_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        job.retryable = True
        job.completed_at = None
        job.error_message = safe_error[:500]
        db.add(
            JobEvent(
                job_id=job.id,
                event_type="retry_scheduled",
                message=f"Retry {job.retry_count} scheduled",
            )
        )
        db.commit()
        return job.retry_count


def mark_job_failed(
    session_factory: sessionmaker,
    job_id: UUID,
    safe_error: str,
    *,
    expected_status: str = "running",
    retryable: bool = True,
    expected_attempt_count: int | None = None,
) -> bool:
    completed_at = datetime.now(UTC)
    with session_factory() as db:
        query = db.query(Job).filter(Job.id == job_id, Job.status == expected_status)
        if expected_attempt_count is not None:
            query = query.filter(Job.attempt_count == expected_attempt_count)
        job = query.with_for_update().first()
        if job is None:
            return False
        job.status = "failed"
        job.retryable = retryable
        job.error_message = safe_error[:500]
        job.completed_at = completed_at
        db.add(JobEvent(job_id=job.id, event_type="failed", message=safe_error[:500]))
        db.commit()
        return True


def _locked_running_job(
    db: Session, job_id: UUID, expected_attempt_count: int
) -> Job | None:
    return (
        db.query(Job)
        .filter(
            Job.id == job_id,
            Job.status == "running",
            Job.attempt_count == expected_attempt_count,
        )
        .with_for_update()
        .first()
    )
