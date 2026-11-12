"""Credit-card bills: which bill a charge is on, and what each bill holds.

The bank (via the provider) sends closed bills with their closing date,
due date, total and payments, and links each charge of a closed bill to
it (`Transaction.bill_id`). Charges after the last closing stay unlinked
and usually `pending` until the next bill closes. Without this, a model
sees "pending" and guesses — typically that the charge is unpaid or was on
the bill already paid. Here the cycle is derived from the bank's own
closing date: a charge dated on or after a bill's closing day belongs to
the next bill (Brazilian convention, and what the bank data shows).
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.services._query_filters import counts_on_bill
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, parse_date, parse_uuid, resolve_workspace_id

CLOSED_BILLS = 3
# How far ahead future installments are grouped into bills.
FUTURE_BILLS = 12
PAYMENT_MATCH_DAYS = 5


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _parse_iso_date(v: Any) -> Optional[date]:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _close_date(bill: CreditCardBill, close_day: Optional[int]) -> Optional[date]:
    raw = bill.raw_data if isinstance(bill.raw_data, dict) else {}
    parsed = _parse_iso_date(raw.get("billClosingDate"))
    if parsed:
        return parsed
    if close_day:
        # The latest close day before the due date.
        for back in (0, 1):
            cand = add_months(bill.due_date.replace(day=1), -back)
            cand = cand.replace(day=min(close_day, calendar.monthrange(cand.year, cand.month)[1]))
            if cand < bill.due_date:
                return cand
    return None


def _payments(bill: CreditCardBill) -> list[tuple[Optional[date], Decimal]]:
    raw = bill.raw_data if isinstance(bill.raw_data, dict) else {}
    out = []
    for p in raw.get("payments") or []:
        if isinstance(p, dict) and p.get("amount") is not None:
            out.append((_parse_iso_date(p.get("paymentDate")), Decimal(str(p["amount"]))))
    return out


def _bill_summary(bill: CreditCardBill, close: Optional[date]) -> dict[str, Any]:
    total = Decimal(str(bill.total_amount or 0))
    pays = _payments(bill)
    paid = sum((a for _, a in pays), Decimal("0"))
    if pays:
        status = "paid" if paid >= total - Decimal("0.01") else "partially_paid"
    elif bill.due_date < app_today():
        # The provider lists payments for most banks; none listed after the
        # due date is worth flagging but is not proof of non-payment.
        status = "no_payment_reported"
    else:
        status = "awaiting_payment"
    return {
        "bill": "closed",
        "close_date": close.isoformat() if close else None,
        "due_date": bill.due_date.isoformat(),
        "total": num(total),
        "minimum_payment": num(bill.minimum_payment),
        "paid_amount": num(paid) if pays else None,
        "payment_dates": sorted({d.isoformat() for d, _ in pays if d}),
        "payment_status": status,
    }


class CardCycles:
    """Closed bills of one card plus the cycle math for what came after."""

    def __init__(self, account: Account, bills: list[CreditCardBill]):
        self.account = account
        self.bills = sorted(bills, key=lambda b: b.due_date, reverse=True)
        self.by_id = {b.id: b for b in self.bills}
        self.close_of = {b.id: _close_date(b, account.statement_close_day) for b in self.bills}
        self.last = next((b for b in self.bills if self.close_of[b.id]), None)

    def payment_dates(self) -> list[tuple[Optional[date], Decimal]]:
        return [p for b in self.bills for p in _payments(b)]

    def cycle_of(self, tx_date: date) -> Optional[tuple[int, date, date]]:
        """(k, close, due) of the bill after the last closed one that a charge
        dated `tx_date` lands on; None when it predates the last closing."""
        if self.last is None:
            return None
        close0 = self.close_of[self.last.id]
        assert close0 is not None
        if tx_date < close0:
            return None
        k = 1
        while add_months(close0, k) <= tx_date:
            k += 1
        return k, add_months(close0, k), add_months(self.last.due_date, k)

    def label(self, tx: Transaction) -> dict[str, Any]:
        today = app_today()
        if tx.bill_id and tx.bill_id in self.by_id:
            b = self.by_id[tx.bill_id]
            s = _bill_summary(b, self.close_of[b.id])
            return {k: s[k] for k in ("bill", "close_date", "due_date", "payment_status")}
        cyc = self.cycle_of(tx.date) if tx.date else None
        if cyc is None:
            if self.last is None:
                return {"bill": "unknown", "reason": "no bills synced for this card"}
            return {"bill": "unknown", "reason": "dated before the last closed bill but not linked to it"}
        _, close, due = cyc
        return {
            "bill": _open_state(close, today),
            "close_date": close.isoformat(),
            "due_date": due.isoformat(),
            "estimated": True,
        }


def _open_state(close: date, today: date) -> str:
    if close <= today:
        # Closed by the calendar, not yet sent by the bank.
        return "closed_awaiting_bank_data"
    if add_months(close, -1) <= today:
        return "open"
    return "future"


async def load_cycles(session: AsyncSession, account_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, CardCycles]:
    ids = list(set(account_ids))
    if not ids:
        return {}
    accounts = (
        await session.execute(select(Account).where(Account.id.in_(ids), Account.type == "credit_card"))
    ).scalars().all()
    if not accounts:
        return {}
    bills = (
        await session.execute(
            select(CreditCardBill)
            .where(CreditCardBill.account_id.in_([a.id for a in accounts]))
            .order_by(CreditCardBill.due_date.desc())
        )
    ).scalars().all()
    per: dict[uuid.UUID, list[CreditCardBill]] = {a.id: [] for a in accounts}
    for b in bills:
        if len(per[b.account_id]) < 24:
            per[b.account_id].append(b)
    return {a.id: CardCycles(a, per[a.id]) for a in accounts}


def _signed(tx: Transaction) -> Decimal:
    amt = Decimal(str(tx.amount))
    return amt if tx.type == "debit" else -amt


def _is_bill_payment(tx: Transaction, payments: list[tuple[Optional[date], Decimal]]) -> bool:
    if tx.type != "credit" or tx.date is None:
        return False
    amt = Decimal(str(tx.amount))
    return any(
        d is not None and abs((tx.date - d).days) <= PAYMENT_MATCH_DAYS and abs(a - amt) < Decimal("0.01")
        for d, a in payments
    )


def _item(tx: Transaction) -> dict[str, Any]:
    return {
        "id": str(tx.id),
        "date": tx.date.isoformat() if tx.date else None,
        "description": tx.description,
        "amount": num(_signed(tx)),
        "status": tx.status,
        "installment": f"{tx.installment_number}/{tx.total_installments}"
        if getattr(tx, "installment_number", None) and getattr(tx, "total_installments", None)
        else None,
    }


@tool(
    name="get_credit_card_bills",
    description=(
        "Credit-card bills (faturas) per card: the last closed bills from the "
        "bank (closing date, due date, total, minimum, whether the bank reports "
        "it paid) and the open bill plus future bills built from charges after "
        "the last closing (future installments land on future bills). Use this "
        "for ANY question about which bill a charge is on, what is due next, "
        "whether a bill was paid, or how much the next bill is so far. A charge "
        "dated on or after a bill's closing date is on the NEXT bill. On cards, "
        "`pending` means 'not on a closed bill yet', NOT 'unpaid' or 'needs to "
        "be recorded'. Open/future totals are partial (more charges can still "
        "come) and their dates are estimated from the last closed bill. Pass "
        "`due_date` to list every charge of one bill (closed or open)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "format": "uuid", "description": "One card; omit for all cards."},
            "due_date": {"type": "string", "format": "date", "description": "List the charges of the bill due on this date (YYYY-MM-DD)."},
            "include_items": {"type": "boolean", "default": False, "description": "List the charges of the open bill too."},
        },
        "additionalProperties": False,
    },
    tags=["read", "accounts", "credit_card"],
)
async def get_credit_card_bills(
    *,
    session: AsyncSession,
    ctx: CallContext,
    account_id: str | None = None,
    due_date: str | None = None,
    include_items: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    q = (
        select(Account)
        .outerjoin(BankConnection, BankConnection.id == Account.connection_id)
        .where(Account.type == "credit_card", Account.is_closed.is_(False))
        .where((Account.workspace_id == ws_id) | (BankConnection.workspace_id == ws_id))
    )
    wanted = parse_uuid(account_id) if account_id else None
    if wanted:
        q = q.where(Account.id == wanted)
    accounts = (await session.execute(q)).scalars().all()
    cycles = await load_cycles(session, [a.id for a in accounts])
    want_due = parse_date(due_date) if due_date else None
    today = app_today()

    from mcp_server.tools.sync import bank_names

    banks = await bank_names(session, [a.connection_id for a in accounts])
    cards = []
    for acc in accounts:
        cyc = cycles[acc.id]
        closed = [_bill_summary(b, cyc.close_of[b.id]) for b in cyc.bills[:CLOSED_BILLS]]
        card: dict[str, Any] = {
            "account_id": str(acc.id),
            "account_name": acc.name,
            "bank": banks.get(acc.connection_id),
            "closed_bills": closed,
            "open_and_future_bills": [],
        }
        if cyc.last is not None:
            close0 = cyc.close_of[cyc.last.id]
            rows = (
                await session.execute(
                    select(Transaction)
                    .where(
                        Transaction.account_id == acc.id,
                        Transaction.bill_id.is_(None),
                        Transaction.date >= close0,
                        counts_on_bill(),
                    )
                    .order_by(Transaction.date)
                )
            ).scalars().all()
            payments = cyc.payment_dates()
            buckets: dict[int, list[Transaction]] = {}
            for tx in rows:
                if _is_bill_payment(tx, payments):
                    continue
                c = cyc.cycle_of(tx.date)
                if c is not None and c[0] <= FUTURE_BILLS:
                    buckets.setdefault(c[0], []).append(tx)
            for k in sorted(buckets):
                close, due = add_months(close0, k), add_months(cyc.last.due_date, k)
                txs = buckets[k]
                entry: dict[str, Any] = {
                    "bill": _open_state(close, today),
                    "close_date": close.isoformat(),
                    "due_date": due.isoformat(),
                    "estimated": True,
                    "total_so_far": num(sum((_signed(t) for t in txs), Decimal("0"))),
                    "charges": len(txs),
                }
                if (include_items and k == 1) or (want_due and want_due == due):
                    entry["items"] = [_item(t) for t in txs]
                card["open_and_future_bills"].append(entry)
        else:
            card["note"] = "No bills synced for this card; bill membership is unknown."

        if want_due:
            match = next((b for b in cyc.bills if b.due_date == want_due), None)
            if match is not None:
                txs = (
                    await session.execute(
                        select(Transaction).where(Transaction.bill_id == match.id).order_by(Transaction.date)
                    )
                ).scalars().all()
                card["bill_items"] = {"due_date": want_due.isoformat(), "items": [_item(t) for t in txs]}
        cards.append(card)
    return {"today": today.isoformat(), "cards": cards}

