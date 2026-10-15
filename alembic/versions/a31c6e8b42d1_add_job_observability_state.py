"""add shared job observability aggregates and heartbeats

Revision ID: a31c6e8b42d1
Revises: 4d91f7a2c0e6
Create Date: 2026-10-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a31c6e8b42d1"
down_revision: Union[str, Sequence[str], None] = "4d91f7a2c0e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_metric_totals",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("jobs_submitted_total", sa.BigInteger(), nullable=False),
        sa.Column("dispatch_attempts_total", sa.BigInteger(), nullable=False),
        sa.Column("dispatch_failures_total", sa.BigInteger(), nullable=False),
        sa.Column("executions_started_total", sa.BigInteger(), nullable=False),
        sa.Column("executions_succeeded_total", sa.BigInteger(), nullable=False),
        sa.Column("executions_failed_total", sa.BigInteger(), nullable=False),
        sa.Column("retries_scheduled_total", sa.BigInteger(), nullable=False),
        sa.Column("cancellations_total", sa.BigInteger(), nullable=False),
        sa.Column("queued_jobs", sa.BigInteger(), nullable=False),
        sa.Column("running_jobs", sa.BigInteger(), nullable=False),
        sa.Column("execution_duration_count", sa.BigInteger(), nullable=False),
        sa.Column("execution_duration_sum", sa.Float(), nullable=False),
        sa.Column("duration_le_0_1", sa.BigInteger(), nullable=False),
        sa.Column("duration_le_0_5", sa.BigInteger(), nullable=False),
        sa.Column("duration_le_1", sa.BigInteger(), nullable=False),
        sa.Column("duration_le_5", sa.BigInteger(), nullable=False),
        sa.Column("duration_le_30", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_job_metric_totals_singleton"),
        sa.CheckConstraint("queued_jobs >= 0", name="ck_job_metric_queued_nonnegative"),
        sa.CheckConstraint(
            "running_jobs >= 0", name="ck_job_metric_running_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(length=32), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("service", "instance_id"),
    )
    op.create_index(
        "ix_service_heartbeats_service_seen",
        "service_heartbeats",
        ["service", "last_seen_at"],
    )
    op.create_index(
        "ix_jobs_status_created",
        "jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_job_events_type_created",
        "job_events",
        ["event_type", "created_at"],
    )
    op.execute(
        sa.text(
            "INSERT INTO job_metric_totals (id, jobs_submitted_total, "
            "dispatch_attempts_total, dispatch_failures_total, "
            "executions_started_total, executions_succeeded_total, "
            "executions_failed_total, retries_scheduled_total, cancellations_total, "
            "queued_jobs, running_jobs, execution_duration_count, "
            "execution_duration_sum, duration_le_0_1, duration_le_0_5, "
            "duration_le_1, duration_le_5, duration_le_30) "
            "SELECT 1, "
            "(SELECT count(*) FROM job_events WHERE event_type = 'submitted'), "
            "(SELECT coalesce(sum(dispatch_attempt_count), 0) FROM jobs), "
            "(SELECT count(*) FROM job_events WHERE event_type = 'dispatch_deferred'), "
            "(SELECT count(*) FROM job_events WHERE event_type = 'started'), "
            "(SELECT count(*) FROM job_events WHERE event_type = 'succeeded'), "
            "(SELECT count(*) FROM job_events WHERE event_type = 'failed'), "
            "(SELECT count(*) FROM job_events WHERE event_type = 'retry_scheduled'), "
            "(SELECT count(*) FROM job_events WHERE event_type = 'cancelled'), "
            "(SELECT count(*) FROM jobs WHERE status = 'queued'), "
            "(SELECT count(*) FROM jobs WHERE status = 'running'), "
            "0, 0, 0, 0, 0, 0, 0"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_job_events_type_created", table_name="job_events")
    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_index("ix_service_heartbeats_service_seen", table_name="service_heartbeats")
    op.drop_table("service_heartbeats")
    op.drop_table("job_metric_totals")
