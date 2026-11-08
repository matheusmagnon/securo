"""Sharing every later parcel of an installment purchase with a group."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.installment_split_rule import InstallmentSplitRule
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import balance_service, group_service, installment_split_service

PURCHASE = date(2026, 9, 15)


async def _account(session: AsyncSession, user_id) -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name="Card", type="credit_card",
        balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    await session.flush()
    return account


async def _parcel(
    session, user_id, account_id, number, total=4, amount="100.00",
    description=None, when=None, purchase=PURCHASE,
) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        account_id=account_id,
        description=description or f"AUTOMATICAR PARC {number:02d}/{total:02d} PALMAS BR",
        amount=Decimal(amount),
        currency="BRL",
        date=when or (purchase + timedelta(days=30 * (number - 1))),
        type="debit",
        source="sync",
        installment_number=number,
        total_installments=total,
        installment_purchase_date=purchase,
    )
    session.add(tx)
    await session.flush()
    return tx


async def _group(session, test_user, workspace_id):
    group = await group_service.create_group(session, workspace_id, test_user.id, GroupCreate(name="Casa"))
    me = await group_service.create_member(
        session, group.id, workspace_id, GroupMemberCreate(name="Eu", is_self=True)
    )
    her = await group_service.create_member(session, group.id, workspace_id, GroupMemberCreate(name="Ela"))
    return group, me, her


def _equal(*members) -> TransactionSplitsInput:
    return TransactionSplitsInput(
        share_type="equal", splits=[TransactionSplitInput(group_member_id=m.id) for m in members]
    )


async def _shares(session, tx) -> dict:
    rows = (await session.execute(
        select(TransactionSplit).where(TransactionSplit.transaction_id == tx.id)
    )).scalars().all()
    return {r.group_member_id: r.share_amount for r in rows}


@pytest.mark.parametrize(
    "description,total,expected",
    [
        ("AUTOMATICAR   PARC 03/04 PALMAS      BR", 4, "AUTOMATICAR PALMAS BR"),
        ("Automaticar 2/3", 3, "AUTOMATICAR"),
        ("Hospital Veterinario S 10/10", 10, "HOSPITAL VETERINARIO S"),
        ("GLOBO*Globopl PARC 07/12 RIO DE JANEIBR", 12, "GLOBO*GLOBOPL RIO DE JANEIBR"),
        # Only the n/total marker goes; other numbers stay.
        ("LOJA 08/11 PARC 01/03", 3, "LOJA 08/11"),
    ],
)
def test_description_key(description, total, expected):
    assert installment_split_service.description_key(description, total) == expected


@pytest.mark.asyncio
async def test_split_applies_to_this_and_later_parcels_only(session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    p1 = await _parcel(session, test_user.id, card.id, 1)
    p2 = await _parcel(session, test_user.id, card.id, 2)
    p3 = await _parcel(session, test_user.id, card.id, 3)
    p4 = await _parcel(session, test_user.id, card.id, 4)

    changed = await installment_split_service.set_series_split(session, p2, _equal(me, her), test_user.id)
    await session.commit()

    assert changed == 3
    assert await _shares(session, p1) == {}
    for p in (p2, p3, p4):
        assert await _shares(session, p) == {me.id: Decimal("50.00"), her.id: Decimal("50.00")}


@pytest.mark.asyncio
async def test_other_purchase_same_day_untouched(session, test_user, test_workspace):
    """Same card, date and parcel count, different store: not the same purchase."""
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    mine = await _parcel(session, test_user.id, card.id, 1)
    other = await _parcel(session, test_user.id, card.id, 2, description="FARMACIA PARC 02/04")

    await installment_split_service.set_series_split(session, mine, _equal(me, her), test_user.id)
    await session.commit()
    assert await _shares(session, other) == {}


@pytest.mark.asyncio
async def test_sync_applies_rule_to_new_parcel(session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    p3 = await _parcel(session, test_user.id, card.id, 3, amount="674.62")
    await installment_split_service.set_series_split(session, p3, _equal(me, her), test_user.id)
    await session.commit()

    # Next month the bank reports parcel 4.
    p4 = await _parcel(session, test_user.id, card.id, 4, amount="674.62")
    applied = await installment_split_service.apply_rules(session, [p4])
    await session.commit()

    assert applied == 1
    assert await _shares(session, p4) == {me.id: Decimal("337.31"), her.id: Decimal("337.31")}


@pytest.mark.asyncio
async def test_sync_leaves_already_split_and_earlier_parcels(session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    p2 = await _parcel(session, test_user.id, card.id, 2)
    await installment_split_service.set_series_split(session, p2, _equal(me, her), test_user.id)
    await session.commit()

    earlier = await _parcel(session, test_user.id, card.id, 1)  # imported late
    assert await installment_split_service.apply_rules(session, [earlier]) == 0
    assert await _shares(session, earlier) == {}


@pytest.mark.asyncio
async def test_exact_amounts_become_percentages(session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    p1 = await _parcel(session, test_user.id, card.id, 1, amount="100.00")
    p2 = await _parcel(session, test_user.id, card.id, 2, amount="99.99")
    payload = TransactionSplitsInput(
        share_type="exact",
        splits=[
            TransactionSplitInput(group_member_id=me.id, share_amount=Decimal("30.00")),
            TransactionSplitInput(group_member_id=her.id, share_amount=Decimal("70.00")),
        ],
    )
    await installment_split_service.set_series_split(session, p1, payload, test_user.id)
    await session.commit()

    rule = (await session.execute(select(InstallmentSplitRule))).scalar_one()
    assert rule.share_type == "percent"
    assert await _shares(session, p1) == {me.id: Decimal("30.00"), her.id: Decimal("70.00")}
    assert await _shares(session, p2) == {me.id: Decimal("30.00"), her.id: Decimal("69.99")}


@pytest.mark.asyncio
async def test_empty_split_stops_sharing(session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    p1 = await _parcel(session, test_user.id, card.id, 1)
    p2 = await _parcel(session, test_user.id, card.id, 2)
    await installment_split_service.set_series_split(session, p1, _equal(me, her), test_user.id)
    await session.commit()

    await installment_split_service.set_series_split(
        session, p1, TransactionSplitsInput(share_type="equal", splits=[]), test_user.id
    )
    await session.commit()

    assert (await session.execute(select(InstallmentSplitRule))).first() is None
    assert await _shares(session, p2) == {}
    p3 = await _parcel(session, test_user.id, card.id, 3)
    assert await installment_split_service.apply_rules(session, [p3]) == 0


@pytest.mark.asyncio
async def test_not_an_installment_rejected(session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    tx = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, account_id=card.id, description="MERCADO",
        amount=Decimal("10"), currency="BRL", date=date.today(), type="debit", source="sync",
    )
    session.add(tx)
    await session.flush()
    with pytest.raises(ValueError):
        await installment_split_service.set_series_split(session, tx, _equal(me, her), test_user.id)


@pytest.mark.asyncio
async def test_balance_counts_due_parcels_and_lists_upcoming(session, test_user, test_workspace):
    """Parcels dated after today are upcoming, not owed yet."""
    group, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    today = date.today()
    due = await _parcel(session, test_user.id, card.id, 1, total=3, when=today - timedelta(days=1),
                        purchase=today - timedelta(days=1))
    await _parcel(session, test_user.id, card.id, 2, total=3, when=today + timedelta(days=30),
                  purchase=today - timedelta(days=1))
    await _parcel(session, test_user.id, card.id, 3, total=3, when=today + timedelta(days=60),
                  purchase=today - timedelta(days=1))

    await installment_split_service.set_series_split(session, due, _equal(me, her), test_user.id)
    await session.commit()

    balances = await balance_service.compute_balances(session, group.id, test_workspace.id, test_user.id)
    owed = {ln["member_id"]: ln["amount"] for ln in balances["lines"]}
    upcoming = {ln["member_id"]: ln["amount"] for ln in balances["upcoming_lines"]}
    assert owed == {her.id: Decimal("50.00")}
    assert upcoming == {her.id: Decimal("100.00")}


@pytest.mark.asyncio
async def test_update_api_split_installments(client, auth_headers, session, test_user, test_workspace):
    _, me, her = await _group(session, test_user, test_workspace.id)
    card = await _account(session, test_user.id)
    p1 = await _parcel(session, test_user.id, card.id, 1)
    p2 = await _parcel(session, test_user.id, card.id, 2)
    await session.commit()
    p2_id = p2.id

    body = {
        "splits": {
            "share_type": "equal",
            "splits": [{"group_member_id": str(me.id)}, {"group_member_id": str(her.id)}],
        },
        "split_installments": True,
    }
    resp = await client.patch(f"/api/transactions/{p1.id}", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    rows = (await session.execute(
        select(TransactionSplit.share_amount).where(TransactionSplit.transaction_id == p2_id)
    )).scalars().all()
    assert sorted(rows) == [Decimal("50.00"), Decimal("50.00")]
