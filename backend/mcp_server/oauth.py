"""OAuth 2.1 authorization server for remote MCP clients (ChatGPT, Claude).

Those apps can't paste a token: they discover how to log in from the MCP
endpoint's 401, register themselves, send the user to a consent page and
exchange the resulting code for tokens. The pieces, per the MCP
authorization spec:

- `/.well-known/oauth-protected-resource[/mcp]` (RFC 9728) names this
  server as the authorization server for `/mcp`.
- `/.well-known/oauth-authorization-server` (RFC 8414) lists the endpoints.
- `POST /mcp/oauth/register` — dynamic client registration (RFC 7591).
  Public clients only; redirect hosts limited to `mcp_oauth_redirect_hosts`.
- `GET|POST /mcp/oauth/authorize` — a server-rendered page where the user
  signs in with their Securo email + password (+ TOTP when enabled) and
  approves the client. Authorization code + PKCE S256, nothing else.
- `POST /mcp/oauth/token` — code and refresh-token grants.

The access token is the same JWT the MCP server already verifies
(`mint_token(..., external=True)`), bound to the user's default workspace,
so tools see exactly what the user sees in the web app. Refresh tokens
rotate on each use and are stored only as SHA-256 hashes.
"""
from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import pyotp
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.config import get_agent_settings
from app.agents.mcp.auth import mint_token
from app.core.auth import UserManager
from app.core.config import get_settings
from app.core.database import get_async_session
from app.models.mcp_oauth import McpOAuthClient, McpOAuthGrant
from app.models.user import User
from app.services.workspace_service import get_default_workspace

router = APIRouter()

SCOPE = "securo"
CODE_TTL_SECONDS = 300
TOTP_VALID_WINDOW = 1
# Failed sign-ins tolerated per email and per IP within the window.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 900

_failures: dict[str, deque[float]] = defaultdict(deque)


# ------------------------------------------------------------------ helpers


def issuer() -> str:
    return get_settings().frontend_url.rstrip("/")


def resource_url() -> str:
    return f"{issuer()}/mcp"


def resource_metadata_url() -> str:
    return f"{issuer()}/.well-known/oauth-protected-resource/mcp"


def www_authenticate() -> str:
    return f'Bearer resource_metadata="{resource_metadata_url()}", scope="{SCOPE}"'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    # SQLite (tests) hands timestamps back naive; Postgres keeps the zone.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


def _allowed_redirect_hosts() -> set[str]:
    raw = get_agent_settings().mcp_oauth_redirect_hosts
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _redirect_uri_ok(uri: Any) -> bool:
    if not isinstance(uri, str) or len(uri) > 2000:
        return False
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.fragment or not host:
        return False
    if parts.scheme == "http" and host not in ("localhost", "127.0.0.1"):
        return False
    if parts.scheme not in ("https", "http"):
        return False
    return host in _allowed_redirect_hosts()


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def _throttled(*keys: str) -> bool:
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    for key in keys:
        q = _failures[key]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= MAX_FAILURES:
            return True
    return False


def _record_failure(*keys: str) -> None:
    now = time.monotonic()
    for key in keys:
        _failures[key].append(now)


def _token_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error, "error_description": description},
        headers={"Cache-Control": "no-store"},
    )


def _redirect_with(uri: str, params: dict[str, str]) -> RedirectResponse:
    sep = "&" if urlsplit(uri).query else "?"
    return RedirectResponse(f"{uri}{sep}{urlencode(params)}", status_code=303)


async def _load_client(session: AsyncSession, client_id: Any) -> Optional[McpOAuthClient]:
    try:
        cid = uuid.UUID(str(client_id))
    except (ValueError, TypeError):
        return None
    return await session.get(McpOAuthClient, cid)


