from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

JOB_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset({"queued"}),
    "cancelled": frozenset(),
}


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_jobs_retry_count"),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count"),
        CheckConstraint(
            "dispatch_attempt_count >= 0", name="ck_jobs_dispatch_attempt_count"
        ),
        Index("ix_jobs_owner_created", "owner_id", "created_at"),
        Index("ix_jobs_owner_status_created", "owner_id", "status", "created_at"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    next_dispatch_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    dispatch_lease_token: Mapped[UUID | None] = mapped_column()
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    output_payload: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        Index("ix_job_events_job_created", "job_id", "created_at"),
        Index("ix_job_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
