"""Next settlement: which open shared expenses go into the next acerto.

Card charges count on their bill's due date, paid bills on the payment
date, recurring placeholders are forecasts, and expenses a settlement
listed are no longer open.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.credit_card_bill import CreditCardBill
from app.models.group import Group, GroupMember
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.group_settlement import GroupSettlementCreate, GroupSettlementUpdate
from app.services import next_settlement_service, settlement_service
from app.services.next_settlement_service import next_settlement_date


async def _setup(session, user, ws):
    today = app_today()
    card = Account(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, name="nubank",
                   type="credit_card", currency="BRL")
    bank = Account(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, name="conta",
                   type="checking", currency="BRL")
    group = Group(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, name="Casa", default_currency="BRL")
    session.add_all([card, bank, group])
    await session.flush()
    bill = CreditCardBill(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, account_id=card.id,
                          external_id="b1", due_date=today - timedelta(days=13), total_amount=Decimal("100"),
                          raw_data={"billClosingDate": (today - timedelta(days=20)).isoformat()})
    me = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=ws.id, name="matheus",
                     linked_user_id=user.id, is_self=True)
    her = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=ws.id, name="yanna")
    session.add_all([bill, me, her])
    await session.flush()

    def tx(desc, amount, when, account, **kw):
        t = Transaction(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, account_id=account.id,
                        description=desc, amount=Decimal(amount), date=when, type="debit",
                        status=kw.pop("status", "posted"), currency="BRL", source=kw.pop("source", "sync"), **kw)
        half = (Decimal(amount) / 2).quantize(Decimal("0.01"))
        session.add(t)
        session.add_all([
            TransactionSplit(transaction_id=t.id, workspace_id=ws.id, group_member_id=me.id, share_amount=Decimal(amount) - half),
            TransactionSplit(transaction_id=t.id, workspace_id=ws.id, group_member_id=her.id, share_amount=half),
        ])
        return t

    txs = {
        "old_bill": tx("on closed bill", "100.00", today - timedelta(days=30), card, bill_id=bill.id),
        "open_bill": tx("open bill", "400.00", today - timedelta(days=5), card, status="pending"),
        "far_parcel": tx("parcel 3/3", "408.33", today + timedelta(days=25), card, status="pending"),
        "internet": tx("Internet", "99.90", today - timedelta(days=3), bank),
        "entrada": tx("Entrada AP", "578.95", today + timedelta(days=10), bank, source="recurring", status="pending"),
    }
    await session.commit()
    return group, me, her, txs


def _by_desc(out):
    return {i["description"]: i for i in out["items"]}


@pytest.mark.asyncio
async def test_reference_dates_buckets_and_settled_items(session, test_user, test_workspace):
    group, me, her, txs = await _setup(session, test_user, test_workspace)
    today = app_today()

    # The last settlement paid the closed-bill charge off.
    await settlement_service.create_settlement(
        session, group.id, test_workspace.id, test_user.id,
        GroupSettlementCreate(from_member_id=her.id, to_member_id=me.id, amount=Decimal("50"),
                              currency="BRL", date=today - timedelta(days=2),
                              item_transaction_ids=[txs["old_bill"].id]),
    )
    out = await next_settlement_service.compute_next_settlement(session, group.id, test_workspace.id, test_user.id)
    items = _by_desc(out)
    assert "on closed bill" not in items
    assert out["next_settlement_date"] == next_settlement_date(today - timedelta(days=2), today)
    assert out["settlements"][0]["item_count"] == 1

    ob = items["open bill"]
    assert ob["reference_kind"] == "card_bill" and ob["estimated"] and ob["bucket"] == "next"
    assert ob["reference_date"] > today and ob["you_net"] == Decimal("200.00")
    assert items["parcel 3/3"]["bucket"] == "later"
    assert items["Internet"]["reference_kind"] == "paid" and items["Internet"]["you_net"] == Decimal("49.95")
    ent = items["Entrada AP"]
    assert ent["reference_kind"] == "planned" and not ent["confirmed"] and ent["bucket"] == "next"

    assert out["next_confirmed_total"] == Decimal("249.95")
    assert out["next_total_with_planned"] == Decimal("249.95") + ent["you_net"]
    assert out["later_total"] == items["parcel 3/3"]["you_net"]


@pytest.mark.asyncio
async def test_items_validation_and_replace(session, test_user, test_workspace):
    group, me, her, txs = await _setup(session, test_user, test_workspace)
    today = app_today()
    # Rollbacks below expire the ORM rows, so keep plain ids.
    gid, ws, uid = group.id, test_workspace.id, test_user.id
    ids = {k: t.id for k, t in txs.items()}
    base = dict(from_member_id=her.id, to_member_id=me.id, amount=Decimal("10"), currency="BRL", date=today)

    s = await settlement_service.create_settlement(
        session, gid, ws, uid, GroupSettlementCreate(**base, item_transaction_ids=[ids["internet"]]),
    )
    sid = s.id
    with pytest.raises(ValueError, match="already settled"):
        await settlement_service.create_settlement(
            session, gid, ws, uid, GroupSettlementCreate(**base, item_transaction_ids=[ids["internet"]]),
        )
    await session.rollback()
    with pytest.raises(ValueError, match="not shared"):
        await settlement_service.update_settlement(
            session, gid, sid, ws, uid, GroupSettlementUpdate(item_transaction_ids=[uuid.uuid4()]),
        )
    await session.rollback()
    await settlement_service.update_settlement(
        session, gid, sid, ws, uid, GroupSettlementUpdate(item_transaction_ids=[ids["open_bill"]]),
    )
    items = _by_desc(await next_settlement_service.compute_next_settlement(session, gid, ws, uid))
    assert "Internet" in items and "open bill" not in items

    # Deleting the settlement reopens its items.
    await settlement_service.delete_settlement(session, gid, sid, ws, uid)
    items = _by_desc(await next_settlement_service.compute_next_settlement(session, gid, ws, uid))
    assert "open bill" in items


def test_next_settlement_date():
    from datetime import date

    assert next_settlement_date(date(2026, 11, 10), date(2026, 11, 12)) == date(2026, 12, 10)
    assert next_settlement_date(date(2026, 11, 10), date(2026, 11, 5)) == date(2026, 12, 10)
    assert next_settlement_date(date(2026, 1, 31), date(2026, 2, 1)) == date(2026, 2, 28)
    assert next_settlement_date(None, date(2026, 11, 12)) is None