# ----------------------------------------------------------------- metadata


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    return {
        "resource": resource_url(),
        "authorization_servers": [issuer()],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Securo",
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
async def authorization_server_metadata():
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/oauth/authorize",
        "token_endpoint": f"{base}/mcp/oauth/token",
        "registration_endpoint": f"{base}/mcp/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        "authorization_response_iss_parameter_supported": True,
    }


# ------------------------------------------------------------- registration


@router.post("/mcp/oauth/register")
async def register_client(request: Request, session: AsyncSession = Depends(get_async_session)):
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _token_error("invalid_client_metadata", "body must be a JSON object")

    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 10:
        return _token_error("invalid_redirect_uri", "redirect_uris must list 1 to 10 URIs")
    bad = [u for u in uris if not _redirect_uri_ok(u)]
    if bad:
        return _token_error("invalid_redirect_uri", f"redirect URI not allowed: {bad[0]}")

    auth_method = body.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        # Only public clients: a secret would add nothing PKCE doesn't
        # already give, and we'd have to store it.
        return _token_error("invalid_client_metadata", "only token_endpoint_auth_method=none is supported")

    name = body.get("client_name")
    name = name.strip()[:200] if isinstance(name, str) and name.strip() else "Cliente MCP"

    client = McpOAuthClient(id=uuid.uuid4(), client_name=name, redirect_uris=list(uris), created_at=_now())
    session.add(client)
    await session.commit()
    return JSONResponse(
        status_code=201,
        content={
            "client_id": str(client.id),
            "client_id_issued_at": int(time.time()),
            "client_name": name,
            "redirect_uris": list(uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
    )


# ------------------------------------------------------------ authorization


_AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state", "scope", "resource")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --fg:#14171c; --muted:#5d6672; --border:#d9dde3; --accent:#14171c; --accent-fg:#fff; --danger:#b42318; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --fg:#eef0f3; --muted:#9aa3ae; --border:#2b3038; --accent:#eef0f3; --accent-fg:#0f1115; --danger:#f97066; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:16px; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ width:100%; max-width:400px; background:var(--card); border:1px solid var(--border); border-radius:14px; padding:28px 24px; }}
h1 {{ font-size:19px; margin:0 0 6px; }}
p {{ margin:0 0 14px; color:var(--muted); }}
ul {{ margin:0 0 18px; padding-left:18px; color:var(--muted); }}
label {{ display:block; font-size:13px; font-weight:600; margin:12px 0 4px; }}
input {{ width:100%; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:transparent; color:var(--fg); font:inherit; }}
.row {{ display:flex; gap:10px; margin-top:20px; }}
button {{ flex:1; padding:11px; border-radius:8px; font:inherit; font-weight:600; cursor:pointer; border:1px solid var(--border); background:transparent; color:var(--fg); }}
button.primary {{ background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }}
.error {{ color:var(--danger); font-weight:600; }}
.host {{ font-family:ui-monospace,monospace; font-size:13px; color:var(--fg); }}
</style></head><body><main>{body}</main></body></html>"""
    return HTMLResponse(
        doc,
        status_code=status,
        headers={
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


def _error_page(message: str) -> HTMLResponse:
    return _page("Securo", f"<h1>Não foi possível conectar</h1><p>{html.escape(message)}</p>", status=400)


def _consent_page(client: McpOAuthClient, params: dict[str, str], *, error: str = "", email: str = "") -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in params.items() if v
    )
    name = html.escape(client.client_name)
    host = html.escape(urlsplit(params["redirect_uri"]).hostname or "")
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    body = f"""
<h1>Conectar {name} ao Securo</h1>
<p>Depois de autorizar, <strong>{name}</strong> poderá:</p>
<ul>
<li>consultar suas contas, transações, orçamentos e a divisão da Casa;</li>
<li>propor lançamentos e alterações, que só são gravados quando você confirmar na conversa.</li>
</ul>
<p>Retorno para <span class="host">{host}</span></p>
{err}
<form method="post" action="/mcp/oauth/authorize" autocomplete="on">
{hidden}
<label for="email">E-mail</label>
<input id="email" name="email" type="email" autocomplete="username" required value="{html.escape(email)}">
<label for="password">Senha</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<label for="totp">Código 2FA <span style="font-weight:400">(se estiver ativado)</span></label>
<input id="totp" name="totp" inputmode="numeric" autocomplete="one-time-code" maxlength="8">
<div class="row">
<button type="submit" name="decision" value="deny" formnovalidate>Cancelar</button>
<button type="submit" name="decision" value="allow" class="primary">Autorizar</button>
</div>
</form>"""
    return _page(f"Conectar {client.client_name}", body)


async def _validate_auth_request(
    session: AsyncSession, params: dict[str, str]
) -> tuple[Optional[McpOAuthClient], Optional[Response]]:
    """Checks that must pass before anything is sent to the redirect URI.

    A bad client or redirect URI gets an error page, never a redirect: that
    would turn this endpoint into an open redirector. Once both are known
    good, other errors go back to the client as `error=` per RFC 6749.
    """
    client = await _load_client(session, params.get("client_id"))
    if client is None:
        return None, _error_page("Aplicativo desconhecido. Remova o conector e adicione de novo.")
    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri not in (client.redirect_uris or []):
        return None, _error_page("Endereço de retorno não registrado para este aplicativo.")

    def back(error: str, desc: str) -> Response:
        out = {"error": error, "error_description": desc, "iss": issuer()}
        if params.get("state"):
            out["state"] = params["state"]
        return _redirect_with(redirect_uri, out)

    if params.get("response_type") != "code":
        return None, back("unsupported_response_type", "only response_type=code is supported")
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return None, back("invalid_request", "PKCE with code_challenge_method=S256 is required")
    if not 43 <= len(params["code_challenge"]) <= 128:
        return None, back("invalid_request", "malformed code_challenge")
    return client, None


@router.get("/mcp/oauth/authorize")
async def authorize_page(request: Request, session: AsyncSession = Depends(get_async_session)):
    params = {k: request.query_params.get(k, "") for k in _AUTH_PARAMS}
    client, problem = await _validate_auth_request(session, params)
    if problem is not None:
        return problem
    assert client is not None
    return _consent_page(client, params)


@router.post("/mcp/oauth/authorize")
async def authorize_submit(request: Request, session: AsyncSession = Depends(get_async_session)):
    form = await request.form()
    params = {k: str(form.get(k) or "") for k in _AUTH_PARAMS}
    client, problem = await _validate_auth_request(session, params)
    if problem is not None:
        return problem
    assert client is not None
    redirect_uri = params["redirect_uri"]
    state = {"state": params["state"]} if params["state"] else {}

    if form.get("decision") != "allow":
        return _redirect_with(redirect_uri, {"error": "access_denied", "iss": issuer(), **state})

    if not get_settings().local_auth_enabled:
        return _error_page("O login por senha está desativado neste Securo.")

    email = str(form.get("email") or "").strip()
    password = str(form.get("password") or "")
    totp = str(form.get("totp") or "").strip().replace(" ", "")
    keys = (f"email:{email.lower()}", f"ip:{_client_ip(request)}")
    if _throttled(*keys):
        return _consent_page(client, params, email=email, error="Muitas tentativas. Aguarde 15 minutos.")

    manager = UserManager(SQLAlchemyUserDatabase(session, User))
    user = await manager.authenticate(SimpleNamespace(username=email, password=password))
    if user is None or not user.is_active:
        _record_failure(*keys)
        return _consent_page(client, params, email=email, error="E-mail ou senha incorretos.")
    if user.is_2fa_enabled and user.totp_secret:
        if not totp or not pyotp.TOTP(user.totp_secret).verify(totp, valid_window=TOTP_VALID_WINDOW):
            _record_failure(*keys)
            return _consent_page(client, params, email=email, error="Código 2FA inválido.")

    workspace = await get_default_workspace(session, user.id)
    if workspace is None:
        return _error_page("Sua conta não tem um espaço de trabalho.")

    code = secrets.token_urlsafe(32)
    session.add(
        McpOAuthGrant(
            id=uuid.uuid4(),
            client_id=client.id,
            user_id=user.id,
            workspace_id=workspace.id,
            redirect_uri=redirect_uri,
            code_hash=_hash(code),
            code_challenge=params["code_challenge"],
            code_expires_at=_now() + timedelta(seconds=CODE_TTL_SECONDS),
            created_at=_now(),
        )
    )
    await session.commit()
    return _redirect_with(redirect_uri, {"code": code, "iss": issuer(), **state})


# -------------------------------------------------------------------- token


async def _issue_tokens(session: AsyncSession, grant: McpOAuthGrant) -> JSONResponse:
    s = get_agent_settings()
    refresh = secrets.token_urlsafe(48)
    grant.refresh_hash = _hash(refresh)
    grant.refresh_expires_at = _now() + timedelta(days=max(s.mcp_oauth_refresh_ttl_days, 1))
    grant.last_used_at = _now()
    await session.commit()
    access = mint_token(
        user_id=grant.user_id,
        workspace_id=grant.workspace_id,
        ttl_seconds=s.mcp_oauth_access_ttl_seconds,
        external=True,
    )
    return JSONResponse(
        content={
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": s.mcp_oauth_access_ttl_seconds,
            "refresh_token": refresh,
            "scope": SCOPE,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _user_still_active(session: AsyncSession, user_id: uuid.UUID) -> bool:
    user = await session.get(User, user_id)
    return bool(user and user.is_active)


@router.post("/mcp/oauth/token")
async def token(request: Request, session: AsyncSession = Depends(get_async_session)):
    form = await request.form()
    grant_type = form.get("grant_type")
    client = await _load_client(session, form.get("client_id"))
    if client is None:
        return _token_error("invalid_client", "unknown client_id", status=401)

    if grant_type == "authorization_code":
        code = str(form.get("code") or "")
        verifier = str(form.get("code_verifier") or "")
        if not code or not verifier:
            return _token_error("invalid_request", "code and code_verifier are required")
        grant = await session.scalar(select(McpOAuthGrant).where(McpOAuthGrant.code_hash == _hash(code)))
        if grant is None or grant.client_id != client.id or grant.revoked_at is not None:
            return _token_error("invalid_grant", "unknown or used code")
        # Burn the code whatever happens next: a code is good for one try.
        challenge = grant.code_challenge or ""
        expires = _aware(grant.code_expires_at)
        grant.code_hash = None
        grant.code_challenge = None
        grant.code_expires_at = None
        await session.commit()
        if expires is None or expires < _now():
            return _token_error("invalid_grant", "code expired")
        if str(form.get("redirect_uri") or grant.redirect_uri) != grant.redirect_uri:
            return _token_error("invalid_grant", "redirect_uri mismatch")
        if not _pkce_matches(verifier, challenge):
            return _token_error("invalid_grant", "PKCE verification failed")
        if not await _user_still_active(session, grant.user_id):
            return _token_error("invalid_grant", "user inactive")
        return await _issue_tokens(session, grant)

    if grant_type == "refresh_token":
        refresh = str(form.get("refresh_token") or "")
        grant = await session.scalar(
            select(McpOAuthGrant).where(McpOAuthGrant.refresh_hash == _hash(refresh))
        ) if refresh else None
        if grant is None or grant.client_id != client.id or grant.revoked_at is not None:
            return _token_error("invalid_grant", "unknown refresh token")
        expires = _aware(grant.refresh_expires_at)
        if expires is None or expires < _now():
            return _token_error("invalid_grant", "refresh token expired")
        if not await _user_still_active(session, grant.user_id):
            return _token_error("invalid_grant", "user inactive")
        return await _issue_tokens(session, grant)

    return _token_error("unsupported_grant_type", "use authorization_code or refresh_token")
