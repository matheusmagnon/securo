"""Read-only group + member + balance + settlement exposure for the agent.

Splits are written through `propose_create_transaction` (with its
`group_id` + `splits` parameters) — there's no `propose_create_split`
tool because splits live attached to the parent transaction, not as
standalone rows.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import balance_service, group_service, settlement_service
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, parse_uuid, resolve_workspace_id


@tool(
    name="list_groups",
    description=(
        "List the user's expense-sharing groups (Splitwise-style: 'Amigos', "
        "'Roommates', etc.) along with their members. Returns each group "
        "with `members: [{id, name, is_self}]` so a single call gives the "
        "model everything it needs to propose a transaction with equal/"
        "exact/percent splits. The `is_self` flag marks the member that "
        "represents the user (used to compute who-owes-who balances)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_archived": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def list_groups(
    *,
    session: AsyncSession,
    ctx: CallContext,
    include_archived: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    groups = await group_service.list_groups(session, ws_id, ctx.user_id, include_archived=include_archived)
    return {
        "items": [
            {
                "id": str(g.id),
                "name": g.name,
                "kind": g.kind,
                "default_currency": g.default_currency,
                "is_archived": bool(g.is_archived),
                "members": [
                    {
                        "id": str(m.id),
                        "name": m.name,
                        "is_self": bool(m.is_self),
                    }
                    for m in (g.members or [])
                ],
            }
            for g in groups
        ],
        "total": len(groups),
    }


@tool(
    name="get_group_balances",
    description=(
        "Compute the who-owes-who balance for one expense-sharing group. "
        "Returns lines per member: positive `amount` means the member "
        "OWES the user (self) that much in `currency`; negative means the "
        "user owes them. `amount_in_default_currency` is the value "
        "converted to the group's default currency for a single bottom "
        "line. Already accounts for past `group_settlements` (payments "
        "that closed previous balances). Use this for 'quem ainda me "
        "deve?', 'estamos quites?', 'qual o saldo do grupo Amigos?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "format": "uuid"},
        },
        "required": ["group_id"],
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def get_group_balances(
    *,
    session: AsyncSession,
    ctx: CallContext,
    group_id: str,
) -> dict[str, Any]:
    gid = parse_uuid(group_id)
    if gid is None:
        return dict(error="group not found or not visible to this user")


    ws_id = await resolve_workspace_id(session, ctx)
    result = await balance_service.compute_balances(session, gid, ws_id, ctx.user_id)
    if result is None:
        return {"error": "group not found or not visible to this user"}

    # Resolve member names so the agent doesn't need a second list_groups
    # call to humanize the output.
    from sqlalchemy import select
    from app.models.group import GroupMember
    rows = (await session.execute(
        select(GroupMember).where(GroupMember.group_id == gid)
    )).scalars().all()
    name_by_id = {m.id: m.name for m in rows}
    is_self_by_id = {m.id: bool(m.is_self) for m in rows}

    lines = []
    for ln in result.get("lines", []):
        mid = ln["member_id"]
        lines.append({
            "member_id": str(mid),
            "member_name": name_by_id.get(mid),
            "is_self": is_self_by_id.get(mid, False),
            "currency": ln["currency"],
            "amount": num(ln["amount"]),
            "amount_in_default_currency": num(ln.get("amount_in_default_currency")),
            "direction": _direction(ln["amount"]),
        })
    return {
        "group_id": str(result["group_id"]),
        "self_member_id": str(result["self_member_id"]) if result.get("self_member_id") else None,
        "default_currency": result.get("default_currency"),
        "lines": lines,
    }


@tool(
    name="list_group_settlements",
    description=(
        "List the recorded settlements (payments between members that "
        "close out balances) for one group, newest first. Each row has "
        "{from_member_id, to_member_id, amount, currency, date, notes}. "
        "Pair with `get_group_balances` when the user asks 'quem já me "
        "pagou?' or 'qual o histórico de acertos?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "format": "uuid"},
        },
        "required": ["group_id"],
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def list_group_settlements(
    *,
    session: AsyncSession,
    ctx: CallContext,
    group_id: str,
) -> dict[str, Any]:
    gid = parse_uuid(group_id)
    if gid is None:
        return dict(error="group not found or not visible to this user")

    ws_id = await resolve_workspace_id(session, ctx)
    rows = await settlement_service.list_settlements(session, gid, ws_id, ctx.user_id)
    if rows is None:
        return {"error": "group not found or not visible to this user"}

    items = [
        {
            "id": str(s.id),
            "group_id": str(s.group_id),
            "from_member_id": str(s.from_member_id),
            "to_member_id": str(s.to_member_id),
            "amount": num(s.amount),
            "currency": s.currency,
            "date": s.date.isoformat() if s.date else None,
            "notes": getattr(s, "notes", None),
            "transaction_id": str(s.transaction_id) if getattr(s, "transaction_id", None) else None,
        }
        for s in rows
    ]
    return {"items": items, "total": len(items)}


def _direction(amount: Any) -> str:
    """The sign of a balance line, spelled out so it can't be read backwards."""
    value = Decimal(str(amount or 0))
    if value > 0:
        return "member_owes_you"
    if value < 0:
        return "you_owe_member"
    return "settled"


