"""installments of a contract paid before its recurring bill existed

Revision ID: 113
Revises: 112
"""

import sqlalchemy as sa

from alembic import op

revision = "113"
down_revision = "112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("recurring_transactions")}
    if "installment_offset" not in columns:
        op.add_column(
            "recurring_transactions",
            sa.Column("installment_offset", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("recurring_transactions", "installment_offset")
