from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class VonageConfig(BaseModel):
    api_key: str = Field(description="Vonage API key (public identifier)")
    sender: str = Field(description="Sender ID or number")
    api_base_url: HttpUrl = Field(default=HttpUrl("https://rest.nexmo.com"))


class VonageCredentials(BaseModel):
    api_secret: str