@tool(
    name="get_group_month_overview",
    description=(
        "One month of an expense-sharing group, as the app's 'Casa' page "
        "shows it: total cost, how much each member paid (`paid_by`) and "
        "should bear (`share_of`), every shared item with its payer and "
        "status, the month's recurring bills with paid/unpaid and due date, "
        "long contracts (financing, installments) with progress, and the "
        "balance. Use this first for 'como estão as contas da casa este "
        "mês?', 'o que falta pagar?', 'quem pagou o quê?'. Item status: "
        "paid; pending = charged on a card bill that is still open (NOT "
        "unpaid by the member); upcoming = dated later this month; planned "
        "= a recurring bill not paid yet. `balance` is CUMULATIVE across all "
        "months net of recorded settlements (what is owed now); "
        "`upcoming_balance` adds charges dated after today (future card "
        "installments). Each line says `direction` explicitly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "format": "uuid"},
            "month": {"type": "string", "pattern": "^\\d{4}-(0[1-9]|1[0-2])$", "description": "YYYY-MM; defaults to the current month."},
        },
        "required": ["group_id"],
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def get_group_month_overview(
    *,
    session: AsyncSession,
    ctx: CallContext,
    group_id: str,
    month: str | None = None,
) -> dict[str, Any]:
    from datetime import date as _date

    from app.core.app_clock import get_workspace_timezone, today_in
    from app.schemas.group import GroupOverview
    from app.services import group_overview_service

    gid = parse_uuid(group_id)
    if gid is None:
        return {"error": "group not found or not visible to this user"}
    ws_id = await resolve_workspace_id(session, ctx)
    if month:
        try:
            year, mon = (int(part) for part in month.split("-"))
            first = _date(year, mon, 1)
        except ValueError:
            return {"error": "month must be YYYY-MM"}
    else:
        first = today_in(await get_workspace_timezone(session, ws_id)).replace(day=1)
    raw = await group_overview_service.compute_overview(session, gid, ws_id, ctx.user_id, first)
    if raw is None:
        return {"error": "group not found or not visible to this user"}
    ov = GroupOverview.model_validate(raw).model_dump(mode="json")
    names = {m["id"]: m["name"] for m in ov["members"]}
    self_id = ov.get("self_member_id")

    def who(mid: Any) -> Any:
        if mid is None:
            return None
        return f"{names.get(mid, '?')} (you)" if mid == self_id else names.get(mid)

    def lines(rows: list[dict]) -> list[dict]:
        return [
            {"member": who(r["member_id"]), "currency": r["currency"], "amount": r["amount"], "direction": _direction(r["amount"])}
            for r in rows
        ]

    return {
        "month": ov["month"],
        "currency": ov["default_currency"],
        "you": who(self_id),
        "total": ov["total"],
        "paid_by": [{"member": who(r["member_id"]), "amount": r["amount"]} for r in ov["paid_by"]],
        "share_of": [{"member": who(r["member_id"]), "amount": r["amount"]} for r in ov["share_of"]],
        "items": [
            {
                "date": i["date"], "description": i["description"], "category": i["category_name"],
                "type": i["type"], "amount": i["amount"], "your_share": i["my_share"],
                "paid_by": who(i["payer_member_id"]), "status": i["status"], "recurring": i["recurring"],
                "transaction_id": i["transaction_id"],
            }
            for i in ov["items"]
        ],
        "bills": [
            {
                "description": b["description"], "due_date": b["due_date"], "amount": b["amount"],
                "paid": b["paid"], "paid_date": b["paid_date"], "payer": who(b["payer_member_id"]),
                "installment": f"{b['number']}/{b['total']}" if b.get("total") else None,
            }
            for b in ov["bills"]
        ],
        "contracts": [
            {
                "description": c["description"], "amount": c["amount"], "frequency": c["frequency"],
                "payer": who(c["payer_member_id"]), "done": c["done"], "total": c["total"],
                "remaining_amount": c["remaining_amount"], "next_date": c["next_date"], "end_date": c["end_date"],
            }
            for c in ov["contracts"]
        ],
        "balance": lines(ov["balance_lines"]),
        "upcoming_balance": lines(ov["upcoming_lines"]),
    }
