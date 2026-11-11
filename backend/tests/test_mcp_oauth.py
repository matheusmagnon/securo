"""OAuth flow for remote MCP clients (mcp_server/oauth.py).

Drives the whole dance a client like ChatGPT does: 401 challenge →
metadata → registration → consent page → code → tokens → MCP call →
refresh, plus the refusals that keep it safe.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pyotp
import pytest

from app.core.database import get_async_session

REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


@pytest.fixture(autouse=True)
def _mcp_app(monkeypatch):
    from tests.conftest import TestSessionLocal, override_get_async_session
    import mcp_server.main as mcp_main
    import mcp_server.oauth as oauth

    monkeypatch.setattr(mcp_main, "async_session_maker", TestSessionLocal)
    mcp_main.app.dependency_overrides[get_async_session] = override_get_async_session
    oauth._failures.clear()
    yield mcp_main.app
    mcp_main.app.dependency_overrides.clear()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fin.test")


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _register(cli, redirect=REDIRECT):
    return await cli.post("/mcp/oauth/register", json={"client_name": "ChatGPT", "redirect_uris": [redirect]})


def _auth_params(client_id, challenge, **over):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "securo",
    }
    params.update(over)
    return params


async def _authorize(cli, client_id, challenge, *, password="testpass123", totp="", decision="allow"):
    form = _auth_params(client_id, challenge)
    form.update({"email": "test@example.com", "password": password, "totp": totp, "decision": decision})
    return await cli.post("/mcp/oauth/authorize", data=form)


def _query(response):
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


async def _full_flow(cli):
    client_id = (await _register(cli)).json()["client_id"]
    verifier, challenge = _pkce()
    r = await _authorize(cli, client_id, challenge)
    code = _query(r)["code"]
    tok = await cli.post("/mcp/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": client_id, "redirect_uri": REDIRECT,
    })
    return client_id, code, verifier, tok


@pytest.mark.asyncio
async def test_unauthenticated_mcp_points_to_resource_metadata(_mcp_app):
    async with _client(_mcp_app) as cli:
        r = await cli.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]
        meta = (await cli.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert meta["resource"].endswith("/mcp")
        server = (await cli.get("/.well-known/oauth-authorization-server")).json()
        assert server["issuer"] == meta["authorization_servers"][0]
        assert server["code_challenge_methods_supported"] == ["S256"]


@pytest.mark.asyncio
async def test_full_flow_gives_a_token_that_calls_tools_and_refreshes(_mcp_app, test_user):
    async with _client(_mcp_app) as cli:
        client_id, _, _, tok = await _full_flow(cli)
        assert tok.status_code == 200, tok.text
        body = tok.json()
        assert body["token_type"] == "Bearer" and body["refresh_token"]

        r = await cli.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_accounts", "arguments": {}}},
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is False

        again = await cli.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": client_id,
        })
        assert again.status_code == 200
        # Rotated: the old refresh token is dead.
        old = await cli.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": client_id,
        })
        assert old.status_code == 400 and old.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_code_is_single_use_and_needs_the_right_verifier(_mcp_app, test_user):
    async with _client(_mcp_app) as cli:
        client_id, code, verifier, tok = await _full_flow(cli)
        assert tok.status_code == 200
        reuse = await cli.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": client_id,
        })
        assert reuse.json()["error"] == "invalid_grant"

        _, challenge = _pkce()
        code2 = _query(await _authorize(cli, client_id, challenge))["code"]
        wrong = await cli.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code2, "code_verifier": verifier, "client_id": client_id,
        })
        assert wrong.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_wrong_password_shows_the_form_again_and_throttles(_mcp_app, test_user):
    async with _client(_mcp_app) as cli:
        client_id = (await _register(cli)).json()["client_id"]
        _, challenge = _pkce()
        for _ in range(5):
            r = await _authorize(cli, client_id, challenge, password="nope")
            assert r.status_code == 200 and "incorretos" in r.text
        r = await _authorize(cli, client_id, challenge)
        assert "Muitas tentativas" in r.text


@pytest.mark.asyncio
async def test_two_factor_is_required_when_enabled(_mcp_app, test_user, session):
    secret = pyotp.random_base32()
    test_user.is_2fa_enabled = True
    test_user.totp_secret = secret
    await session.commit()
    async with _client(_mcp_app) as cli:
        client_id = (await _register(cli)).json()["client_id"]
        _, challenge = _pkce()
        r = await _authorize(cli, client_id, challenge)
        assert "2FA" in r.text and r.status_code == 200
        r = await _authorize(cli, client_id, challenge, totp=pyotp.TOTP(secret).now())
        assert r.status_code == 303 and "code" in _query(r)


@pytest.mark.asyncio
async def test_deny_and_bad_requests(_mcp_app, test_user):
    async with _client(_mcp_app) as cli:
        # Unknown redirect host: refused at registration.
        bad = await _register(cli, "https://evil.example/cb")
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"

        client_id = (await _register(cli)).json()["client_id"]
        _, challenge = _pkce()
        denied = await _authorize(cli, client_id, challenge, decision="deny")
        assert denied.status_code == 303
        q = _query(denied)
        assert q["error"] == "access_denied" and q["state"] == "xyz"

        # Unregistered redirect URI: an error page, never a redirect.
        page = await cli.get("/mcp/oauth/authorize", params=_auth_params(client_id, challenge, redirect_uri="https://claude.ai/x"))
        assert page.status_code == 400 and "location" not in page.headers

        # No PKCE: sent back to the client as an OAuth error.
        nopkce = await cli.get("/mcp/oauth/authorize", params=_auth_params(client_id, challenge, code_challenge_method="plain"))
        assert nopkce.status_code == 303 and _query(nopkce)["error"] == "invalid_request"

        ok = await cli.get("/mcp/oauth/authorize", params=_auth_params(client_id, challenge))
        assert ok.status_code == 200 and "Conectar ChatGPT" in ok.text
        assert ok.headers["x-frame-options"] == "DENY"
