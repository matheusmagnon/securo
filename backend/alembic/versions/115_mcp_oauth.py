"""OAuth clients and grants for the MCP server (ChatGPT/Claude connectors)

Revision ID: 115
Revises: 114
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "115"
down_revision = "114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("mcp_oauth_clients"):
        return
    op.create_table(
        "mcp_oauth_clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mcp_oauth_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_oauth_clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.String(2000), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("code_challenge", sa.String(128), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mcp_oauth_grants_client_id", "mcp_oauth_grants", ["client_id"])
    op.create_index("ix_mcp_oauth_grants_user_id", "mcp_oauth_grants", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_grants_user_id", table_name="mcp_oauth_grants")
    op.drop_index("ix_mcp_oauth_grants_client_id", table_name="mcp_oauth_grants")
    op.drop_table("mcp_oauth_grants")
    op.drop_table("mcp_oauth_clients")
