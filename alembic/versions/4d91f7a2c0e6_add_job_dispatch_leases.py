"""add recoverable job dispatch leases

Revision ID: 4d91f7a2c0e6
Revises: 2b8d4c7f91a3
Create Date: 2026-10-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "4d91f7a2c0e6"
down_revision: Union[str, Sequence[str], None] = "2b8d4c7f91a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "dispatch_attempt_count", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "next_dispatch_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column("jobs", sa.Column("dispatch_lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column(
            "dispatch_lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "jobs", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_jobs_dispatch_attempt_count", "jobs", "dispatch_attempt_count >= 0"
    )
    op.create_index(
        "ix_jobs_dispatch_recovery",
        "jobs",
        ["status", "dispatched_at", "next_dispatch_at", "dispatch_lease_expires_at"],
    )
    op.alter_column("jobs", "dispatch_attempt_count", server_default=None)
    op.alter_column("jobs", "next_dispatch_at", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_jobs_dispatch_recovery", table_name="jobs")
    op.drop_constraint("ck_jobs_dispatch_attempt_count", "jobs", type_="check")
    op.drop_column("jobs", "dispatched_at")
    op.drop_column("jobs", "dispatch_lease_expires_at")
    op.drop_column("jobs", "dispatch_lease_token")
    op.drop_column("jobs", "next_dispatch_at")
    op.drop_column("jobs", "dispatch_attempt_count")
