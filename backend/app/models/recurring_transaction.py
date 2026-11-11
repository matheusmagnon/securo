import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false, func

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.category import Category
    from app.models.recurring_transaction_category_line import RecurringTransactionCategoryLine
    from app.models.transaction import Transaction
    from app.models.user import User


class RecurringTransaction(Base):
    __tablename__ = "recurring_transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "description", "frequency", "start_date", name="uq_recurring_tx"),
        CheckConstraint(
            "weekend_adjustment IN ('none', 'previous_friday', 'next_monday')",
            name="ck_recurring_transactions_weekend_adjustment",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True)
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(500))
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    type: Mapped[str] = mapped_column(String(10))  # debit, credit
    frequency: Mapped[str] = mapped_column(String(20))  # weekly, biweekly, monthly, quarterly, semiannual, yearly
    weekend_adjustment: Mapped[str] = mapped_column(
        String(20), default="none", server_default="none"
    )
    day_of_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When True (the default), generate_pending materializes this bill's due
    # occurrences into real transactions. When False, no placeholder is written
    # and the bill is only shown as a projection until the actual charge is
    # matched to it (e.g. from bank sync). Either way, incoming real
    # transactions are linked back to the bill to avoid duplicates.
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Generate each occurrence on the first day of its month instead of on
    # its due date, so a bill shows as upcoming before it is paid.
    generate_at_month_start: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # A bill paid any day of its month (an installment paid ahead of its due
    # date): a charge of the same month answers the occurrence, wherever in
    # the month it lands, instead of only near the due date.
    match_whole_month: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Installments of the contract paid before this bill was created, so the
    # counter follows the contract's numbering ("5 of 156", not "1 of 152").
    installment_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # A bill shared with a group: every occurrence starts split equally among
    # its members, and the real charge that replaces it keeps the split.
    split_group_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    next_occurrence: Mapped[date] = mapped_column(Date)
    amount_primary: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    fx_rate_used: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=20, scale=10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # True while the amount is split across several categories, kept in
    # `category_lines`; `category_id` is then null. Every generated
    # occurrence starts with the same split.
    has_category_lines: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )

    user: Mapped["User"] = relationship()
    account: Mapped[Optional["Account"]] = relationship()
    category: Mapped[Optional["Category"]] = relationship()
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="recurring_transaction"
    )
    category_lines: Mapped[list["RecurringTransactionCategoryLine"]] = relationship(
        back_populates="recurring_transaction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RecurringTransactionCategoryLine.position",
        lazy="selectin",
    )

    def _count_occurrences(self, before: Optional[date] = None, through: Optional[date] = None) -> int:
        from app.services.recurring_transaction_service import _advance_date

        intended_day = self.day_of_month or self.start_date.day
        current, count = self.start_date, 0
        while count < 5000:
            if before is not None and current >= before:
                break
            if through is not None and current > through:
                break
            count += 1
            current = _advance_date(current, self.frequency, intended_day=intended_day)
        return count

    @property
    def occurrences_done(self) -> int:
        """Occurrences behind the bill: generated, or paid ahead of one."""
        if self.start_date is None or self.next_occurrence is None:
            return 0
        done = self._count_occurrences(before=self.next_occurrence) + (self.installment_offset or 0)
        total = self.occurrences_total
        return min(done, total) if total is not None else done

    @property
    def occurrences_total(self) -> Optional[int]:
        """How many occurrences a bill with an end date has in all."""
        if self.start_date is None or self.end_date is None:
            return None
        return self._count_occurrences(through=self.end_date) + (self.installment_offset or 0)
