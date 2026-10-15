from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.db.dependency import get_db
from app.models.job import JOB_TRANSITIONS, Job, JobEvent
from app.models.user import User
from app.schemas.job import JobCreate, JobEventResponse, JobResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _owned_job(db: Session, job_id: UUID, owner_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.owner_id == owner_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def submit_job(
    request: JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = Job(
        owner_id=user.id,
        job_type=request.job_type,
        input_payload=request.input_payload,
        status="queued",
    )
    db.add(job)
    db.flush()
    db.add(JobEvent(job_id=job.id, event_type="submitted", message="Job queued"))
    db.commit()
    db.refresh(job)
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
    job = _owned_job(db, job_id, user.id)
    if "cancelled" not in JOB_TRANSITIONS[job.status]:
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
    return job
