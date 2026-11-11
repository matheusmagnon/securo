"""transfers dismissed as settlement suggestions

Revision ID: 112
Revises: 111
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "112"
down_revision = "111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("group_settlement_dismissals"):
        return
    op.create_table(
        "group_settlement_dismissals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.UniqueConstraint("group_id", "transaction_id", name="uq_group_settlement_dismissal"),
    )
    op.create_index(
        "ix_group_settlement_dismissals_group_id", "group_settlement_dismissals", ["group_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_group_settlement_dismissals_group_id", table_name="group_settlement_dismissals")
    op.drop_table("group_settlement_dismissals")
