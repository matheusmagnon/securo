"""Share an existing transaction with an expense-sharing group (or stop).

The app does this from the transaction dialog ("dividir com o grupo"); the
MCP tools could only create a NEW transaction already split, so a model
asked to put an existing card charge into "Casa" had to refuse or risk a
duplicate. This tool edits the existing row's splits through the same
services the app uses, including "this and the next installments".

The preview runs the real change inside a SAVEPOINT, reads the group
balance before and after, and rolls back: what the user confirms is
exactly what `apply: true` will do.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import balance_service, group_service, installment_split_service, split_service
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, parse_uuid, resolve_workspace_id
from mcp_server.tools.bills import load_cycles
from mcp_server.tools.groups import _direction
from mcp_server.tools.proposals import _APPLY_FIELD, _PROPOSAL_PREFACE, _can_apply

SCOPES = ("this", "this_and_next", "all_installments")


def _installment(tx: Transaction) -> Optional[str]:
    if tx.installment_number and tx.total_installments:
        return f"{tx.installment_number}/{tx.total_installments}"
    return None


async def _splits_of(session: AsyncSession, tx_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[tuple[uuid.UUID, Decimal]]]:
    rows = (
        await session.execute(
            select(TransactionSplit.transaction_id, TransactionSplit.group_member_id, TransactionSplit.share_amount)
            .where(TransactionSplit.transaction_id.in_(tx_ids))
            .order_by(TransactionSplit.created_at)
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal]]] = {}
    for tid, mid, amount in rows:
        out.setdefault(tid, []).append((mid, Decimal(str(amount))))
    return out


async def _balance(session, gid, ws_id, user_id) -> tuple[dict[str, dict[uuid.UUID, Decimal]], Optional[uuid.UUID]]:
    res = await balance_service.compute_balances(session, gid, ws_id, user_id) or {}
    out: dict[str, dict[uuid.UUID, Decimal]] = {"balance": {}, "upcoming_balance": {}}
    for key, src in (("balance", "lines"), ("upcoming_balance", "upcoming_lines")):
        for ln in res.get(src, []):
            amt = Decimal(str(ln.get("amount_in_default_currency") if ln.get("amount_in_default_currency") is not None else ln["amount"]))
            out[key][ln["member_id"]] = out[key].get(ln["member_id"], Decimal("0")) + amt
    return out, res.get("self_member_id")


@tool(
    name="propose_split_transaction",
    description=_PROPOSAL_PREFACE
    + (
        "Share an EXISTING transaction (e.g. a card charge) with an "
        "expense-sharing group such as 'Casa', change how it is shared, or "
        "take it out of the group (`remove: true`). Use this instead of "
        "creating a new transaction, which would duplicate the expense. "
        "Defaults: the user's only group, all members, equal shares. "
        "Installments: the amount shown is ONE parcel. `scope` = 'this' "
        "(only this parcel), 'this_and_next' (this and later parcels, "
        "including ones future syncs bring) or 'all_installments' (from "
        "parcel 1, so earlier parcels on already-paid bills too). For an "
        "installment, ASK the user which scope before previewing. The "
        "preview lists every parcel affected with its bill, each member's "
        "share per parcel and in total, and the group balance BEFORE and "
        "AFTER (`balance_change`). Always show the user that before/after "
        "picture (amount per parcel x parcels = total per person) and get "
        "an explicit yes before calling again with apply=true."
    ),
    parameters={
        "type": "object",
        "properties": {
            "transaction_id": {"type": "string", "format": "uuid"},
            "group_id": {"type": "string", "format": "uuid", "description": "Omit when the user has a single group."},
            "share_type": {"type": "string", "enum": ["equal", "percent", "exact"], "default": "equal"},
            "members": {
                "type": "array",
                "description": "Members to share with (ids from list_groups). Omit for every member. "
                "share_pct is required for 'percent' (sum 100); share_amount for 'exact' (sum = one parcel's amount).",
                "items": {
                    "type": "object",
                    "properties": {
                        "member_id": {"type": "string", "format": "uuid"},
                        "share_pct": {"type": "number"},
                        "share_amount": {"type": "number"},
                    },
                    "required": ["member_id"],
                    "additionalProperties": False,
                },
            },
            "scope": {"type": "string", "enum": list(SCOPES), "default": "this"},
            "remove": {"type": "boolean", "default": False, "description": "Take the transaction(s) out of the group instead."},
            "apply": _APPLY_FIELD,
        },
        "required": ["transaction_id"],
        "additionalProperties": False,
    },
    is_proposal=True,
    tags=["propose", "groups", "transactions"],
)
async def propose_split_transaction(
    *,
    session: AsyncSession,
    ctx: CallContext,
    transaction_id: str,
    group_id: str | None = None,
    share_type: str = "equal",
    members: list[dict[str, Any]] | None = None,
    scope: str = "this",
    remove: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    tid = parse_uuid(transaction_id)
    tx = (
        await session.execute(select(Transaction).where(Transaction.id == tid, Transaction.workspace_id == ws_id))
    ).scalar_one_or_none() if tid else None
    if tx is None:
        return {"error": "transaction not found"}
    if tx.transfer_pair_id is not None:
        return {"error": "a transfer between accounts cannot be shared with a group"}
    if tx.has_category_lines:
        return {"error": "this transaction is split across categories; undo that in the app before sharing it"}
    if scope not in SCOPES:
        return {"error": f"scope must be one of {', '.join(SCOPES)}"}
    if share_type not in ("equal", "percent", "exact"):
        return {"error": "share_type must be equal, percent or exact"}

    notes: list[str] = []
    is_inst = installment_split_service.is_installment(tx)
    if not is_inst and scope != "this":
        notes.append("Not an installment purchase: only this transaction is affected.")
        scope = "this"

    # Parcels touched, and the parcel the change is anchored on.
    every = await installment_split_service._series(session, tx, 1) if is_inst else [tx]
    if scope == "this":
        anchor, parcels = tx, [tx]
    elif scope == "this_and_next":
        anchor, parcels = tx, [p for p in every if p.installment_number >= tx.installment_number]
    else:
        anchor, parcels = every[0], every
    others = [p for p in every if p.id not in {q.id for q in parcels}]

    current = await _splits_of(session, [p.id for p in every])

    # The group.
    all_members: dict[uuid.UUID, GroupMember] = {}
    if remove:
        mids = {m for p in parcels for m, _ in current.get(p.id, [])}
        if not mids:
            return {"error": "not shared with any group, nothing to remove"}
        gm = (await session.execute(select(GroupMember).where(GroupMember.id.in_(mids)))).scalars().first()
        gid = gm.group_id
    elif group_id:
        gid = parse_uuid(group_id)
    else:
        groups = [g for g in await group_service.list_groups(session, ws_id, ctx.user_id) if not g.is_archived]
        if len(groups) != 1:
            return {
                "error": "pass group_id",
                "groups": [{"id": str(g.id), "name": g.name} for g in groups],
            }
        gid = groups[0].id
    group = await group_service.get_group_visible(session, gid, ws_id, ctx.user_id) if gid else None
    if group is None:
        return {"error": "group not found or not visible to this user"}
    all_members = {
        m.id: m for m in (await session.execute(select(GroupMember).where(GroupMember.group_id == gid))).scalars().all()
    }

    # The payload, as the app's dialog would send it.
    if remove:
        payload = TransactionSplitsInput(share_type="equal", splits=[])
    else:
        chosen = members or [{"member_id": str(mid)} for mid in all_members]
        try:
            entries = [
                TransactionSplitInput(
                    group_member_id=uuid.UUID(str(m["member_id"])),
                    share_pct=Decimal(str(m["share_pct"])) if m.get("share_pct") is not None else None,
                    share_amount=Decimal(str(m["share_amount"])) if m.get("share_amount") is not None else None,
                )
                for m in chosen
            ]
        except (ValueError, KeyError):
            return {"error": "invalid members"}
        if any(e.group_member_id not in all_members for e in entries):
            return {"error": "one or more members do not belong to this group"}
        payload = TransactionSplitsInput(share_type=share_type, splits=entries)

    # Snapshot everything shown before the savepoint can expire the rows.
    cycles = await load_cycles(session, {p.account_id for p in every})

    def describe(p: Transaction) -> dict[str, Any]:
        cyc = cycles.get(p.account_id)
        return {
            "transaction_id": str(p.id),
            "description": p.description,
            "date": p.date.isoformat() if p.date else None,
            "amount": num(p.amount),
            "installment": _installment(p),
            "credit_card_bill": cyc.label(p) if cyc else None,
        }

    parcel_info = {p.id: describe(p) for p in every}
    order = [p.id for p in parcels]
    other_ids = [p.id for p in others]
    anchor_id = tx.id

    names = {mid: m.name for mid, m in all_members.items()}
    group_name = group.name
    before, self_id = await _balance(session, gid, ws_id, ctx.user_id)

    def who(mid: uuid.UUID) -> str:
        n = names.get(mid, "?")
        return f"{n} (you)" if mid == self_id else n

    writing = _can_apply(ctx, apply)
    try:
        sp = await session.begin_nested()
        try:
            if scope == "this":
                await split_service.replace_splits(session, anchor, payload, ctx.user_id)
            else:
                await installment_split_service.set_series_split(session, anchor, payload, ctx.user_id)
            await session.flush()
            after_splits = await _splits_of(session, order)
            after, _ = await _balance(session, gid, ws_id, ctx.user_id)
        except Exception:
            await sp.rollback()
            raise
        if writing:
            await sp.commit()
            await session.commit()
        else:
            await sp.rollback()
    except ValueError as e:
        return {"error": str(e)}

    def shares(rows: list[tuple[uuid.UUID, Decimal]]) -> Optional[list[dict[str, Any]]]:
        return [{"member": who(m), "share": num(a)} for m, a in rows] or None

    totals: dict[uuid.UUID, Decimal] = {}
    parcels_out = []
    for pid in order:
        new = after_splits.get(pid, [])
        for m, a in new:
            totals[m] = totals.get(m, Decimal("0")) + a
        parcels_out.append({
            **parcel_info[pid],
            "current_split": shares(current.get(pid, [])),
            "new_split": shares(new),
        })

    def lines(snap: dict[uuid.UUID, Decimal]) -> list[dict[str, Any]]:
        return [{"member": who(m), "amount": num(a), "direction": _direction(a)} for m, a in snap.items() if a]

    change = []
    for key in ("balance", "upcoming_balance"):
        for m in sorted(set(before[key]) | set(after[key]), key=str):
            b, a = before[key].get(m, Decimal("0")), after[key].get(m, Decimal("0"))
            if a != b:
                change.append({
                    "ledger": key, "member": who(m),
                    "before": num(b), "before_direction": _direction(b),
                    "after": num(a), "after_direction": _direction(a),
                })

    warnings = []
    paid_bills = [
        parcel_info[pid]["installment"] or parcel_info[pid]["date"]
        for pid in order
        if (parcel_info[pid]["credit_card_bill"] or {}).get("bill") == "closed"
    ]
    if paid_bills:
        warnings.append(
            f"Parcels on bills that already closed: {', '.join(paid_bills)}. Including them changes the cumulative "
            "balance; leave them out if they were already settled outside the app."
        )
    if any(current.get(pid) for pid in order):
        warnings.append("Some parcels are already shared; their split will be replaced.")
    unshared_others = [parcel_info[p]["installment"] for p in other_ids if not current.get(p)]
    if unshared_others and not remove:
        warnings.append(f"Not affected and not shared: parcels {', '.join(unshared_others)}.")
    if scope != "this" and not remove:
        notes.append("Later parcels that future syncs bring will be shared the same way.")

    preview = {
        "kind": "split_transaction",
        "action": "remove_from_group" if remove else "share_with_group",
        "group": {"id": str(gid), "name": group_name},
        "scope": scope,
        "share_type": None if remove else share_type,
        "anchor_transaction_id": str(anchor_id),
        "parcel_amount": parcel_info[anchor_id]["amount"],
        "parcels_affected": len(order),
        "parcels": parcels_out,
        "total_per_member": [{"member": who(m), "total": num(a)} for m, a in totals.items()],
        "balance_before": {k: lines(v) for k, v in before.items()},
        "balance_after": {k: lines(v) for k, v in after.items()},
        "balance_change": change,
        "warnings": warnings,
        "notes": notes,
    }
    if writing:
        return {**preview, "applied": True}
    return preview
