"""What goes into a group's next settlement, item by item.

The balance says how much is owed; this says *which* shared expenses make
it up. An expense is open until a settlement lists it
(``GroupSettlementItem``). Each open expense gets a reference date, the day
it counts for settling, following the rules the household uses:

- card charge: the due date of the card bill it is on (closed bills from
  the bank; later charges by the card's cycle, estimated);
- recurring placeholder (``source == "recurring"``): a forecast, dated by
  its schedule, until the real payment replaces it;
- anything else: the day it was paid (a later date is still upcoming).

Items whose reference date falls on or before the next settlement date go
into the next settlement; the rest wait. The next settlement date repeats
the day of the month of the last settlement (10/11 -> 10/12); without one,
every open item counts as next.

Amounts are projected onto the caller like the balance: positive means the
other member owes the caller, negative that the caller owes them.
"""

from __future__ import annotations

import calendar
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.group_settlement import GroupSettlement
from app.models.group_settlement_item import GroupSettlementItem
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services.balance_service import payer_context
from app.services.card_cycle_service import load_cycles

_ZERO = Decimal("0")


def next_settlement_date(last: Optional[date], today: date) -> Optional[date]:
    """The first day after ``today`` (and after ``last``) with ``last``'s day of month."""
    if last is None:
        return None
    floor = max(last, today)
    year, month = floor.year, floor.month
    for _ in range(3):
        day = min(last.day, calendar.monthrange(year, month)[1])
        cand = date(year, month, day)
        if cand > floor:
            return cand
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None  # pragma: no cover


def _reference(tx: Transaction, cycles, today: date) -> dict[str, Any]:
    if tx.source == "recurring":
        return {"kind": "planned", "date": tx.date}
    cyc = cycles.get(tx.account_id)
    if cyc is not None:
        label = cyc.label(tx)
        due = label.get("due_date")
        if due:
            return {
                "kind": "card_bill",
                "date": date.fromisoformat(due),
                "bill_state": label.get("bill"),
                "estimated": bool(label.get("estimated")),
            }
        return {"kind": "card", "date": tx.date}
    if tx.date > today:
        return {"kind": "upcoming", "date": tx.date}
    return {"kind": "paid", "date": tx.date}


async def compute_next_settlement(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[dict]:
    from app.core.app_clock import get_workspace_timezone, today_in
    from app.services.group_service import get_group_visible

    group = await get_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return None
    members, member_by_user, default_payer, self_member = await payer_context(
        session, group, user_id
    )
    names = {m.id: m.name for m in members}
    caller = self_member.id if self_member is not None else None
    today = today_in(await get_workspace_timezone(session, group.workspace_id))

    settlements = (
        await session.execute(
            select(GroupSettlement)
            .where(GroupSettlement.group_id == group_id)
            .order_by(GroupSettlement.date.desc(), GroupSettlement.created_at.desc())
        )
    ).scalars().all()
    item_rows = (
        await session.execute(
            select(GroupSettlementItem.settlement_id, GroupSettlementItem.transaction_id).where(
                GroupSettlementItem.group_id == group_id
            )
        )
    ).all()
    settled_tx = {tx_id for _, tx_id in item_rows}
    items_per_settlement: dict[uuid.UUID, int] = defaultdict(int)
    for sid, _ in item_rows:
        items_per_settlement[sid] += 1

    last_date = settlements[0].date if settlements else None
    next_date = next_settlement_date(last_date, today)

    rows = (
        await session.execute(
            select(TransactionSplit, Transaction)
            .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
            .join(GroupMember, TransactionSplit.group_member_id == GroupMember.id)
            .where(GroupMember.group_id == group_id, Transaction.is_ignored.is_(False))
        )
    ).all()
    by_tx: dict[uuid.UUID, tuple[Transaction, list[TransactionSplit]]] = {}
    for split, tx in rows:
        if tx.id in settled_tx:
            continue
        by_tx.setdefault(tx.id, (tx, []))[1].append(split)

    # Only credit-card accounts come back (load_cycles filters them).
    cycles = await load_cycles(session, {tx.account_id for tx, _ in by_tx.values()})

    items: list[dict[str, Any]] = []
    for tx, splits in by_tx.values():
        payer = member_by_user.get(tx.user_id, default_payer)
        sign = -1 if tx.type == "credit" else 1
        # member -> what they owe the payer for this transaction
        owes = {
            s.group_member_id: Decimal(str(s.share_amount))
            for s in splits
            if s.group_member_id != payer
        }
        # Caller's view, per counterpart: + they owe you, - you owe them.
        net: dict[uuid.UUID, Decimal] = {}
        if caller is not None:
            if payer == caller:
                net = {m: sign * a for m, a in owes.items()}
            elif caller in owes:
                net = {payer: -sign * owes[caller]}
        ref = _reference(tx, cycles, today)
        in_next = next_date is None or ref["date"] <= next_date
        items.append(
            {
                "transaction_id": tx.id,
                "date": tx.date,
                "description": tx.description,
                "amount": Decimal(str(tx.amount)),
                "currency": tx.currency,
                "type": tx.type,
                "installment": (
                    f"{tx.installment_number}/{tx.total_installments}"
                    if tx.installment_number and tx.total_installments
                    else None
                ),
                "payer_member_id": payer if payer in names else None,
                "payer_name": names.get(payer),
                "shares": [
                    {"member_id": s.group_member_id, "name": names.get(s.group_member_id), "amount": Decimal(str(s.share_amount))}
                    for s in splits
                ],
                "reference_kind": ref["kind"],
                "reference_date": ref["date"],
                "bill_state": ref.get("bill_state"),
                "estimated": ref.get("estimated", False),
                "confirmed": ref["kind"] not in ("planned", "upcoming"),
                "bucket": "next" if in_next else "later",
                "you_net": sum(net.values(), _ZERO),
                "net_by_member": [{"member_id": m, "amount": a} for m, a in net.items()],
            }
        )
    items.sort(key=lambda i: (i["reference_date"], i["date"], i["description"]))

    def total(bucket: str, confirmed_only: bool) -> Decimal:
        return sum(
            (
                i["you_net"]
                for i in items
                if i["bucket"] == bucket and (i["confirmed"] or not confirmed_only)
            ),
            _ZERO,
        )

    return {
        "group_id": group_id,
        "self_member_id": caller,
        "currency": group.default_currency,
        "today": today,
        "last_settlement_date": last_date,
        "next_settlement_date": next_date,
        "items": items,
        "next_confirmed_total": total("next", True),
        "next_total_with_planned": total("next", False),
        "later_total": total("later", False),
        "settlements": [
            {
                "id": s.id,
                "date": s.date,
                "amount": Decimal(str(s.amount)),
                "from_member_id": s.from_member_id,
                "to_member_id": s.to_member_id,
                "item_count": items_per_settlement.get(s.id, 0),
            }
            for s in settlements
        ],
    }
