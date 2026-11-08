"""Share every later parcel of an installment purchase with a group.

`set_series_split` runs when someone splits one parcel and asks for "this
and the next installments": the parcels from that one on that already
exist get the split, and an `InstallmentSplitRule` remembers it so
`apply_rules` can split the parcels that arrive with later syncs.

Earlier parcels are deliberately left alone: whatever was settled for them
before the rule existed must not reappear in the group balance.
"""
from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.installment_split_rule import InstallmentSplitRule
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import split_service

logger = logging.getLogger(__name__)

_HUNDRED = Decimal("100")
_CENT = Decimal("0.01")


def is_installment(tx: Transaction) -> bool:
    return (
        tx.installment_number is not None
        and tx.total_installments is not None
        and tx.total_installments > 1
        and tx.installment_purchase_date is not None
    )


def description_key(description: Optional[str], total_installments: int) -> str:
    """The description without its parcel marker, normalised.

    "AUTOMATICAR   PARC 03/04 PALMAS  BR" -> "AUTOMATICAR PALMAS BR"
    "Automaticar 2/3"                     -> "AUTOMATICAR"

    Only an "n/total" marker is removed, so other numbers in the text
    (a store number, a date) stay part of the key.
    """
    text = description or ""
    marker = rf"(?:\bPARC(?:ELA)?\.?\s*)?\b0*\d{{1,3}}\s*/\s*0*{int(total_installments)}\b"
    text = re.sub(marker, " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().upper()[:255]


def _key_of(tx: Transaction) -> str:
    return description_key(tx.original_description or tx.description, tx.total_installments)


async def _series(
    session: AsyncSession, tx: Transaction, from_number: int
) -> list[Transaction]:
    """Parcels of `tx`'s purchase numbered `from_number` or later."""
    result = await session.execute(
        select(Transaction)
        .where(
            Transaction.workspace_id == tx.workspace_id,
            Transaction.account_id == tx.account_id,
            Transaction.installment_purchase_date == tx.installment_purchase_date,
            Transaction.total_installments == tx.total_installments,
            Transaction.installment_number >= from_number,
        )
        .order_by(Transaction.installment_number)
    )
    key = _key_of(tx)
    return [row for row in result.scalars().all() if _key_of(row) == key]


def _rule_payload(tx: Transaction, payload: TransactionSplitsInput) -> tuple[str, list[dict]]:
    """Turn a split into something that fits every parcel.

    Parcels of one purchase can differ by a cent, so exact amounts become
    percentages of the parcel they were entered on.
    """
    if payload.share_type == "equal":
        return "equal", [{"group_member_id": str(s.group_member_id), "share_pct": None} for s in payload.splits]
    if payload.share_type == "percent":
        return "percent", [
            {"group_member_id": str(s.group_member_id), "share_pct": str(s.share_pct)} for s in payload.splits
        ]
    total = Decimal(str(tx.amount)).copy_abs()
    shares: list[dict] = []
    running = Decimal("0")
    for i, s in enumerate(payload.splits):
        if i == len(payload.splits) - 1:
            pct = _HUNDRED - running
        else:
            pct = (Decimal(str(s.share_amount or 0)) / total * _HUNDRED).quantize(_CENT) if total else Decimal("0")
            running += pct
        shares.append({"group_member_id": str(s.group_member_id), "share_pct": str(pct)})
    return "percent", shares


def _payload_from_rule(rule: InstallmentSplitRule) -> TransactionSplitsInput:
    return TransactionSplitsInput(
        share_type=rule.share_type,
        splits=[
            TransactionSplitInput(
                group_member_id=uuid.UUID(s["group_member_id"]),
                share_pct=Decimal(s["share_pct"]) if s.get("share_pct") is not None else None,
            )
            for s in rule.shares
        ],
    )


async def find_rule(session: AsyncSession, tx: Transaction) -> Optional[InstallmentSplitRule]:
    if not is_installment(tx):
        return None
    result = await session.execute(
        select(InstallmentSplitRule).where(
            InstallmentSplitRule.account_id == tx.account_id,
            InstallmentSplitRule.purchase_date == tx.installment_purchase_date,
            InstallmentSplitRule.total_installments == tx.total_installments,
            InstallmentSplitRule.description_key == _key_of(tx),
        )
    )
    return result.scalar_one_or_none()


async def set_series_split(
    session: AsyncSession,
    tx: Transaction,
    payload: TransactionSplitsInput,
    user_id: uuid.UUID,
) -> int:
    """Split `tx` and every later parcel of its purchase the same way.

    An empty payload stops sharing the purchase: the rule goes away and the
    later parcels lose their split too. Returns how many parcels changed.
    """
    if not is_installment(tx):
        raise ValueError("This transaction is not an installment")

    rule = await find_rule(session, tx)
    parcels = await _series(session, tx, tx.installment_number)

    if not payload.splits:
        if rule is not None:
            await session.delete(rule)
        for row in parcels:
            await split_service.replace_splits(session, row, payload, user_id)
        return len(parcels)

    share_type, shares = _rule_payload(tx, payload)
    member = await session.get(GroupMember, uuid.UUID(shares[0]["group_member_id"]))
    if member is None:
        raise ValueError("One or more split members not found")

    if rule is None:
        rule = InstallmentSplitRule(
            workspace_id=tx.workspace_id,
            account_id=tx.account_id,
            purchase_date=tx.installment_purchase_date,
            total_installments=tx.total_installments,
            description_key=_key_of(tx),
        )
        session.add(rule)
    rule.user_id = user_id
    rule.group_id = member.group_id
    rule.from_installment = tx.installment_number
    rule.share_type = share_type
    rule.shares = shares

    series_payload = _payload_from_rule(rule)
    changed = 0
    for row in parcels:
        if row.has_category_lines:
            continue  # split across categories: can't also be split among people
        # The anchor keeps exactly what was entered (exact amounts included).
        await split_service.replace_splits(
            session, row, payload if row.id == tx.id else series_payload, user_id
        )
        changed += 1
    await session.flush()
    return changed


async def apply_rules(session: AsyncSession, transactions: Iterable[Transaction]) -> int:
    """Split newly imported parcels of purchases someone shares.

    Parcels that already carry a split, or are split across categories, are
    left as they are. A rule that no longer fits (a member was removed) is
    logged and skipped rather than failing the sync.
    """
    applied = 0
    for tx in transactions:
        if not is_installment(tx) or tx.has_category_lines:
            continue
        rule = await find_rule(session, tx)
        if rule is None or tx.installment_number < rule.from_installment:
            continue
        existing = await session.execute(
            select(TransactionSplit.id).where(TransactionSplit.transaction_id == tx.id).limit(1)
        )
        if existing.first() is not None:
            continue
        try:
            async with session.begin_nested():
                await split_service.replace_splits(session, tx, _payload_from_rule(rule), rule.user_id)
            applied += 1
        except ValueError:
            logger.warning(
                "Installment split rule %s no longer applies to transaction %s", rule.id, tx.id,
                exc_info=True,
            )
    return applied
