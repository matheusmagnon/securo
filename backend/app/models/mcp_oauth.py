import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class McpOAuthClient(Base):
    """An MCP client (ChatGPT, Claude…) registered through OAuth dynamic
    client registration. Public client: no secret, PKCE is mandatory."""

    __tablename__ = "mcp_oauth_clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_name: Mapped[str] = mapped_column(String(200))
    redirect_uris: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class McpOAuthGrant(Base):
    """One user's consent for one client.

    Born holding a short-lived authorization code; the token exchange burns
    the code and stores a refresh token, rotated on every refresh. Only
    SHA-256 hashes of the code and refresh token are kept. Deleting the row
    (or setting `revoked_at`) disconnects that client.
    """

    __tablename__ = "mcp_oauth_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_oauth_clients.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    redirect_uri: Mapped[str] = mapped_column(String(2000))
    code_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    code_challenge: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    code_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    refresh_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
