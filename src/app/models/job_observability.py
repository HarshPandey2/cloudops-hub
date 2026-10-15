from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    SmallInteger,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobMetricTotals(Base):
    """Small PostgreSQL-backed aggregate shared by API and worker processes."""

    __tablename__ = "job_metric_totals"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_job_metric_totals_singleton"),
        CheckConstraint("queued_jobs >= 0", name="ck_job_metric_queued_nonnegative"),
        CheckConstraint("running_jobs >= 0", name="ck_job_metric_running_nonnegative"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    jobs_submitted_total: Mapped[int] = mapped_column(BigInteger, default=0)
    dispatch_attempts_total: Mapped[int] = mapped_column(BigInteger, default=0)
    dispatch_failures_total: Mapped[int] = mapped_column(BigInteger, default=0)
    executions_started_total: Mapped[int] = mapped_column(BigInteger, default=0)
    executions_succeeded_total: Mapped[int] = mapped_column(BigInteger, default=0)
    executions_failed_total: Mapped[int] = mapped_column(BigInteger, default=0)
    retries_scheduled_total: Mapped[int] = mapped_column(BigInteger, default=0)
    cancellations_total: Mapped[int] = mapped_column(BigInteger, default=0)
    queued_jobs: Mapped[int] = mapped_column(BigInteger, default=0)
    running_jobs: Mapped[int] = mapped_column(BigInteger, default=0)
    execution_duration_count: Mapped[int] = mapped_column(BigInteger, default=0)
    execution_duration_sum: Mapped[float] = mapped_column(Float, default=0.0)
    duration_le_0_1: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_le_0_5: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_le_1: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_le_5: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_le_30: Mapped[int] = mapped_column(BigInteger, default=0)


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"
    __table_args__ = (
        Index("ix_service_heartbeats_service_seen", "service", "last_seen_at"),
    )

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_category: Mapped[str | None] = mapped_column(String(32))
