"""Ask the bank for fresh data, from a chat.

The sync itself runs in the Celery worker, as the hourly one does; the tool
only queues it and reports when each connection last synced, so the model
can tell the user to ask again in a minute or two.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from mcp_server.auth import CallContext, ToolAccessDenied
from mcp_server.registry import tool
from mcp_server.tools._helpers import resolve_workspace_id

# A connection synced this recently is left alone: the bank has nothing new
# and every provider refresh counts against its quota.
COOLDOWN = timedelta(minutes=15)
SYNC_TASK = "app.tasks.sync_tasks.sync_single_connection"


def _send(connection_id: str, user_id: str) -> None:
    from app.worker import celery_app

    celery_app.send_task(SYNC_TASK, args=[connection_id, user_id])


@tool(
    name="sync_bank_connections",
    description=(
        "Fetch fresh data from the user's banks (all their bank connections). "
        "Use when the user asks to update/sync, or when data looks stale "
        "(check `last_sync_at`). The sync runs in the background and takes "
        "about 1-2 minutes: after calling, tell the user and query again "
        "afterwards — results are not in this response. Connections synced "
        "in the last 15 minutes are skipped."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["sync", "accounts"],
)
async def sync_bank_connections(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    if not ctx.can_write:
        raise ToolAccessDenied("Read-only access cannot start a bank sync.")
    ws_id = await resolve_workspace_id(session, ctx)
    rows = (
        await session.execute(
            select(BankConnection).where(
                BankConnection.workspace_id == ws_id,
                BankConnection.user_id == ctx.user_id,
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    out = []
    for c in rows:
        last = c.last_sync_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        recent = last is not None and now - last < COOLDOWN
        if not recent:
            _send(str(c.id), str(ctx.user_id))
        out.append({
            "connection_id": str(c.id),
            "institution": c.institution_name,
            "status": c.status,
            "last_sync_at": last.isoformat() if last else None,
            "action": "skipped_recently_synced" if recent else "sync_queued",
        })
    return {"connections": out, "note": "Query again in 1-2 minutes to see new data."}
