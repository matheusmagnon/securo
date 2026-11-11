"""The household month: what the group cost, who paid, bills and contracts."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.schemas.group import GroupOverview
from app.schemas.recurring_transaction import RecurringTransactionCreate
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import group_overview_service, split_service
from app.services.recurring_transaction_service import create_recurring_transaction
from tests import test_settlement_suggestions as _suggestions

_tx = _suggestions._tx
couple = _suggestions.couple  # the fixture, reused

TODAY = date.today()
MONTH = TODAY.replace(day=1)


async def _split(session, c, tx, user_id):
    await split_service.replace_splits(
        session, tx,
        TransactionSplitsInput(share_type="equal", splits=[
            TransactionSplitInput(group_member_id=c["me"].id),
            TransactionSplitInput(group_member_id=c["her"].id),
        ]),
        user_id,
    )


async def _overview(session, c, user_id, workspace_id, month=MONTH):
    data = await group_overview_service.compute_overview(
        session, c["group"].id, workspace_id, user_id, month
    )
    return GroupOverview(**data)


@pytest.mark.asyncio
async def test_month_totals_and_who_paid(session, test_user, test_workspace, couple):
    c = couple  # her "Mercado" of 300, split, five days ago
    mine = await _tx(session, c["mine"], "100.00", "debit", when=MONTH, description="Internet")
    await _split(session, c, mine, test_user.id)
    # Her own unsplit spending stays out of the household view.
    await _tx(session, c["hers"], "999.00", "debit", when=MONTH, description="Pessoal")
    await session.commit()

    o = await _overview(session, c, test_user.id, test_workspace.id)
    in_month = [i for i in o.items if i.date >= MONTH]
    assert "Pessoal" not in {i.description for i in o.items}
    paid = {p.member_id: p.amount for p in o.paid_by}
    assert paid[c["me"].id] == Decimal("100.00")
    expected_her = sum((i.amount for i in in_month if i.payer_member_id == c["her"].id), Decimal("0"))
    assert paid[c["her"].id] == expected_her
    internet = next(i for i in o.items if i.description == "Internet")
    assert internet.my_share == Decimal("50.00")
    assert internet.status == "paid"


@pytest.mark.asyncio
async def test_shared_bill_paid_or_due_and_contract_progress(
    session, test_user, test_workspace, couple
):
    c = couple
    due = MONTH.replace(day=28)
    bill = await create_recurring_transaction(
        session, test_workspace.id, test_user.id,
        RecurringTransactionCreate(
            description="Chácara 22", amount=Decimal("897.44"), currency="BRL", type="debit",
            frequency="monthly", start_date=due, end_date=due.replace(year=due.year + 2),
            account_id=c["mine"].id, split_group_id=c["group"].id, installment_offset=4,
        ),
    )
    o = await _overview(session, c, test_user.id, test_workspace.id)
    [b] = o.bills
    assert (b.paid, b.number, b.total) == (False, 5, 29)  # 25 occurrences + 4 paid before

    paid = await _tx(
        session, c["mine"], "897.44", "debit", when=due - timedelta(days=10),
        recurring_transaction_id=bill.id,
    )
    await _split(session, c, paid, test_user.id)
    await session.commit()
    o = await _overview(session, c, test_user.id, test_workspace.id)
    [b] = o.bills
    assert b.paid is True
    [contract] = o.contracts
    assert contract.total == 29
    done = 4 + (1 if due <= TODAY else 0)  # the 4 before, and this one once due
    assert contract.done == done
    assert contract.remaining_amount == Decimal("897.44") * (29 - done)


@pytest.mark.asyncio
async def test_both_members_see_the_same_month(session, test_user, test_workspace, couple):
    c = couple
    mine = await _tx(session, c["mine"], "80.00", "debit", when=MONTH)
    await _split(session, c, mine, test_user.id)
    await session.commit()

    me = await _overview(session, c, test_user.id, test_workspace.id)
    her = await _overview(session, c, c["her_user"].id, test_workspace.id)
    assert me.total == her.total
    assert {i.transaction_id for i in me.items} == {i.transaction_id for i in her.items}
