"""One month of a shared household, at a glance.

The group page answers "who owes whom"; this answers what a couple checks
each month: what the house cost, who paid how much of it, which of the
shared bills are paid and which are still due, and how far along the
long contracts (an installment plan, a down payment) are.

Only what went through the group is read: split transactions and the
recurring bills shared with it. A member's own accounts, cards and
unsplit spending stay out of it, so the view is the same for both.

Amounts follow the group page's convention: ``amount_primary`` when the
sync stored one, the native ``amount`` otherwise.
"""

import calendar
import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.group import GroupMember
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit

_ZERO = Decimal("0")


def _month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def _amount(tx: Transaction) -> Decimal:
    value = tx.amount_primary if tx.amount_primary is not None else tx.amount
    return Decimal(str(value or 0)).copy_abs()


def _status(tx: Transaction, today: date) -> str:
    """planned: a bill's placeholder, not paid yet. pending: charged but not
    settled by the bank (an open card bill). upcoming: dated later. paid."""
    if tx.source == "recurring":
        return "planned"
    if tx.date > today:
        return "upcoming"
    if tx.status == "pending":
        return "pending"
    return "paid"


async def compute_overview(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    month: date,
) -> Optional[dict]:
    from app.core.app_clock import get_workspace_timezone, today_in
    from app.services import balance_service
    from app.services.recurring_transaction_service import get_occurrences_in_range

    balances = await balance_service.compute_balances(session, group_id, workspace_id, user_id)
    if balances is None:
        return None
    from app.services.group_service import get_group_visible

    group = await get_group_visible(session, group_id, workspace_id, user_id)
    today = today_in(await get_workspace_timezone(session, group.workspace_id))
    first, last = _month_bounds(month)

    members = list(
        (await session.execute(select(GroupMember).where(GroupMember.group_id == group_id)))
        .scalars()
        .all()
    )
    member_ids = [m.id for m in members]
    # Same payer rule as the balances: the member linked to whoever recorded
    # the transaction, else the owner's own member.
    by_user = {m.linked_user_id: m.id for m in members if m.linked_user_id is not None}
    owner_self = next(
        (m.id for m in members if m.linked_user_id == group.user_id),
        next((m.id for m in members if m.is_self and m.linked_user_id is None), None),
    )

    def payer_of(recorder: uuid.UUID) -> Optional[uuid.UUID]:
        return by_user.get(recorder, owner_self)

    # --- Split transactions of the month ---------------------------------
    rows = (
        await session.execute(
            select(Transaction, TransactionSplit, Category)
            .join(TransactionSplit, TransactionSplit.transaction_id == Transaction.id)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(
                TransactionSplit.group_member_id.in_(member_ids),
                Transaction.date >= first,
                Transaction.date <= last,
                Transaction.is_ignored.is_(False),
            )
            .order_by(Transaction.date, Transaction.id)
        )
    ).all()

    items: dict[uuid.UUID, dict] = {}
    for tx, split, category in rows:
        item = items.get(tx.id)
        if item is None:
            item = items[tx.id] = {
                "transaction_id": tx.id,
                "date": tx.date,
                "description": tx.description,
                "category_name": category.name if category else None,
                "category_color": category.color if category else None,
                "category_icon": category.icon if category else None,
                "type": tx.type,
                "amount": _amount(tx),
                "payer_member_id": payer_of(tx.user_id),
                "status": _status(tx, today),
                "recurring": tx.recurring_transaction_id is not None,
                "shares": {},
            }
        item["shares"][split.group_member_id] = Decimal(str(split.share_amount or 0))

    paid_by: dict[uuid.UUID, Decimal] = {m: _ZERO for m in member_ids}
    share_of: dict[uuid.UUID, Decimal] = {m: _ZERO for m in member_ids}
    total = _ZERO
    for item in items.values():
        sign = Decimal("-1") if item["type"] == "credit" else Decimal("1")
        total += sign * item["amount"]
        if item["payer_member_id"] in paid_by:
            paid_by[item["payer_member_id"]] += sign * item["amount"]
        for member_id, share in item["shares"].items():
            share_of[member_id] = share_of.get(member_id, _ZERO) + sign * share

    # --- Shared recurring bills -------------------------------------------
    bills_rows = (
        await session.execute(
            select(RecurringTransaction).where(RecurringTransaction.split_group_id == group_id)
        )
    ).scalars().all()
    bills: list[dict] = []
    contracts: list[dict] = []
    for bill in bills_rows:
        intended_day = bill.day_of_month or bill.start_date.day
        dates = get_occurrences_in_range(
            bill.start_date, bill.frequency, bill.end_date, first,
            date.fromordinal(last.toordinal() + 1),
            intended_day=intended_day, weekend_adjustment=bill.weekend_adjustment,
        )
        # An ended or paused bill still shows in the months it ran.
        if dates and (bill.is_active or dates[0] < bill.next_occurrence):
            linked = (
                await session.execute(
                    select(Transaction).where(
                        Transaction.recurring_transaction_id == bill.id,
                        Transaction.date >= first,
                        Transaction.date <= last,
                        Transaction.is_ignored.is_(False),
                    )
                )
            ).scalars().all()
            paid = next((t for t in linked if t.source != "recurring"), None)
            total_count = bill.occurrences_total
            offset = bill.installment_offset or 0
            for due in dates:
                number = bill._count_occurrences(through=due) + offset
                bills.append(
                    {
                        "recurring_id": bill.id,
                        "description": bill.description,
                        "due_date": due,
                        "amount": _amount(paid) if paid else Decimal(str(bill.amount)),
                        "payer_member_id": payer_of(bill.user_id),
                        "paid": paid is not None,
                        "paid_date": paid.date if paid else None,
                        "number": number,
                        "total": total_count,
                    }
                )
        if bill.end_date is not None and bill.is_active:
            total_count = bill.occurrences_total or 0
            done = (bill.installment_offset or 0) + (
                bill._count_occurrences(through=today) if bill.start_date <= today else 0
            )
            contracts.append(
                {
                    "recurring_id": bill.id,
                    "description": bill.description,
                    "amount": Decimal(str(bill.amount)),
                    "frequency": bill.frequency,
                    "payer_member_id": payer_of(bill.user_id),
                    "done": min(done, total_count),
                    "total": total_count,
                    "remaining_amount": Decimal(str(bill.amount)) * max(total_count - done, 0),
                    "end_date": bill.end_date,
                    "next_date": bill.next_occurrence,
                }
            )
    bills.sort(key=lambda b: (b["due_date"], b["description"]))
    contracts.sort(key=lambda c: c["end_date"])

    self_member_id = balances["self_member_id"]
    return {
        "group_id": group_id,
        "month": first.strftime("%Y-%m"),
        "default_currency": group.default_currency,
        "self_member_id": self_member_id,
        "members": [{"id": m.id, "name": m.name} for m in members],
        "balance_lines": balances["lines"],
        "upcoming_lines": balances["upcoming_lines"],
        "total": total,
        "paid_by": [{"member_id": m, "amount": a} for m, a in paid_by.items()],
        "share_of": [{"member_id": m, "amount": a} for m, a in share_of.items()],
        "items": [
            {**{k: v for k, v in item.items() if k != "shares"},
             "my_share": item["shares"].get(self_member_id) if self_member_id else None}
            for item in sorted(items.values(), key=lambda i: (i["date"], i["description"]))
        ],
        "bills": bills,
        "contracts": contracts,
    }
