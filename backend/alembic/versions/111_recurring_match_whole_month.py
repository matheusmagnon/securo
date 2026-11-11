"""recurring bills matched by any charge of the same month

Revision ID: 111
Revises: 110
"""

import sqlalchemy as sa

from alembic import op

revision = "111"
down_revision = "110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("recurring_transactions")}
    if "match_whole_month" not in columns:
        op.add_column(
            "recurring_transactions",
            sa.Column("match_whole_month", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column("recurring_transactions", "match_whole_month")
