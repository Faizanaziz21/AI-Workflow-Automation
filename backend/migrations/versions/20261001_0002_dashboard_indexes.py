"""dashboard indexes

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01 20:00:14.507055
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_exec_events_org_type_created", "execution_events", ["org_id", "event_type", "created_at"])
    op.create_index("ix_node_runs_org_finished", "node_runs", ["org_id", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_node_runs_org_finished", table_name="node_runs")
    op.drop_index("ix_exec_events_org_type_created", table_name="execution_events")
