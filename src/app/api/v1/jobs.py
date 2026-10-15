import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, sessionmaker

from app.auth.current_user import get_current_user
from app.core.config import settings
from app.db.dependency import get_db
from app.models.job import JOB_TRANSITIONS, Job, JobEvent
from app.models.user import User
from app.schemas.job import JobCreate, JobEventResponse, JobResponse
from app.services.job_dispatcher import dispatch_queued_jobs
from app.services.job_handlers import JobPayloadError, validate_job_input
from app.workers.jobs import process_job

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger("fastapi-production-api.jobs")


def _owned_job(
    db: Session,
    job_id: UUID,
    owner_id: int,
    *,
    lock: bool = False,
) -> Job:
    query = db.query(Job).filter(Job.id == job_id, Job.owner_id == owner_id)
    if lock:
        query = query.with_for_update()
    job = query.first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def submit_job(
    request: JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        input_payload = validate_job_input(request.job_type, request.input_payload)
    except JobPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    job = Job(
        owner_id=user.id,
        job_type=request.job_type,
        input_payload=input_payload,
        status="queued",
    )
    db.add(job)
    db.flush()
    db.add(JobEvent(job_id=job.id, event_type="submitted", message="Job queued"))
    db.commit()
    db.refresh(job)
    dispatch_sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    try:
        dispatch_queued_jobs(
            dispatch_sessions, process_job.delay, limit=1, job_ids=[job.id]
        )
    except Exception:
        logger.error(
            "job_dispatch_failed",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "job_event": "dispatch_failed",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job was saved but could not be submitted to the worker",
        ) from None
    db.refresh(job)
    if job.status == "queued" and job.dispatched_at is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job was saved but could not be submitted to the worker",
        )
    logger.info(
        "job_accepted",
        extra={
            "job_id": str(job.id),
            "job_type": job.job_type,
            "job_event": "accepted",
        },
    )
    return job


@router.get("", response_model=list[JobResponse])
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    job_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(queued|running|succeeded|failed|cancelled)$",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    query = db.query(Job).filter(Job.owner_id == user.id)
    if job_status is not None:
        query = query.filter(Job.status == job_status)
    return (
        query.order_by(Job.created_at.desc(), Job.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _owned_job(db, job_id, user.id)


@router.get("/{job_id}/history", response_model=list[JobEventResponse])
def get_job_history(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = _owned_job(db, job_id, user.id)
    return (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.created_at, JobEvent.id)
        .all()
    )


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = _owned_job(db, job_id, user.id, lock=True)
    if job.status != "queued" or "cancelled" not in JOB_TRANSITIONS[job.status]:
        logger.warning(
            "job_cancellation_rejected",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "job_event": "cancellation_rejected",
            },
        )
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a job in the {job.status} state",
        )
    job.status = "cancelled"
    job.completed_at = datetime.now(UTC)
    db.add(
        JobEvent(
            job_id=job.id, event_type="cancelled", message="Job cancelled by owner"
        )
    )
    db.commit()
    db.refresh(job)
    logger.info(
        "job_cancellation_accepted",
        extra={
            "job_id": str(job.id),
            "job_type": job.job_type,
            "job_event": "cancelled",
        },
    )
    return job


@router.post(
    "/{job_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = _owned_job(db, job_id, user.id, lock=True)
    if (
        job.status != "failed"
        or not job.retryable
        or job.retry_count >= settings.JOB_MAX_RETRIES
    ):
        raise HTTPException(status_code=409, detail="Job is not eligible for retry")
    job.status = "queued"
    job.retryable = True
    job.retry_count += 1
    job.started_at = None
    job.completed_at = None
    job.error_message = None
    job.dispatched_at = None
    job.dispatch_lease_token = None
    job.dispatch_lease_expires_at = None
    job.next_dispatch_at = datetime.now(UTC)
    db.add(
        JobEvent(
            job_id=job.id,
            event_type="retry_scheduled",
            message=f"Manual retry {job.retry_count} submitted",
        )
    )
    db.commit()
    db.refresh(job)
    dispatch_sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    try:
        dispatch_queued_jobs(
            dispatch_sessions, process_job.delay, limit=1, job_ids=[job.id]
        )
    except Exception:
        logger.error(
            "job_retry_dispatch_failed",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "job_event": "retry_dispatch_failed",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job retry was saved but could not be submitted to the worker",
        ) from None
    db.refresh(job)
    if job.status == "queued" and job.dispatched_at is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job retry was saved but could not be submitted to the worker",
        )
    logger.info(
        "job_retry_accepted",
        extra={
            "job_id": str(job.id),
            "job_type": job.job_type,
            "retry_count": job.retry_count,
            "job_event": "retry_accepted",
        },
    )
    return job
