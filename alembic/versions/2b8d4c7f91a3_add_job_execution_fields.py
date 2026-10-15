"""persist job execution attempts and output

Revision ID: 2b8d4c7f91a3
Revises: 7a2d4c9e1f30
Create Date: 2026-10-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "2b8d4c7f91a3"
down_revision: Union[str, Sequence[str], None] = "7a2d4c9e1f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "jobs",
        sa.Column("retryable", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("jobs", sa.Column("output_payload", sa.JSON(), nullable=True))
    op.create_check_constraint("ck_jobs_attempt_count", "jobs", "attempt_count >= 0")
    op.alter_column("jobs", "attempt_count", server_default=None)
    op.alter_column("jobs", "retryable", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_attempt_count", "jobs", type_="check")
    op.drop_column("jobs", "output_payload")
    op.drop_column("jobs", "retryable")
    op.drop_column("jobs", "attempt_count")
