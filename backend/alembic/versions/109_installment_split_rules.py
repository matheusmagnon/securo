"""split every later parcel of a shared installment purchase

Revision ID: 109
Revises: 108
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "109"
down_revision = "108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "installment_split_rules" in inspector.get_table_names():
        return
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "installment_split_rules",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", uuid, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", uuid, sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=False),
        sa.Column("total_installments", sa.SmallInteger(), nullable=False),
        sa.Column("description_key", sa.String(255), nullable=False),
        sa.Column("from_installment", sa.SmallInteger(), nullable=False),
        sa.Column("share_type", sa.String(10), nullable=False),
        sa.Column("shares", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "purchase_date",
            "total_installments",
            "description_key",
            name="uq_installment_split_rules_purchase",
        ),
    )
    op.create_index("ix_installment_split_rules_workspace_id", "installment_split_rules", ["workspace_id"])
    op.create_index("ix_installment_split_rules_account_id", "installment_split_rules", ["account_id"])
    op.create_index("ix_installment_split_rules_group_id", "installment_split_rules", ["group_id"])


def downgrade() -> None:
    op.drop_table("installment_split_rules")
