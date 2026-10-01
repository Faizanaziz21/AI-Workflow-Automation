"""Partial index for the stalled-execution sweep.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_exec_active_created",
        "executions",
        ["created_at"],
        postgresql_where="status IN ('RUNNING', 'RETRYING')",
    )


def downgrade() -> None:
    op.drop_index("ix_exec_active_created", table_name="executions")
