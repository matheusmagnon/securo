"""Rule action "split_group": split matching transactions among a group."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.services import group_service, rule_service

PARCELA = Decimal("897.44")


async def _setup(session: AsyncSession, test_user, workspace_id):
    group = await group_service.create_group(session, workspace_id, test_user.id, GroupCreate(name="Casa"))
    me = await group_service.create_member(
        session, group.id, workspace_id, GroupMemberCreate(name="Eu", is_self=True)
    )
    her = await group_service.create_member(session, group.id, workspace_id, GroupMemberCreate(name="Ela"))
    account = Account(
        id=uuid.uuid4(), user_id=test_user.id, name="99Pay", type="checking",
        balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    await session.flush()
    return group, me, her, account


def _rule(test_user, workspace_id, group_id, amount=PARCELA) -> Rule:
    return Rule(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=workspace_id,
        name="Chácara",
        conditions_op="and",
        conditions=[
            {"field": "amount", "op": "equals", "value": str(-amount)},  # debits are negative
            {"field": "type", "op": "equals", "value": "debit"},
        ],
        actions=[{"op": "split_group", "value": str(group_id)}],
        priority=10,
        is_active=True,
    )


async def _tx(session, test_user, account, amount=PARCELA, tx_type="debit") -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, account_id=account.id, description="Boletos",
        amount=amount, currency="BRL", date=date(2026, 11, 10), type=tx_type, source="sync",
    )
    session.add(tx)
    await session.flush()
    return tx


async def _shares(session, tx_id) -> list[Decimal]:
    rows = await session.execute(
        select(TransactionSplit.share_amount).where(TransactionSplit.transaction_id == tx_id)
    )
    return sorted(rows.scalars().all())


@pytest.mark.asyncio
async def test_matching_transaction_is_split_equally(session, test_user, test_workspace):
    group, _, _, account = await _setup(session, test_user, test_workspace.id)
    session.add(_rule(test_user, test_workspace.id, group.id))
    await session.flush()
    tx = await _tx(session, test_user, account)

    await rule_service.apply_rules_to_transaction(session, test_user.id, tx)
    await session.commit()

    assert await _shares(session, tx.id) == [Decimal("448.72"), Decimal("448.72")]


@pytest.mark.asyncio
async def test_other_amounts_and_credits_untouched(session, test_user, test_workspace):
    group, _, _, account = await _setup(session, test_user, test_workspace.id)
    session.add(_rule(test_user, test_workspace.id, group.id))
    await session.flush()
    other = await _tx(session, test_user, account, amount=Decimal("120.00"))
    refund = await _tx(session, test_user, account, tx_type="credit")

    for tx in (other, refund):
        await rule_service.apply_rules_to_transaction(session, test_user.id, tx)
    await session.commit()

    assert await _shares(session, other.id) == []
    assert await _shares(session, refund.id) == []


@pytest.mark.asyncio
async def test_already_split_transaction_keeps_its_split(session, test_user, test_workspace):
    from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
    from app.services import split_service

    group, me, her, account = await _setup(session, test_user, test_workspace.id)
    session.add(_rule(test_user, test_workspace.id, group.id))
    await session.flush()
    tx = await _tx(session, test_user, account)
    await split_service.replace_splits(
        session, tx,
        TransactionSplitsInput(share_type="exact", splits=[
            TransactionSplitInput(group_member_id=me.id, share_amount=Decimal("297.44")),
            TransactionSplitInput(group_member_id=her.id, share_amount=Decimal("600.00")),
        ]),
        test_user.id,
    )

    await rule_service.apply_rules_to_transaction(session, test_user.id, tx)
    await session.commit()
    assert await _shares(session, tx.id) == [Decimal("297.44"), Decimal("600.00")]


@pytest.mark.asyncio
async def test_rule_with_unknown_group_is_rejected(session, test_user, test_workspace):
    from app.schemas.rule import RuleCreate

    data = RuleCreate(
        name="x", conditions_op="and",
        conditions=[{"field": "amount", "op": "equals", "value": "1"}],
        actions=[{"op": "split_group", "value": str(uuid.uuid4())}],
    )
    with pytest.raises(ValueError):
        await rule_service.create_rule(session, test_workspace.id, test_user.id, data)


@pytest.mark.asyncio
async def test_rule_with_group_is_accepted(session, test_user, test_workspace):
    from app.schemas.rule import RuleCreate

    group, _, _, _ = await _setup(session, test_user, test_workspace.id)
    data = RuleCreate(
        name="Chácara", conditions_op="and",
        conditions=[{"field": "amount", "op": "equals", "value": "897.44"}],
        actions=[{"op": "split_group", "value": str(group.id)}],
    )
    rule = await rule_service.create_rule(session, test_workspace.id, test_user.id, data)
    assert rule.actions[0]["op"] == "split_group"


@pytest.mark.asyncio
async def test_stale_group_does_not_fail(session, test_user, test_workspace):
    """A rule whose group was deleted logs and moves on."""
    group, _, _, account = await _setup(session, test_user, test_workspace.id)
    session.add(_rule(test_user, test_workspace.id, uuid.uuid4()))
    await session.flush()
    tx = await _tx(session, test_user, account)
    await rule_service.apply_rules_to_transaction(session, test_user.id, tx)
    assert await _shares(session, tx.id) == []
