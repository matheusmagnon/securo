"""Per-user provider credentials (bring your own Pluggy application)."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from app.models.provider_credential import ProviderCredential
from app.models.user import User
from app.providers.pluggy import PluggyProvider
from app.services import connection_service, provider_credential_service

AUTH = "app.providers.pluggy.PluggyProvider.authenticate"


def _rejected(status: int = 401) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.pluggy.ai/auth")
    return httpx.HTTPStatusError("rejected", request=request, response=httpx.Response(status, request=request))


@pytest.mark.asyncio
async def test_save_encrypts_secret_and_resolves_user_provider(session: AsyncSession, test_user: User):
    with patch(AUTH, new=AsyncMock(return_value="key")):
        await provider_credential_service.save_credential(
            session, test_user.id, "pluggy", " my-client ", " my-secret "
        )

    row = await provider_credential_service.get_credential(session, test_user.id, "pluggy")
    assert row.client_id == "my-client"
    assert "my-secret" not in row.client_secret_encrypted

    provider = await provider_credential_service.resolve_provider(session, "pluggy", test_user.id)
    assert isinstance(provider, PluggyProvider)
    assert provider._client_id == "my-client"
    assert provider._client_secret == "my-secret"


@pytest.mark.asyncio
async def test_save_replaces_existing_row(session: AsyncSession, test_user: User):
    with patch(AUTH, new=AsyncMock(return_value="key")):
        await provider_credential_service.save_credential(session, test_user.id, "pluggy", "a", "s1")
        await provider_credential_service.save_credential(session, test_user.id, "pluggy", "b", "s2")

    provider = await provider_credential_service.resolve_provider(session, "pluggy", test_user.id)
    assert (provider._client_id, provider._client_secret) == ("b", "s2")


@pytest.mark.asyncio
async def test_rejected_credentials_are_not_saved(session: AsyncSession, test_user: User):
    with patch(AUTH, new=AsyncMock(side_effect=_rejected())):
        with pytest.raises(provider_credential_service.InvalidProviderCredentials):
            await provider_credential_service.save_credential(session, test_user.id, "pluggy", "a", "bad")
    assert await provider_credential_service.get_credential(session, test_user.id, "pluggy") is None


@pytest.mark.asyncio
async def test_unsupported_provider_rejected(session: AsyncSession, test_user: User):
    with pytest.raises(ValueError):
        await provider_credential_service.save_credential(session, test_user.id, "simplefin", "a", "b")


@pytest.mark.asyncio
async def test_without_own_credentials_falls_back_to_instance(session: AsyncSession, test_user: User):
    """No row and no env credentials: same error contract as get_provider."""
    with pytest.raises(ValueError):
        await provider_credential_service.resolve_provider(session, "pluggy", test_user.id)


@pytest.mark.asyncio
async def test_unreadable_secret_is_treated_as_missing(session: AsyncSession, test_user: User):
    session.add(
        ProviderCredential(
            user_id=test_user.id, provider="pluggy", client_id="x", client_secret_encrypted="garbage"
        )
    )
    await session.commit()
    assert await provider_credential_service.user_provider(session, "pluggy", test_user.id) is None
    assert not await provider_credential_service.user_has_credential(session, test_user.id, "pluggy")


@pytest.mark.asyncio
async def test_api_keys_cached_per_client_id():
    PluggyProvider._api_keys.clear()
    auth = AsyncMock(side_effect=["key-a", "key-b"])
    with patch(AUTH, new=auth):
        a = PluggyProvider("client-a", "s")
        b = PluggyProvider("client-b", "s")
        assert await a._ensure_api_key() == "key-a"
        assert await b._ensure_api_key() == "key-b"
        assert await a._ensure_api_key() == "key-a"
    assert auth.await_count == 2


@pytest.mark.asyncio
async def test_connect_token_uses_callers_credentials(session: AsyncSession, test_user: User):
    with patch(AUTH, new=AsyncMock(return_value="key")):
        await provider_credential_service.save_credential(session, test_user.id, "pluggy", "mine", "s")

    seen = {}

    async def fake_token(self, client_user_id, item_id=None):
        seen["client_id"] = self._client_id
        return MagicMock(access_token="tok")

    with patch.object(PluggyProvider, "create_connect_token", new=fake_token):
        result = await connection_service.create_connect_token(session, "pluggy", test_user.id)
    assert result == {"access_token": "tok"}
    assert seen["client_id"] == "mine"


@pytest.mark.asyncio
async def test_connection_resolves_to_owner_not_requester(
    session: AsyncSession, test_user: User, test_connection: BankConnection
):
    """A connection always goes through its owner's provider account."""
    with patch(AUTH, new=AsyncMock(return_value="key")):
        await provider_credential_service.save_credential(session, test_user.id, "pluggy", "owner", "s")

    test_connection.provider = "pluggy"
    await session.commit()
    provider = await connection_service.resolve_provider(
        session, test_connection.provider, test_connection.user_id
    )
    assert provider._client_id == "owner"


# --- API ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_api_roundtrip(client: AsyncClient, auth_headers):
    url = "/api/connections/credentials/pluggy"
    resp = await client.get(url, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["configured"] is False

    with patch(AUTH, new=AsyncMock(return_value="key")):
        resp = await client.put(url, json={"client_id": "cid", "client_secret": "sec"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {**body, "configured": True, "client_id": "cid"}
    assert "client_secret" not in body

    providers = (await client.get("/api/connections/providers", headers=auth_headers)).json()["providers"]
    pluggy = next(p for p in providers if p["name"] == "pluggy")
    assert pluggy["configured"] is True
    assert pluggy["has_user_credentials"] is True
    assert pluggy["supports_user_credentials"] is True

    resp = await client.delete(url, headers=auth_headers)
    assert resp.status_code == 204
    assert (await client.get(url, headers=auth_headers)).json()["configured"] is False


@pytest.mark.asyncio
async def test_credentials_api_rejects_invalid(client: AsyncClient, auth_headers):
    with patch(AUTH, new=AsyncMock(side_effect=_rejected(403))):
        resp = await client.put(
            "/api/connections/credentials/pluggy",
            json={"client_id": "cid", "client_secret": "bad"},
            headers=auth_headers,
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_credentials_api_requires_auth(client: AsyncClient):
    resp = await client.get("/api/connections/credentials/pluggy")
    assert resp.status_code == 401
