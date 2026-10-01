"""Indexes for the retention purge.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_exec_finished", "executions", ["finished_at"], postgresql_where="finished_at IS NOT NULL")
    op.create_index("ix_dead_letters_execution", "dead_letters", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_dead_letters_execution", table_name="dead_letters")
    op.drop_index("ix_exec_finished", table_name="executions")
