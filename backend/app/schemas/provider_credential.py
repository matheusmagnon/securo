from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ProviderCredentialWrite(BaseModel):
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str = Field(min_length=1, max_length=512)


class ProviderCredentialRead(BaseModel):
    """The secret is write-only: only whether it exists is ever returned."""

    provider: str
    configured: bool
    client_id: Optional[str] = None
    updated_at: Optional[datetime] = None
