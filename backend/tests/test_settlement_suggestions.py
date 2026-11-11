"""Transfers between members offered as settlements of the group."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.account import Account
from app.models.transaction import Transaction
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.group_settlement import GroupSettlementCreate
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import (
    balance_service,
    group_service,
    settlement_service,
    settlement_suggestion_service,
    split_service,
)
from tests.test_balance_service import _make_user_with_workspace

TODAY = date.today()


async def _account(session, user_id, workspace_id):
    account = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name="Conta",
        type="checking", balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    await session.flush()
    return account


async def _tx(session, account, amount, tx_type, when=TODAY, **kw):
    tx = Transaction(
        id=uuid.uuid4(), user_id=account.user_id, workspace_id=account.workspace_id,
        account_id=account.id, description=kw.pop("description", "Transferências"),
        amount=Decimal(amount), currency="BRL", date=when, type=tx_type,
        source=kw.pop("source", "sync"), **kw,
    )
    session.add(tx)
    await session.flush()
    return tx


@pytest.fixture
async def couple(session, test_user, test_workspace):
    """Her expense of 300, split equally: the caller owes her 150."""
    group = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="Casa", default_currency="BRL")
    )
    me = await group_service.create_member(
        session, group.id, test_workspace.id,
        GroupMemberCreate(name="Eu", is_self=True, linked_user_id=test_user.id),
    )
    her_user, her_ws = await _make_user_with_workspace(session, "ela@example.com")
    her = await group_service.create_member(
        session, group.id, test_workspace.id,
        GroupMemberCreate(name="Ela", linked_user_id=her_user.id),
    )
    mine = await _account(session, test_user.id, test_workspace.id)
    hers = await _account(session, her_user.id, her_ws.id)
    bill = await _tx(session, hers, "300.00", "debit", when=TODAY - timedelta(days=5), description="Mercado")
    await split_service.replace_splits(
        session, bill,
        TransactionSplitsInput(share_type="equal", splits=[
            TransactionSplitInput(group_member_id=me.id), TransactionSplitInput(group_member_id=her.id),
        ]),
        her_user.id,
    )
    await session.commit()
    return {"group": group, "me": me, "her": her, "her_user": her_user, "mine": mine, "hers": hers}


async def _suggestions(session, c, test_user, test_workspace):
    return await settlement_suggestion_service.list_suggestions(
        session, c["group"].id, test_workspace.id, test_user.id
    )


@pytest.mark.asyncio
async def test_transfer_paying_the_debt_is_offered(session, test_user, test_workspace, couple):
    c = couple
    pix = await _tx(session, c["mine"], "150.00", "debit")
    # Her bank dates the PIX a day later (UTC).
    received = await _tx(session, c["hers"], "150.00", "credit", when=TODAY + timedelta(days=1))
    await session.commit()

    [s] = await _suggestions(session, c, test_user, test_workspace)
    assert s["transaction_id"] == pix.id
    assert s["receiver_transaction_id"] == received.id
    assert (s["from_member_id"], s["to_member_id"]) == (c["me"].id, c["her"].id)
    assert s["amount"] == Decimal("150.00")
    assert s["owed"] == Decimal("150.00")
    assert s["matches_balance"] is True


@pytest.mark.asyncio
async def test_recording_it_links_both_sides_and_settles(session, test_user, test_workspace, couple):
    c = couple
    pix = await _tx(session, c["mine"], "150.00", "debit")
    received = await _tx(session, c["hers"], "150.00", "credit")
    await session.commit()
    [s] = await _suggestions(session, c, test_user, test_workspace)

    await settlement_service.create_settlement(
        session, c["group"].id, test_workspace.id, test_user.id,
        GroupSettlementCreate(
            from_member_id=s["from_member_id"], to_member_id=s["to_member_id"],
            amount=s["amount"], currency="BRL", date=s["date"],
            transaction_id=pix.id, receiver_transaction_id=received.id,
        ),
    )

    balances = await balance_service.compute_balances(
        session, c["group"].id, test_workspace.id, test_user.id
    )
    assert balances["lines"] == []
    # Her synced credit was linked: no second credit written on her side.
    her_credits = await session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.user_id == c["her_user"].id, Transaction.type == "credit"
        )
    )
    assert her_credits == 1
    assert await _suggestions(session, c, test_user, test_workspace) == []


@pytest.mark.asyncio
async def test_partial_payment_is_offered_as_partial(session, test_user, test_workspace, couple):
    c = couple
    await _tx(session, c["mine"], "100.00", "debit")
    await _tx(session, c["hers"], "100.00", "credit")
    await session.commit()

    [s] = await _suggestions(session, c, test_user, test_workspace)
    assert s["matches_balance"] is False


@pytest.mark.asyncio
async def test_transfer_larger_than_the_debt_is_not_offered(session, test_user, test_workspace, couple):
    c = couple
    await _tx(session, c["mine"], "3000.00", "debit")
    await _tx(session, c["hers"], "3000.00", "credit")
    await session.commit()

    assert await _suggestions(session, c, test_user, test_workspace) == []


@pytest.mark.asyncio
async def test_payment_to_someone_else_is_not_offered(session, test_user, test_workspace, couple):
    """No matching credit on her side: the money went elsewhere."""
    c = couple
    await _tx(session, c["mine"], "150.00", "debit")
    await _tx(session, c["hers"], "150.00", "credit", when=TODAY - timedelta(days=10))
    await session.commit()

    assert await _suggestions(session, c, test_user, test_workspace) == []


@pytest.mark.asyncio
async def test_dismissed_transfer_is_not_offered_again(session, test_user, test_workspace, couple):
    c = couple
    pix = await _tx(session, c["mine"], "150.00", "debit")
    await _tx(session, c["hers"], "150.00", "credit")
    await session.commit()

    assert await settlement_suggestion_service.dismiss(
        session, c["group"].id, test_workspace.id, test_user.id, pix.id
    )
    assert await _suggestions(session, c, test_user, test_workspace) == []


@pytest.mark.asyncio
async def test_the_receiver_is_not_offered_the_payers_transfer(session, test_user, test_workspace, couple):
    """She is owed, not owing: nothing for her to record."""
    c = couple
    await _tx(session, c["mine"], "150.00", "debit")
    await _tx(session, c["hers"], "150.00", "credit")
    await session.commit()

    assert await settlement_suggestion_service.list_suggestions(
        session, c["group"].id, test_workspace.id, c["her_user"].id
    ) in ([], None)


@pytest.mark.asyncio
async def test_receiver_transaction_must_match(session, test_user, test_workspace, couple):
    c = couple
    pix = await _tx(session, c["mine"], "150.00", "debit")
    other = await _tx(session, c["hers"], "149.00", "credit")
    await session.commit()

    with pytest.raises(ValueError):
        await settlement_service.create_settlement(
            session, c["group"].id, test_workspace.id, test_user.id,
            GroupSettlementCreate(
                from_member_id=c["me"].id, to_member_id=c["her"].id,
                amount=Decimal("150.00"), currency="BRL", date=TODAY,
                transaction_id=pix.id, receiver_transaction_id=other.id,
            ),
        )


@pytest.mark.asyncio
async def test_transfers_before_the_last_settlement_are_not_offered(
    session, test_user, test_workspace, couple
):
    c = couple
    await _tx(session, c["mine"], "20.00", "debit", when=TODAY - timedelta(days=3))
    await _tx(session, c["hers"], "20.00", "credit", when=TODAY - timedelta(days=3))
    await settlement_service.create_settlement(
        session, c["group"].id, test_workspace.id, test_user.id,
        GroupSettlementCreate(
            from_member_id=c["me"].id, to_member_id=c["her"].id,
            amount=Decimal("50.00"), currency="BRL", date=TODAY - timedelta(days=2),
        ),
    )
    later = await _tx(session, c["mine"], "100.00", "debit")
    await _tx(session, c["hers"], "100.00", "credit")
    await session.commit()

    [s] = await _suggestions(session, c, test_user, test_workspace)
    assert s["transaction_id"] == later.id
    assert s["matches_balance"] is True  # 150 - 50 settled = 100 owed
