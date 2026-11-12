"""propose_split_transaction: share an existing charge with a group.

The case that blocked ChatGPT: a 3x card purchase not in "Casa" yet. The
preview must not write, must show the balance before/after, and each
scope must touch exactly the parcels it says.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.group import Group, GroupMember
from app.models.installment_split_rule import InstallmentSplitRule
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
import mcp_server.tools  # noqa: F401  (registers the tools)
from mcp_server.registry import call_tool


def _ctx(user, workspace):
    from mcp_server.auth import CallContext

    return CallContext(user_id=user.id, workspace_id=workspace.id, external=True)


async def _setup(session, user, workspace):
    acc = Account(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="platinum",
                  type="credit_card", currency="BRL")
    group = Group(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="Casa", default_currency="BRL")
    session.add_all([acc, group])
    await session.flush()
    me = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=workspace.id, name="matheus",
                     linked_user_id=user.id, is_self=True)
    her = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=workspace.id, name="yanna")
    session.add_all([me, her])
    today = app_today()
    purchase = today - timedelta(days=70)
    parcels = []
    for n in (1, 2, 3):
        t = Transaction(
            id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, account_id=acc.id,
            description=f"Hidraulica {n}/3", amount=Decimal("400.00"), date=purchase + timedelta(days=30 * (n - 1)),
            type="debit", status="posted" if n < 3 else "pending", currency="BRL", source="sync",
            installment_number=n, total_installments=3, installment_purchase_date=purchase,
            installment_total_amount=Decimal("1200.00"),
        )
        parcels.append(t)
    session.add_all(parcels)
    await session.commit()
    return group, me, her, parcels


async def _split_count(session, ids):
    rows = (await session.execute(select(TransactionSplit).where(TransactionSplit.transaction_id.in_(ids)))).scalars().all()
    return len(rows)


def _her_change(out):
    return next(c for c in out["balance_change"] if c["member"] == "yanna" and c["ledger"] == "balance")


@pytest.mark.asyncio
async def test_preview_writes_nothing_and_shows_before_after(session, test_user, test_workspace):
    group, me, her, parcels = await _setup(session, test_user, test_workspace)
    out = await call_tool(session, _ctx(test_user, test_workspace), "propose_split_transaction",
                          {"transaction_id": str(parcels[2].id)})
    assert "applied" not in out, out
    assert out["group"]["name"] == "Casa" and out["parcels_affected"] == 1
    assert out["parcels"][0]["new_split"] == [{"member": "matheus (you)", "share": 200.0}, {"member": "yanna", "share": 200.0}]
    assert {"member": "yanna", "total": 200.0} in out["total_per_member"]
    ch = _her_change(out)
    assert ch["before"] == 0 and ch["after"] == 200.0 and ch["after_direction"] == "member_owes_you"
    assert any("1/3" in w and "2/3" in w for w in out["warnings"])
    assert await _split_count(session, [p.id for p in parcels]) == 0


@pytest.mark.asyncio
async def test_apply_this_only_touches_one_parcel(session, test_user, test_workspace):
    group, me, her, parcels = await _setup(session, test_user, test_workspace)
    out = await call_tool(session, _ctx(test_user, test_workspace), "propose_split_transaction",
                          {"transaction_id": str(parcels[2].id), "apply": True})
    assert out["applied"] is True
    assert await _split_count(session, [parcels[2].id]) == 2
    assert await _split_count(session, [parcels[0].id, parcels[1].id]) == 0


@pytest.mark.asyncio
async def test_all_installments_shares_every_parcel_and_keeps_a_rule(session, test_user, test_workspace):
    group, me, her, parcels = await _setup(session, test_user, test_workspace)
    out = await call_tool(session, _ctx(test_user, test_workspace), "propose_split_transaction",
                          {"transaction_id": str(parcels[2].id), "scope": "all_installments", "apply": True})
    assert out["parcels_affected"] == 3
    assert {"member": "yanna", "total": 600.0} in out["total_per_member"]
    assert await _split_count(session, [p.id for p in parcels]) == 6
    rule = (await session.execute(select(InstallmentSplitRule))).scalar_one()
    assert rule.from_installment == 1


@pytest.mark.asyncio
async def test_percent_and_remove(session, test_user, test_workspace):
    group, me, her, parcels = await _setup(session, test_user, test_workspace)
    ctx = _ctx(test_user, test_workspace)
    tid = str(parcels[2].id)
    out = await call_tool(session, ctx, "propose_split_transaction", {
        "transaction_id": tid, "share_type": "percent", "apply": True,
        "members": [{"member_id": str(me.id), "share_pct": 70}, {"member_id": str(her.id), "share_pct": 30}],
    })
    assert out["parcels"][0]["new_split"][1] == {"member": "yanna", "share": 120.0}
    out = await call_tool(session, ctx, "propose_split_transaction", {"transaction_id": tid, "remove": True})
    assert out["action"] == "remove_from_group" and _her_change(out)["after"] == 0
    await call_tool(session, ctx, "propose_split_transaction", {"transaction_id": tid, "remove": True, "apply": True})
    assert await _split_count(session, [parcels[2].id]) == 0


@pytest.mark.asyncio
async def test_refusals(session, test_user, test_workspace):
    group, me, her, parcels = await _setup(session, test_user, test_workspace)
    ctx = _ctx(test_user, test_workspace)
    assert "error" in await call_tool(session, ctx, "propose_split_transaction", {"transaction_id": str(uuid.uuid4())})
    bad = await call_tool(session, ctx, "propose_split_transaction", {
        "transaction_id": str(parcels[2].id), "members": [{"member_id": str(uuid.uuid4())}],
    })
    assert "error" in bad
    wrong = await call_tool(session, ctx, "propose_split_transaction", {
        "transaction_id": str(parcels[2].id), "share_type": "exact", "apply": True,
        "members": [{"member_id": str(me.id), "share_amount": 100}, {"member_id": str(her.id), "share_amount": 100}],
    })
    assert "error" in wrong
    assert await _split_count(session, [parcels[2].id]) == 0
