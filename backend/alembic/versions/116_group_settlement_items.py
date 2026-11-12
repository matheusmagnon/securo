"""Which shared transactions each group settlement paid off

Revision ID: 116
Revises: 115
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "116"
down_revision = "115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("group_settlement_items"):
        return
    op.create_table(
        "group_settlement_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "settlement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("group_settlements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("group_id", "transaction_id", name="uq_group_settlement_item"),
    )
    op.create_index("ix_group_settlement_items_settlement_id", "group_settlement_items", ["settlement_id"])
    op.create_index("ix_group_settlement_items_group_id", "group_settlement_items", ["group_id"])


def downgrade() -> None:
    op.drop_table("group_settlement_items")
