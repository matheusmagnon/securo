"""Credit-card bill awareness of the MCP tools (mcp_server/tools/bills.py).

Mirrors the case that misled ChatGPT: a bill closed on day X and was paid;
an installment dated on day X is pending and must read as the NEXT bill,
and the payment itself must not shrink the open bill.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from mcp_server.registry import call_tool
from mcp_server.tools.bills import add_months


async def _card(session, user, workspace):
    today = app_today()
    close0 = today - timedelta(days=8)
    due0 = today - timedelta(days=1)
    acc = Account(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="platinum",
                  type="credit_card", currency="BRL")
    session.add(acc)
    await session.flush()
    bill = CreditCardBill(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, account_id=acc.id,
        external_id="b1", due_date=due0, total_amount=Decimal("100.00"), currency="BRL",
        raw_data={
            "billClosingDate": f"{close0.isoformat()}T00:00:00.000Z",
            "payments": [{"amount": 100.0, "paymentDate": f"{due0.isoformat()}T00:00:00.000Z"}],
        },
    )
    session.add(bill)

    def tx(desc, d, amount, *, type_="debit", status="pending", bill_id=None, n=None, total=None):
        t = Transaction(
            id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, account_id=acc.id,
            description=desc, amount=Decimal(amount), date=d, type=type_, status=status,
            bill_id=bill_id, currency="BRL", source="sync", installment_number=n, total_installments=total,
        )
        session.add(t)
        return t

    tx("Carro 1/3", close0 - timedelta(days=10), "100.00", status="posted", bill_id=bill.id, n=1, total=3)
    tx("Carro 2/3", close0, "100.00", n=2, total=3)
    tx("Carro 3/3", add_months(close0, 1) + timedelta(days=5), "100.00", n=3, total=3)
    tx("Pagamento recebido", due0, "100.00", type_="credit", status="posted")
    await session.commit()
    return acc, close0, due0


def _ctx(user, workspace):
    from mcp_server.auth import CallContext

    return CallContext(user_id=user.id, workspace_id=workspace.id, external=True)


@pytest.mark.asyncio
async def test_bills_split_closed_open_and_future(session, test_user, test_workspace):
    acc, close0, due0 = await _card(session, test_user, test_workspace)
    out = await call_tool(session, _ctx(test_user, test_workspace), "get_credit_card_bills", {"include_items": True})
    card = next(c for c in out["cards"] if c["account_id"] == str(acc.id))

    closed = card["closed_bills"][0]
    assert closed["due_date"] == due0.isoformat()
    assert closed["close_date"] == close0.isoformat()
    assert closed["payment_status"] == "paid"

    open_bill, future = card["open_and_future_bills"]
    assert open_bill["bill"] == "open"
    assert open_bill["due_date"] == add_months(due0, 1).isoformat()
    # The payment is not a credit on the open bill.
    assert open_bill["total_so_far"] == 100.0 and open_bill["charges"] == 1
    assert open_bill["items"][0]["installment"] == "2/3"
    assert future["bill"] == "future"
    assert future["due_date"] == add_months(due0, 2).isoformat()


@pytest.mark.asyncio
async def test_transaction_rows_say_which_bill(session, test_user, test_workspace):
    acc, close0, due0 = await _card(session, test_user, test_workspace)
    out = await call_tool(session, _ctx(test_user, test_workspace), "list_transactions",
                          {"account_ids": [str(acc.id)], "search": "Carro"})
    by_desc = {i["description"]: i["credit_card_bill"] for i in out["items"]}
    assert by_desc["Carro 1/3"]["bill"] == "closed"
    assert by_desc["Carro 1/3"]["payment_status"] == "paid"
    assert by_desc["Carro 2/3"] == {
        "bill": "open", "close_date": add_months(close0, 1).isoformat(),
        "due_date": add_months(due0, 1).isoformat(), "estimated": True,
    }
    assert by_desc["Carro 3/3"]["bill"] == "future"


@pytest.mark.asyncio
async def test_sync_queues_each_connection_once(session, test_user, test_workspace, monkeypatch):
    from datetime import datetime, timezone

    from app.models.bank_connection import BankConnection
    import mcp_server.tools.sync as sync_tool

    sent = []
    monkeypatch.setattr(sync_tool, "_send", lambda cid, uid: sent.append(cid))
    fresh = BankConnection(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                           provider="pluggy", external_id="a", institution_name="Banco A", credentials={},
                           last_sync_at=datetime.now(timezone.utc))
    stale = BankConnection(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                           provider="pluggy", external_id="b", institution_name="Banco B", credentials={},
                           last_sync_at=datetime.now(timezone.utc) - timedelta(hours=3))
    session.add_all([fresh, stale])
    await session.commit()
    out = await call_tool(session, _ctx(test_user, test_workspace), "sync_bank_connections", {})
    actions = {c["bank"]: c["action"] for c in out["connections"]}
    assert actions == {"Banco A": "skipped_recently_synced", "Banco B": "sync_queued"}
    assert sent == [str(stale.id)]


@pytest.mark.asyncio
async def test_sync_status_reports_pluggy_clock_and_real_bank(session, test_user, test_workspace, monkeypatch):
    from app.models.bank_connection import BankConnection
    import mcp_server.tools.sync as sync_tool

    async def fake_item(_session, conn):
        return {"provider_status": "UPDATED", "provider_last_updated_at": "2026-11-11T21:51:17Z",
                "provider_next_auto_sync_at": None, "on_demand_refresh": False}

    monkeypatch.setattr(sync_tool, "_provider_item", fake_item)
    conn = BankConnection(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                          provider="pluggy", external_id="n", institution_name="MeuPluggy",
                          display_name="MeuPluggy NuBank", credentials={})
    session.add(conn)
    await session.commit()
    out = await call_tool(session, _ctx(test_user, test_workspace), "get_sync_status", {})
    row = out["connections"][0]
    assert row["bank"] == "NuBank"
    assert row["provider_last_updated_at"] == "2026-11-11T21:51:17Z"
    assert row["on_demand_refresh"] is False
