"""Bills paid any day of their month.

The apartment down payment: R$ 578,95 due on the 30th, usually paid around
the 15th, from a synced account whose bank calls it "Transferências" (a rule
renames it). A charge of the bill's month answers the occurrence wherever in
the month it lands; outside the month the usual window still applies.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.schemas.recurring_transaction import RecurringTransactionCreate, RecurringTransactionUpdate
from app.schemas.rule import RuleCreate
from app.services.recurring_transaction_service import (
    create_recurring_transaction,
    generate_pending,
    update_recurring_transaction,
)
from app.services.rule_service import create_rule
from tests.test_recurring_group_split import _group, _shares
from tests import test_recurring_match_sync as _match_sync
from tests.test_recurring_match_sync import _all_txs, _provider, _run_sync, _tx

conn_account = _match_sync.conn_account  # the fixture, reused

PARCEL = Decimal("578.95")
HALF_UP, HALF_DOWN = Decimal("289.47"), Decimal("289.48")


async def _bill(session, test_workspace, test_user, account, **ov):
    data = RecurringTransactionCreate(
        description="Entrada AP",
        amount=ov.pop("amount", PARCEL),
        currency="BRL",
        type="debit",
        frequency=ov.pop("frequency", "monthly"),
        start_date=ov.pop("start_date", date(2026, 11, 30)),
        account_id=account.id,
        **ov,
    )
    return await create_recurring_transaction(session, test_workspace.id, test_user.id, data)


async def _rename_rule(session, test_workspace, test_user):
    await create_rule(
        session, test_workspace.id, test_user.id,
        RuleCreate(
            name="Entrada AP",
            conditions_op="and",
            conditions=[{"field": "amount", "op": "equals", "value": str(-PARCEL)}],
            actions=[{"op": "set_description", "value": "Entrada AP"}],
        ),
    )


async def _sync(session, conn_account, test_workspace, test_user, when: date):
    conn_id, account_id = _ids[id(conn_account)]
    provider = _provider([_tx(external_id=f"pix-{when}", description="Transferências", amount=PARCEL, date=when)])
    await _run_sync(session, conn_id, test_workspace, test_user, provider, mock_rules=False)
    return await _all_txs(session, account_id)


_ids: dict = {}


def _remember(conn_account):
    conn, account = conn_account
    _ids[id(conn_account)] = (conn.id, account.id)
    return account


@pytest.mark.asyncio
async def test_mid_month_payment_replaces_the_month_start_placeholder(
    session, test_user, test_workspace, conn_account
):
    account = _remember(conn_account)
    group, _, _ = await _group(session, test_user, test_workspace.id)
    await _rename_rule(session, test_workspace, test_user)
    await _bill(
        session, test_workspace, test_user, account,
        generate_at_month_start=True, match_whole_month=True, split_group_id=group.id,
    )
    assert await generate_pending(session, test_user.id, up_to=date(2026, 11, 1)) == 1

    txs = await _sync(session, conn_account, test_workspace, test_user, date(2026, 11, 14))

    assert len(txs) == 1  # merged, not duplicated
    assert txs[0].source == "sync"
    assert txs[0].status == "posted"
    assert txs[0].date == date(2026, 11, 14)
    assert txs[0].description == "Entrada AP"
    assert await _shares(session, txs[0].id) == [HALF_UP, HALF_DOWN]


@pytest.mark.asyncio
async def test_without_the_option_a_mid_month_payment_stays_apart(
    session, test_user, test_workspace, conn_account
):
    account = _remember(conn_account)
    await _rename_rule(session, test_workspace, test_user)
    await _bill(session, test_workspace, test_user, account, generate_at_month_start=True)
    await generate_pending(session, test_user.id, up_to=date(2026, 11, 1))

    txs = await _sync(session, conn_account, test_workspace, test_user, date(2026, 11, 14))

    assert len(txs) == 2


@pytest.mark.asyncio
async def test_a_payment_of_another_month_is_not_taken(
    session, test_user, test_workspace, conn_account
):
    account = _remember(conn_account)
    await _rename_rule(session, test_workspace, test_user)
    await _bill(
        session, test_workspace, test_user, account,
        generate_at_month_start=True, match_whole_month=True,
    )
    await generate_pending(session, test_user.id, up_to=date(2026, 11, 1))

    # A late payment of October, made in October, never fills November.
    txs = await _sync(session, conn_account, test_workspace, test_user, date(2026, 10, 20))

    assert len(txs) == 2
    placeholder = next(t for t in txs if t.source == "recurring")
    assert placeholder.status == "pending"


@pytest.mark.asyncio
async def test_payment_before_the_placeholder_claims_the_bill(
    session, test_user, test_workspace, conn_account
):
    account = _remember(conn_account)
    await _rename_rule(session, test_workspace, test_user)
    bill = await _bill(session, test_workspace, test_user, account, match_whole_month=True)
    bill_id = bill.id

    txs = await _sync(session, conn_account, test_workspace, test_user, date(2026, 11, 14))

    assert len(txs) == 1
    assert txs[0].recurring_transaction_id == bill_id
    assert (await session.get(RecurringTransaction, bill_id)).next_occurrence == date(2026, 12, 30)
    assert await generate_pending(session, test_user.id, up_to=date(2026, 11, 30)) == 0


@pytest.mark.asyncio
async def test_generation_links_a_payment_already_in_the_month(
    session, test_user, test_workspace, conn_account
):
    _, account = conn_account
    paid = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, account_id=account.id,
        description="Entrada AP", amount=PARCEL, currency="BRL", date=date(2026, 11, 14),
        type="debit", source="sync", external_id="pix-early",
    )
    session.add(paid)
    await session.commit()
    paid_id = paid.id
    bill = await _bill(session, test_workspace, test_user, account, match_whole_month=True)
    bill_id = bill.id

    assert await generate_pending(session, test_user.id, up_to=date(2026, 11, 30)) == 0
    rows = (await session.execute(
        select(Transaction).where(Transaction.account_id == account.id)
    )).scalars().all()
    linked = [t for t in rows if t.recurring_transaction_id == bill_id]
    assert [t.id for t in linked] == [paid_id]


@pytest.mark.asyncio
async def test_installment_counter(session, test_user, test_workspace, conn_account):
    _, account = conn_account
    monthly = await _bill(
        session, test_workspace, test_user, account,
        end_date=date(2029, 7, 30), generate_at_month_start=True,
    )
    yearly = await _bill(
        session, test_workspace, test_user, account,
        amount=Decimal("3000"), frequency="yearly",
        start_date=date(2026, 12, 30), end_date=date(2028, 12, 30),
    )
    assert (monthly.occurrences_done, monthly.occurrences_total) == (0, 33)
    assert (yearly.occurrences_done, yearly.occurrences_total) == (0, 3)

    await generate_pending(session, test_user.id, up_to=date(2027, 2, 1))
    monthly = await session.get(RecurringTransaction, monthly.id)
    # Nov, Dec, Jan and Feb (on the 28th: a short month clamps).
    assert monthly.next_occurrence == date(2027, 3, 30)
    assert monthly.occurrences_done == 4
    yearly = await session.get(RecurringTransaction, yearly.id)
    assert yearly.occurrences_done == 1


@pytest.mark.asyncio
async def test_update_toggles_the_option(session, test_user, test_workspace, conn_account):
    _, account = conn_account
    bill = await _bill(session, test_workspace, test_user, account)
    assert bill.match_whole_month is False
    updated = await update_recurring_transaction(
        session, bill.id, test_workspace.id, RecurringTransactionUpdate(match_whole_month=True),
    )
    assert updated.match_whole_month is True
    kept = await update_recurring_transaction(
        session, bill.id, test_workspace.id, RecurringTransactionUpdate(description="Entrada AP 2"),
    )
    assert kept.match_whole_month is True
