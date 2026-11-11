"""recurring bills shared with a group, generated at the start of the month

Revision ID: 110
Revises: 109
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "110"
down_revision = "109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("recurring_transactions")}
    if "generate_at_month_start" not in columns:
        op.add_column(
            "recurring_transactions",
            sa.Column(
                "generate_at_month_start", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    if "split_group_id" not in columns:
        op.add_column(
            "recurring_transactions",
            sa.Column(
                "split_group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("groups.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("recurring_transactions", "split_group_id")
    op.drop_column("recurring_transactions", "generate_at_month_start")
