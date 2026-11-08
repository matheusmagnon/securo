"""A purchase paid in installments whose parcels are shared with a group.

Banks send each parcel of a card purchase as its own transaction, month by
month. Splitting one parcel with "this and the next installments" stores a
rule here; every later parcel of the same purchase (already imported, or
arriving with a future sync) gets the same split.

A purchase is identified like the rest of the codebase identifies an
installment series (account, purchase date, total installments) plus the
description with the "3/4" / "PARC 03/04" marker removed, so two purchases
made the same day on the same card with the same number of parcels don't
get mixed up.
"""
import uuid
from datetime import date as _date
from datetime import datetime, timezone

from sqlalchemy import JSON, Date, DateTime, ForeignKey, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InstallmentSplitRule(Base):
    __tablename__ = "installment_split_rules"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "purchase_date",
            "total_installments",
            "description_key",
            name="uq_installment_split_rules_purchase",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # Who set the split: later parcels are split on their behalf.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    purchase_date: Mapped[_date] = mapped_column(Date)
    total_installments: Mapped[int] = mapped_column(SmallInteger)
    description_key: Mapped[str] = mapped_column(String(255))
    # Parcels numbered from here on are split; earlier ones are left alone.
    from_installment: Mapped[int] = mapped_column(SmallInteger)
    # "equal" or "percent" (exact amounts are stored as percentages, since
    # parcels of one purchase can differ by a cent).
    share_type: Mapped[str] = mapped_column(String(10))
    # [{"group_member_id": str, "share_pct": str | None}]
    shares: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
