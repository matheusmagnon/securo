"""Credit-card bill cycles: which bill a charge is on.

The bank (via the provider) sends closed bills with their closing date,
due date, total and payments, and links each charge of a closed bill to
it (`Transaction.bill_id`). Charges after the last closing stay unlinked
and usually `pending` until the next bill closes. The cycle of such a
charge is derived from the bank's own closing date: a charge dated on or
after a bill's closing day belongs to the next bill (Brazilian convention,
and what the bank data shows).

Used by the MCP bill tools and by the group "next settlement" view.
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
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction


def _num(x: Any) -> Optional[float]:
    return None if x is None else float(x)


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
        "total": _num(total),
        "minimum_payment": _num(bill.minimum_payment),
        "paid_amount": _num(paid) if pays else None,
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
