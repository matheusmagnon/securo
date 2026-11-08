"""Per-user bank sync provider credentials.

`resolve_provider` is the one place that decides which credentials a
provider call uses: the user's own (a `ProviderCredential` row) when they
saved some, otherwise the instance-wide ones from the environment. Calls
made on behalf of an existing connection always resolve against the
connection's owner, never the member who triggered them, so a connection
keeps syncing through the provider account it was created with.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import uuid
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.provider_credential import ProviderCredential
from app.providers import BankProvider, get_provider

# Providers whose credentials a user can bring.
USER_CREDENTIAL_PROVIDERS = ("pluggy",)

_SALT = b"securo-provider-credentials-v1"


@functools.lru_cache(maxsize=1)
def _fernet() -> Fernet:
    secret = get_settings().secret_key.get_secret_value().encode("utf-8")
    raw = hashlib.pbkdf2_hmac("sha256", secret, _SALT, iterations=100_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(raw))


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt(ciphertext: str) -> Optional[str]:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None  # SECRET_KEY rotated or row corrupt: treat as missing


class InvalidProviderCredentials(ValueError):
    """The provider rejected the client ID / secret pair."""


def _check_supported(provider: str) -> None:
    if provider not in USER_CREDENTIAL_PROVIDERS:
        raise ValueError(f"Provider '{provider}' does not support per-user credentials")


async def get_credential(
    session: AsyncSession, user_id: uuid.UUID, provider: str
) -> Optional[ProviderCredential]:
    result = await session.execute(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user_id,
            ProviderCredential.provider == provider,
        )
    )
    return result.scalar_one_or_none()


async def _validate(provider: str, client_id: str, client_secret: str) -> None:
    if provider == "pluggy":
        import httpx

        from app.providers.pluggy import PluggyProvider

        try:
            await PluggyProvider.authenticate(client_id, client_secret)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401, 403):
                raise InvalidProviderCredentials("Pluggy rejected these credentials") from exc
            raise


async def save_credential(
    session: AsyncSession,
    user_id: uuid.UUID,
    provider: str,
    client_id: str,
    client_secret: str,
) -> ProviderCredential:
    """Validate against the provider, then create or replace the user's row."""
    _check_supported(provider)
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("Client ID and client secret are required")

    await _validate(provider, client_id, client_secret)

    credential = await get_credential(session, user_id, provider)
    if credential is None:
        credential = ProviderCredential(user_id=user_id, provider=provider)
        session.add(credential)
    credential.client_id = client_id
    credential.client_secret_encrypted = _encrypt(client_secret)
    await session.commit()
    await session.refresh(credential)
    return credential


async def delete_credential(session: AsyncSession, user_id: uuid.UUID, provider: str) -> bool:
    credential = await get_credential(session, user_id, provider)
    if credential is None:
        return False
    await session.delete(credential)
    await session.commit()
    return True


async def user_provider(
    session: AsyncSession, provider_name: str, user_id: uuid.UUID
) -> Optional[BankProvider]:
    """Provider instance bound to `user_id`'s own credentials, or None."""
    if provider_name == "pluggy":
        credential = await get_credential(session, user_id, provider_name)
        if credential is not None:
            secret = _decrypt(credential.client_secret_encrypted)
            if secret:
                from app.providers.pluggy import PluggyProvider

                return PluggyProvider(credential.client_id, secret)
    return None


async def resolve_provider(
    session: AsyncSession, provider_name: str, user_id: uuid.UUID
) -> BankProvider:
    """The user's own provider when they saved credentials, else the env one.

    Raises ValueError when neither exists (same contract as `get_provider`).
    """
    return await user_provider(session, provider_name, user_id) or get_provider(provider_name)


async def user_has_credential(session: AsyncSession, user_id: uuid.UUID, provider: str) -> bool:
    credential = await get_credential(session, user_id, provider)
    return credential is not None and _decrypt(credential.client_secret_encrypted) is not None
