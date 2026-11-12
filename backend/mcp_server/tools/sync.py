"""Bank data freshness, from a chat: when the bank data was last updated
(by Pluggy and by Securo) and a way to ask for fresh data.

Two clocks matter. Pluggy pulls from the bank on its own schedule
(`lastUpdatedAt`, next run at `nextAutoSyncAt`); Securo then reads what
Pluggy has (`last_sync_at`). MeuPluggy items (Open Finance consents shared
from the MeuPluggy app) can't be refreshed on demand: Securo's sync only
reads Pluggy's latest copy, so Pluggy's clock is the real "as of".
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from mcp_server.auth import CallContext, ToolAccessDenied
from mcp_server.registry import tool
from mcp_server.tools._helpers import resolve_workspace_id

logger = logging.getLogger(__name__)

# A connection synced this recently is left alone: the bank has nothing new
# and every provider call counts against its quota.
COOLDOWN = timedelta(minutes=15)
SYNC_TASK = "app.tasks.sync_tasks.sync_single_connection"
PROVIDER_TIMEOUT = 10.0
AGGREGATOR_NAMES = {"meupluggy"}


def bank_name(conn: BankConnection) -> str:
    """The bank a connection reaches. For an aggregator (MeuPluggy) the
    institution is the aggregator itself and the bank is in display_name."""
    if conn.display_name and (conn.institution_name or "").lower() in AGGREGATOR_NAMES:
        return conn.display_name.replace("MeuPluggy", "").strip() or conn.display_name
    return conn.display_name or conn.institution_name


async def bank_names(session: AsyncSession, connection_ids: Iterable[Any]) -> dict[uuid.UUID, str]:
    ids = {c for c in connection_ids if c}
    if not ids:
        return {}
    rows = (await session.execute(select(BankConnection).where(BankConnection.id.in_(ids)))).scalars().all()
    return {c.id: bank_name(c) for c in rows}


def _send(connection_id: str, user_id: str) -> None:
    from app.worker import celery_app

    celery_app.send_task(SYNC_TASK, args=[connection_id, user_id])


async def _provider_item(session: AsyncSession, conn: BankConnection) -> dict[str, Any]:
    """Pluggy's own view of the connection, read live. Never raises."""
    if conn.provider != "pluggy":
        return {"provider": conn.provider, "provider_status": "not_available"}
    item_id = (conn.credentials or {}).get("item_id")
    if not item_id:
        return {"provider_status": "not_available"}
    try:
        from app.providers.pluggy import PLUGGY_API_BASE
        from app.services.connection_service import resolve_provider

        provider = await resolve_provider(session, conn.provider, conn.user_id)
        headers = await provider._headers()
        async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
            r = await client.get(f"{PLUGGY_API_BASE}/items/{item_id}", headers=headers)
        r.raise_for_status()
        it = r.json() or {}
    except Exception as exc:  # noqa: BLE001 — a status read must not fail the tool
        logger.warning("Pluggy item read failed for connection %s: %s", conn.id, exc)
        return {"provider_status": "unreachable"}
    connector = ((it.get("connector") or {}).get("name") or "").lower()
    return {
        "provider_status": it.get("status"),
        "provider_execution": it.get("executionStatus"),
        "provider_last_updated_at": it.get("lastUpdatedAt"),
        "provider_next_auto_sync_at": it.get("nextAutoSyncAt"),
        "consent_expires_at": it.get("consentExpiresAt"),
        "on_demand_refresh": connector not in AGGREGATOR_NAMES,
    }


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _own_connections(session: AsyncSession, ctx: CallContext) -> list[BankConnection]:
    ws_id = await resolve_workspace_id(session, ctx)
    return list(
        (
            await session.execute(
                select(BankConnection)
                .where(BankConnection.workspace_id == ws_id, BankConnection.user_id == ctx.user_id)
                .order_by(BankConnection.created_at)
            )
        ).scalars().all()
    )


async def _status_row(session: AsyncSession, c: BankConnection) -> dict[str, Any]:
    last = _aware(c.last_sync_at)
    return {
        "connection_id": str(c.id),
        "bank": bank_name(c),
        "status": c.status,
        "securo_last_sync_at": last.isoformat() if last else None,
        **await _provider_item(session, c),
    }


_FRESHNESS_NOTE = (
    "provider_last_updated_at = when Pluggy last pulled from the bank (the data's real 'as of'); "
    "securo_last_sync_at = when Securo last read Pluggy. provider_next_auto_sync_at null means "
    "Pluggy has no automatic update scheduled for that bank. on_demand_refresh false (MeuPluggy) "
    "means a sync only re-reads Pluggy's latest copy; fresher bank data waits for Pluggy's next run. "
    "Times are UTC; convert to America/Sao_Paulo (UTC-3) when telling the user."
)


@tool(
    name="get_sync_status",
    description=(
        "When the user's bank data was last updated, per bank: when Pluggy "
        "last pulled from the bank, its next automatic update, connection "
        "health, consent expiry, and when Securo last imported it. Use for "
        "'qual a última atualização?', 'os dados estão atualizados?', or "
        "before answering about very recent charges. Read-only."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["read", "sync", "accounts"],
)
async def get_sync_status(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    rows = [await _status_row(session, c) for c in await _own_connections(session, ctx)]
    return {"connections": rows, "note": _FRESHNESS_NOTE}


@tool(
    name="sync_bank_connections",
    description=(
        "Import the latest bank data into Securo (all the user's bank "
        "connections) and report how fresh Pluggy's copy is. Use when the "
        "user asks to update/sync. Runs in the background for 1-2 minutes: "
        "tell the user, then query again — new data is not in this response. "
        "Connections synced in the last 15 minutes are skipped. For "
        "MeuPluggy banks this cannot make Pluggy fetch from the bank sooner; "
        "say so, with `provider_last_updated_at` and the next automatic run."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["sync", "accounts"],
)
async def sync_bank_connections(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    if not ctx.can_write:
        raise ToolAccessDenied("Read-only access cannot start a bank sync.")
    now = datetime.now(timezone.utc)
    out = []
    for c in await _own_connections(session, ctx):
        row = await _status_row(session, c)
        last = _aware(c.last_sync_at)
        recent = last is not None and now - last < COOLDOWN
        if not recent:
            _send(str(c.id), str(ctx.user_id))
        row["action"] = "skipped_recently_synced" if recent else "sync_queued"
        out.append(row)
    return {"connections": out, "note": "Query again in 1-2 minutes to see new data. " + _FRESHNESS_NOTE}
