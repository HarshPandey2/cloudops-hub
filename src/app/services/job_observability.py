"""Shared low-cardinality metrics and operational state for background jobs."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.job import Job, JobEvent
from app.models.job_observability import JobMetricTotals, ServiceHeartbeat

DURATION_BUCKETS = (
    (0.1, "duration_le_0_1"),
    (0.5, "duration_le_0_5"),
    (1.0, "duration_le_1"),
    (5.0, "duration_le_5"),
    (30.0, "duration_le_30"),
)


@dataclass(frozen=True)
class JobObservabilitySnapshot:
    totals: dict[str, int | float]
    queued_age_seconds: float
    recent_failures: int
    dispatcher_last_success: datetime | None
    worker_last_seen: datetime | None


def update_job_metrics(
    db: Session,
    *,
    increments: dict[str, int] | None = None,
    transition: tuple[str, str] | None = None,
    execution_duration_seconds: float | None = None,
) -> None:
    totals = db.query(JobMetricTotals).filter_by(id=1).with_for_update().first()
    created_totals = totals is None
    if totals is None:
        state_counts = dict(
            db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        )
        totals = JobMetricTotals(
            id=1,
            queued_jobs=state_counts.get("queued", 0),
            running_jobs=state_counts.get("running", 0),
        )
        db.add(totals)
        db.flush()

    for field, amount in (increments or {}).items():
        if field not in {
            "jobs_submitted_total",
            "dispatch_attempts_total",
            "dispatch_failures_total",
            "executions_started_total",
            "executions_succeeded_total",
            "executions_failed_total",
            "retries_scheduled_total",
            "cancellations_total",
        }:
            raise ValueError("Unsupported job metric")
        setattr(totals, field, getattr(totals, field) + amount)

    # When there is no aggregate row, the status transition has already been
    # applied to the Job in this transaction; the initial counts reflect it.
    if transition is not None and not created_totals:
        previous, current = transition
        if previous == "queued":
            totals.queued_jobs -= 1
        elif previous == "running":
            totals.running_jobs -= 1
        if current == "queued":
            totals.queued_jobs += 1
        elif current == "running":
            totals.running_jobs += 1

    if execution_duration_seconds is not None:
        duration = max(0.0, execution_duration_seconds)
        totals.execution_duration_count += 1
        totals.execution_duration_sum += duration
        for bound, field in DURATION_BUCKETS:
            if duration <= bound:
                setattr(totals, field, getattr(totals, field) + 1)


def record_service_heartbeat(
    session_factory: sessionmaker,
    *,
    service: str,
    instance_id: str,
    now: datetime | None = None,
    successful: bool = True,
    error_category: str | None = None,
) -> None:
    current = now or datetime.now(UTC)
    with session_factory() as db:
        row = (
            db.query(ServiceHeartbeat)
            .filter_by(service=service, instance_id=instance_id)
            .with_for_update()
            .first()
        )
        if row is None:
            row = ServiceHeartbeat(
                service=service,
                instance_id=instance_id,
                last_seen_at=current,
            )
            db.add(row)
        row.last_seen_at = current
        row.last_error_category = error_category
        if successful:
            row.last_success_at = current
        db.commit()


def load_job_observability_snapshot(
    session_factory: sessionmaker,
    *,
    now: datetime | None = None,
    statement_timeout_seconds: float | None = None,
) -> JobObservabilitySnapshot:
    current = now or datetime.now(UTC)
    with session_factory() as db:
        if (
            statement_timeout_seconds is not None
            and db.get_bind().dialect.name == "postgresql"
        ):
            db.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{int(statement_timeout_seconds * 1000)}ms"},
            )
        totals_row = db.query(JobMetricTotals).filter_by(id=1).first()
        if totals_row is None:
            totals = {field: 0 for field in _TOTAL_FIELDS}
            totals["execution_duration_sum"] = 0.0
        else:
            totals = {field: getattr(totals_row, field) for field in _TOTAL_FIELDS}
        oldest_queued = (
            db.query(Job.created_at)
            .filter(
                Job.status == "queued",
                Job.next_dispatch_at <= current,
                or_(
                    Job.dispatch_lease_expires_at.is_(None),
                    Job.dispatch_lease_expires_at <= current,
                ),
            )
            .order_by(Job.created_at)
            .first()
        )
        queued_age = (
            max(0.0, (current - _as_utc(oldest_queued[0])).total_seconds())
            if oldest_queued
            else 0.0
        )
        recent_failures = (
            db.query(JobEvent.id)
            .filter(
                JobEvent.event_type.in_(["failed", "retry_scheduled"]),
                JobEvent.created_at
                >= current - timedelta(seconds=settings.JOB_FAILURE_WINDOW_SECONDS),
            )
            .count()
        )
        dispatcher_last_success = (
            db.query(ServiceHeartbeat.last_success_at)
            .filter(ServiceHeartbeat.service == "dispatcher")
            .order_by(ServiceHeartbeat.last_success_at.desc())
            .limit(1)
            .scalar()
        )
        worker_last_seen = (
            db.query(ServiceHeartbeat.last_seen_at)
            .filter(ServiceHeartbeat.service == "worker")
            .order_by(ServiceHeartbeat.last_seen_at.desc())
            .limit(1)
            .scalar()
        )
    return JobObservabilitySnapshot(
        totals=totals,
        queued_age_seconds=queued_age,
        recent_failures=recent_failures,
        dispatcher_last_success=(
            _as_utc(dispatcher_last_success) if dispatcher_last_success else None
        ),
        worker_last_seen=_as_utc(worker_last_seen) if worker_last_seen else None,
    )


def evaluate_job_alerts(
    snapshot: JobObservabilitySnapshot,
    *,
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now(UTC)
    alerts = []
    if snapshot.queued_age_seconds >= settings.JOB_QUEUE_AGE_ALERT_SECONDS:
        alerts.append("queue_age_exceeded")
    if snapshot.recent_failures >= settings.JOB_FAILURE_ALERT_THRESHOLD:
        alerts.append("repeated_job_failures")
    if _is_stale(
        snapshot.dispatcher_last_success,
        current,
        settings.JOB_DISPATCHER_STALE_SECONDS,
    ):
        alerts.append("dispatcher_stale")
    if _is_stale(
        snapshot.worker_last_seen,
        current,
        settings.JOB_WORKER_HEARTBEAT_STALE_SECONDS,
    ):
        alerts.append("worker_heartbeat_stale")
    return alerts


def _is_stale(value: datetime | None, now: datetime, threshold_seconds: int) -> bool:
    return value is None or (now - _as_utc(value)).total_seconds() > threshold_seconds


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


_TOTAL_FIELDS = (
    "jobs_submitted_total",
    "dispatch_attempts_total",
    "dispatch_failures_total",
    "executions_started_total",
    "executions_succeeded_total",
    "executions_failed_total",
    "retries_scheduled_total",
    "cancellations_total",
    "queued_jobs",
    "running_jobs",
    "execution_duration_count",
    "execution_duration_sum",
    "duration_le_0_1",
    "duration_le_0_5",
    "duration_le_1",
    "duration_le_5",
    "duration_le_30",
)
