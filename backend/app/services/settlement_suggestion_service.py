"""Transfers between group members that look like settlements.

Paying a member back is a bank transfer (a PIX), which reaches Securo through
bank sync like any other movement. Nothing marks it as paying the group, and
not every transfer between the same two people does (a monthly allowance, a
loan), so it is never recorded on its own: it is offered to the payer, who
records it or says it is not one.

A transfer is offered when:

- the caller made it (a debit on their accounts) within the last
  `LOOKBACK_DAYS` and after the last settlement the caller recorded to that
  member (what came before it was settled by it, or was not a settlement),
  and it is not ignored, split, a recurring bill's charge,
  nor already the record of a settlement;
- the member it went to is a Securo user whose accounts show the same amount
  arriving within `PAIR_DAYS` (bank dates drift across midnight, and banks
  report a PIX on either side of it) — that credit is what tells a transfer
  to *them* apart from any other payment of that amount;
- the caller owes that member at least the amount (give or take
  `TOLERANCE`): a transfer larger than the debt is something else;
- the caller has not dismissed it for this group.

Only the receiver's credit id is returned from their side, never its
description or account: the caller learns no more than that the money
arrived.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.group_settlement import GroupSettlement
from app.models.group_settlement_dismissal import GroupSettlementDismissal
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit

LOOKBACK_DAYS = 45
PAIR_DAYS = 3
# Banks and people round: a transfer within a real of the debt pays it off.
TOLERANCE = Decimal("1.00")


def _settled(column):
    return exists().where(
        or_(
            GroupSettlement.transaction_id == column,
            GroupSettlement.receiver_transaction_id == column,
        )
    )


async def list_suggestions(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[list[dict]]:
    from app.core.app_clock import get_workspace_timezone, today_in
    from app.services import balance_service

    balances = await balance_service.compute_balances(session, group_id, workspace_id, user_id)
    if balances is None:
        return None
    self_member_id = balances["self_member_id"]
    if self_member_id is None:
        return []

    # What the caller owes each member now, per currency.
    owed: dict[tuple[uuid.UUID, str], Decimal] = {
        (line["member_id"], line["currency"]): -Decimal(str(line["amount"]))
        for line in balances["lines"]
        if line["amount"] < 0
    }
    if not owed:
        return []

    members = {
        m.id: m
        for m in (
            await session.execute(select(GroupMember).where(GroupMember.group_id == group_id))
        ).scalars()
    }
    today = today_in(await get_workspace_timezone(session, workspace_id))
    since = today - timedelta(days=LOOKBACK_DAYS)
    last_settled = dict(
        (
            await session.execute(
                select(GroupSettlement.to_member_id, func.max(GroupSettlement.date))
                .where(
                    GroupSettlement.group_id == group_id,
                    GroupSettlement.from_member_id == self_member_id,
                )
                .group_by(GroupSettlement.to_member_id)
            )
        ).all()
    )

    dismissed = exists().where(
        GroupSettlementDismissal.group_id == group_id,
        GroupSettlementDismissal.transaction_id == Transaction.id,
    )
    split = exists().where(TransactionSplit.transaction_id == Transaction.id)
    debits = (
        await session.execute(
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.type == "debit",
                Transaction.date >= since,
                Transaction.is_ignored.is_(False),
                Transaction.recurring_transaction_id.is_(None),
                Transaction.source != "settlement",
                ~split,
                ~dismissed,
                ~_settled(Transaction.id),
            )
            .order_by(Transaction.date.desc(), Transaction.id)
        )
    ).scalars().all()

    suggestions: list[dict] = []
    claimed: set[uuid.UUID] = set()
    for debit in debits:
        amount = Decimal(str(debit.amount)).copy_abs()
        for (member_id, currency), debt in owed.items():
            member = members.get(member_id)
            if member is None or member.linked_user_id is None or currency != debit.currency:
                continue
            if amount > debt + TOLERANCE:
                continue
            settled_on = last_settled.get(member_id)
            if settled_on is not None and debit.date <= settled_on:
                continue
            credits = (
                await session.execute(
                    select(Transaction)
                    .where(
                        Transaction.user_id == member.linked_user_id,
                        Transaction.type == "credit",
                        Transaction.currency == currency,
                        Transaction.amount == debit.amount,
                        Transaction.date >= debit.date - timedelta(days=PAIR_DAYS),
                        Transaction.date <= debit.date + timedelta(days=PAIR_DAYS),
                        Transaction.is_ignored.is_(False),
                        ~_settled(Transaction.id),
                    )
                )
            ).scalars().all()
            credits = sorted(
                (c for c in credits if c.id not in claimed),
                key=lambda c: (abs((c.date - debit.date).days), c.date, c.id),
            )
            if not credits:
                continue
            claimed.add(credits[0].id)
            suggestions.append(
                {
                    "transaction_id": debit.id,
                    "receiver_transaction_id": credits[0].id,
                    "from_member_id": self_member_id,
                    "to_member_id": member_id,
                    "amount": amount,
                    "currency": currency,
                    "date": debit.date,
                    "owed": debt,
                    "matches_balance": abs(amount - debt) <= TOLERANCE,
                }
            )
            break
    return suggestions


async def dismiss(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    transaction_id: uuid.UUID,
) -> bool:
    """Stop offering a transfer of the caller's for this group."""
    from app.services.group_service import get_group_visible

    if not await get_group_visible(session, group_id, workspace_id, user_id):
        return False
    tx = await session.get(Transaction, transaction_id)
    if tx is None or tx.user_id != user_id:
        return False
    already = await session.execute(
        select(GroupSettlementDismissal.id).where(
            GroupSettlementDismissal.group_id == group_id,
            GroupSettlementDismissal.transaction_id == transaction_id,
        )
    )
    if already.first() is None:
        session.add(GroupSettlementDismissal(group_id=group_id, transaction_id=transaction_id))
        await session.commit()
    return True
