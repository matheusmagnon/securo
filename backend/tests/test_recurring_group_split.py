"""Recurring bills shared with a group, generated at the start of the month.

The farm contract: R$ 897,44 due on the 10th, paid from a synced account
whose bank describes it only as "Boletos". The bill is generated on the 1st
(already split 50/50, so it shows as upcoming in the group), and the synced
charge, renamed by a rule, replaces it keeping the split.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.recurring_transaction import RecurringTransactionCreate, RecurringTransactionUpdate
from app.schemas.rule import RuleCreate
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import group_service, recurring_split_service, split_service
from app.services.recurring_transaction_service import (
    create_recurring_transaction,
    generate_pending,
    update_recurring_transaction,
)
from app.services.rule_service import create_rule
from tests import test_recurring_match_sync as _match_sync
from tests.test_recurring_match_sync import _all_txs, _provider, _run_sync, _tx

conn_account = _match_sync.conn_account  # the fixture, reused

PARCEL = Decimal("897.44")
HALF = Decimal("448.72")


async def _group(session, test_user, workspace_id):
    group = await group_service.create_group(session, workspace_id, test_user.id, GroupCreate(name="Casa"))
    me = await group_service.create_member(
        session, group.id, workspace_id, GroupMemberCreate(name="Eu", is_self=True)
    )
    her = await group_service.create_member(session, group.id, workspace_id, GroupMemberCreate(name="Ela"))
    await session.commit()
    return group, me, her


async def _bill(session, test_workspace, test_user, account, **ov):
    data = RecurringTransactionCreate(
        description=ov.pop("description", "Chácara 22"),
        amount=ov.pop("amount", PARCEL),
        currency="BRL",
        type="debit",
        frequency="monthly",
        start_date=ov.pop("start_date", date(2026, 12, 10)),
        account_id=account.id,
        **ov,
    )
    return await create_recurring_transaction(session, test_workspace.id, test_user.id, data)


async def _shares(session, tx_id) -> list[Decimal]:
    rows = await session.execute(
        select(TransactionSplit.share_amount).where(TransactionSplit.transaction_id == tx_id)
    )
    return sorted(rows.scalars().all())


async def _rename_rule(session, test_workspace, test_user, extra_actions=()):
    return await create_rule(
        session, test_workspace.id, test_user.id,
        RuleCreate(
            name="Chácara",
            conditions_op="and",
            conditions=[{"field": "amount", "op": "equals", "value": str(-PARCEL)}],
            actions=[{"op": "set_description", "value": "Chácara 22"}, *extra_actions],
        ),
    )


@pytest.mark.asyncio
async def test_month_start_bill_is_generated_on_the_first_already_split(
    session, test_user, test_workspace, conn_account
):
    _, account = conn_account
    group, _, _ = await _group(session, test_user, test_workspace.id)
    bill = await _bill(
        session, test_workspace, test_user, account,
        generate_at_month_start=True, split_group_id=group.id,
    )
    bill_id = bill.id

    assert await generate_pending(session, test_user.id, up_to=date(2026, 11, 30)) == 0
    assert await generate_pending(session, test_user.id, up_to=date(2026, 12, 1)) == 1

    placeholder = (await session.execute(
        select(Transaction).where(Transaction.recurring_transaction_id == bill_id)
    )).scalar_one()
    assert placeholder.date == date(2026, 12, 10)
    assert placeholder.status == "pending"  # synced account: not an actual yet
    assert await _shares(session, placeholder.id) == [HALF, HALF]
    # Only this month: January's occurrence waits for January 1st.
    assert (await session.get(RecurringTransaction, bill_id)).next_occurrence == date(2027, 1, 10)


@pytest.mark.asyncio
async def test_regular_bill_still_waits_for_its_due_date(session, test_user, test_workspace, conn_account):
    _, account = conn_account
    await _bill(session, test_workspace, test_user, account)
    assert await generate_pending(session, test_user.id, up_to=date(2026, 12, 1)) == 0
    assert await generate_pending(session, test_user.id, up_to=date(2026, 12, 10)) == 1


@pytest.mark.asyncio
async def test_synced_payment_replaces_placeholder_and_keeps_split(
    session, test_user, test_workspace, conn_account
):
    conn, account = conn_account
    conn_id, account_id = conn.id, account.id
    group, _, _ = await _group(session, test_user, test_workspace.id)
    await _rename_rule(session, test_workspace, test_user)
    await _bill(
        session, test_workspace, test_user, account,
        generate_at_month_start=True, split_group_id=group.id,
    )
    await generate_pending(session, test_user.id, up_to=date(2026, 12, 1))

    provider = _provider([_tx(external_id="pix-1", description="Boletos", amount=PARCEL, date=date(2026, 12, 10))])
    await _run_sync(session, conn_id, test_workspace, test_user, provider, mock_rules=False)

    txs = await _all_txs(session, account_id)
    assert len(txs) == 1  # merged, not duplicated
    assert txs[0].source == "sync"
    assert txs[0].status == "posted"
    assert txs[0].description == "Chácara 22"
    assert await _shares(session, txs[0].id) == [HALF, HALF]


@pytest.mark.asyncio
async def test_placeholder_without_group_takes_the_rule_split(
    session, test_user, test_workspace, conn_account
):
    """A bill that names no group still ends up split when a rule splits
    the incoming charge (the rule never sees the placeholder's own row)."""
    conn, account = conn_account
    conn_id, account_id = conn.id, account.id
    group, _, _ = await _group(session, test_user, test_workspace.id)
    await _rename_rule(
        session, test_workspace, test_user,
        extra_actions=[{"op": "split_group", "value": str(group.id)}],
    )
    await _bill(session, test_workspace, test_user, account, generate_at_month_start=True)
    await generate_pending(session, test_user.id, up_to=date(2026, 12, 1))

    provider = _provider([_tx(external_id="pix-1", description="Boletos", amount=PARCEL, date=date(2026, 12, 11))])
    await _run_sync(session, conn_id, test_workspace, test_user, provider, mock_rules=False)

    txs = await _all_txs(session, account_id)
    assert len(txs) == 1
    assert await _shares(session, txs[0].id) == [HALF, HALF]


@pytest.mark.asyncio
async def test_payment_before_the_placeholder_takes_the_bill_split(
    session, test_user, test_workspace, conn_account
):
    """Paid before the bill was generated: the charge claims the bill and
    takes its split."""
    conn, account = conn_account
    conn_id, account_id = conn.id, account.id
    group, _, _ = await _group(session, test_user, test_workspace.id)
    await _rename_rule(session, test_workspace, test_user)
    await _bill(session, test_workspace, test_user, account, split_group_id=group.id)

    provider = _provider([_tx(external_id="pix-1", description="Boletos", amount=PARCEL, date=date(2026, 12, 9))])
    await _run_sync(session, conn_id, test_workspace, test_user, provider, mock_rules=False)

    txs = await _all_txs(session, account_id)
    assert len(txs) == 1
    assert txs[0].recurring_transaction_id is not None
    assert await _shares(session, txs[0].id) == [HALF, HALF]


@pytest.mark.asyncio
async def test_resize_keeps_the_split_shape(session, test_user, test_workspace, conn_account):
    _, account = conn_account
    _, me, her = await _group(session, test_user, test_workspace.id)
    equal = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, account_id=account.id,
        description="Luz", amount=Decimal("100.00"), currency="BRL", date=date(2026, 12, 10),
        type="debit", source="recurring",
    )
    exact = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, account_id=account.id,
        description="Água", amount=Decimal("100.00"), currency="BRL", date=date(2026, 12, 10),
        type="debit", source="recurring",
    )
    session.add_all([equal, exact])
    await session.flush()
    await split_service.replace_splits(
        session, equal,
        TransactionSplitsInput(share_type="equal", splits=[
            TransactionSplitInput(group_member_id=me.id), TransactionSplitInput(group_member_id=her.id),
        ]),
        test_user.id,
    )
    await split_service.replace_splits(
        session, exact,
        TransactionSplitsInput(share_type="exact", splits=[
            TransactionSplitInput(group_member_id=me.id, share_amount=Decimal("30.00")),
            TransactionSplitInput(group_member_id=her.id, share_amount=Decimal("70.00")),
        ]),
        test_user.id,
    )
    await session.flush()

    for tx in (equal, exact):
        tx.amount = Decimal("231.40")
        await recurring_split_service.resize_splits(session, tx, Decimal("100.00"), test_user.id)
    await session.commit()

    assert await _shares(session, equal.id) == [Decimal("115.70"), Decimal("115.70")]
    assert await _shares(session, exact.id) == [Decimal("69.42"), Decimal("161.98")]


@pytest.mark.asyncio
async def test_bill_rejects_a_group_out_of_reach(session, test_user, test_workspace, conn_account):
    _, account = conn_account
    with pytest.raises(ValueError):
        await _bill(session, test_workspace, test_user, account, split_group_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_update_sets_and_clears_the_group(session, test_user, test_workspace, conn_account):
    _, account = conn_account
    group, _, _ = await _group(session, test_user, test_workspace.id)
    bill = await _bill(session, test_workspace, test_user, account)

    updated = await update_recurring_transaction(
        session, bill.id, test_workspace.id,
        RecurringTransactionUpdate(split_group_id=group.id, generate_at_month_start=True),
    )
    assert updated.split_group_id == group.id
    assert updated.generate_at_month_start is True

    cleared = await update_recurring_transaction(
        session, bill.id, test_workspace.id, RecurringTransactionUpdate(split_group_id=None),
    )
    assert cleared.split_group_id is None
    assert cleared.generate_at_month_start is True
