"""Recurring bills shared with a group.

A recurring bill (an installment contract, the internet) can name a group:
every occurrence it generates starts split equally among that group's
members. When the real charge arrives and replaces the generated placeholder,
the split stays on the same row, resized if the amount changed.

A placeholder that reached the merge without a split (its bill names no
group) takes the split a "split with group" rule gives the incoming charge:
rules are evaluated on the incoming charge (the `preview`), never on the
placeholder, whose description is the bill's own wording.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import split_service

logger = logging.getLogger(__name__)

_HUNDRED = Decimal("100")
_CENT = Decimal("0.01")

# Set on a detached rule preview when a "split with group" rule matched it:
# (user the rule acts for, group id). The preview has no row to split yet.
RULE_SPLIT_ATTR = "rule_split_group"


async def has_splits(session: AsyncSession, tx: Transaction) -> bool:
    found = await session.execute(
        select(TransactionSplit.id).where(TransactionSplit.transaction_id == tx.id).limit(1)
    )
    return found.first() is not None


async def split_equally(
    session: AsyncSession, tx: Transaction, group_value, user_id: uuid.UUID
) -> bool:
    """Split `tx` equally among a group's members. Returns whether it did.

    A row not written yet, already split (among people or across
    categories), or a transfer is left alone, and so is a group that can no
    longer be used: an automatic split must never fail the sync, import or
    scheduled run that triggered it.
    """
    if tx.id is None or tx.has_category_lines or tx.transfer_pair_id:
        return False
    if await has_splits(session, tx):
        return False
    try:
        group_id = uuid.UUID(str(group_value))
    except (TypeError, ValueError):
        return False
    member_ids = (
        await session.execute(
            select(GroupMember.id).where(GroupMember.group_id == group_id).order_by(GroupMember.created_at)
        )
    ).scalars().all()
    if len(member_ids) < 2:
        return False
    payload = TransactionSplitsInput(
        share_type="equal",
        splits=[TransactionSplitInput(group_member_id=m) for m in member_ids],
    )
    try:
        async with session.begin_nested():
            await split_service.replace_splits(session, tx, payload, user_id)
    except ValueError:
        logger.warning(
            "Could not split transaction %s into group %s", tx.id, group_id, exc_info=True
        )
        return False
    return True


async def split_occurrence(
    session: AsyncSession, tx: Transaction, recurring: Optional[RecurringTransaction]
) -> bool:
    """Give an occurrence of a shared bill its group split."""
    if recurring is None or recurring.split_group_id is None:
        return False
    return await split_equally(session, tx, recurring.split_group_id, recurring.user_id)


async def resize_splits(
    session: AsyncSession, tx: Transaction, old_amount: Decimal, user_id: uuid.UUID
) -> None:
    """Recompute a split after the row's amount changed.

    Equal and percentage splits keep their shape; exact amounts become the
    percentages they were of the old amount.
    """
    rows = (
        await session.execute(
            select(TransactionSplit)
            .where(TransactionSplit.transaction_id == tx.id)
            .order_by(TransactionSplit.created_at, TransactionSplit.id)
        )
    ).scalars().all()
    if not rows:
        return
    share_type = rows[0].share_type
    if share_type == "equal":
        payload = TransactionSplitsInput(
            share_type="equal",
            splits=[TransactionSplitInput(group_member_id=r.group_member_id) for r in rows],
        )
    else:
        old_total = Decimal(str(old_amount)).copy_abs()
        splits: list[TransactionSplitInput] = []
        running = Decimal("0")
        for i, r in enumerate(rows):
            if i == len(rows) - 1:
                pct = _HUNDRED - running
            elif share_type == "percent" and r.share_pct is not None:
                pct = Decimal(str(r.share_pct))
                running += pct
            else:
                pct = (
                    (Decimal(str(r.share_amount)) / old_total * _HUNDRED).quantize(_CENT)
                    if old_total else Decimal("0")
                )
                running += pct
            splits.append(TransactionSplitInput(group_member_id=r.group_member_id, share_pct=pct))
        payload = TransactionSplitsInput(share_type="percent", splits=splits)
    try:
        async with session.begin_nested():
            await split_service.replace_splits(session, tx, payload, user_id)
    except ValueError:
        logger.warning("Could not resize the split of transaction %s", tx.id, exc_info=True)


async def after_placeholder_merge(
    session: AsyncSession,
    placeholder: Transaction,
    preview: Transaction,
    previous_amount: Decimal,
) -> None:
    """Settle a placeholder a real charge just replaced.

    A bill paid any day of its month takes the charge's date: it was paid
    then, and the group must count it as paid, not as still upcoming until
    the due date. Then the group split is kept right.
    """
    recurring = (
        await session.get(RecurringTransaction, placeholder.recurring_transaction_id)
        if placeholder.recurring_transaction_id
        else None
    )
    if recurring is not None and recurring.match_whole_month and preview.date:
        placeholder.date = preview.date
    await session.flush()
    if await has_splits(session, placeholder):
        if placeholder.amount != previous_amount:
            await resize_splits(session, placeholder, previous_amount, placeholder.user_id)
        return
    if await split_occurrence(session, placeholder, recurring):
        return
    rule_split = getattr(preview, RULE_SPLIT_ATTR, None)
    if rule_split is not None:
        user_id, group_value = rule_split
        await split_equally(session, placeholder, group_value, user_id)
