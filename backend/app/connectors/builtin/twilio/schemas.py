from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class TwilioConfig(BaseModel):
    account_sid: str = Field(pattern=r"^AC[0-9a-fA-F]{32}$")
    from_number: str = Field(pattern=r"^\+[1-9]\d{6,14}$", description="Twilio sender number (E.164)")
    api_base_url: HttpUrl = Field(default=HttpUrl("https://api.twilio.com"))


class TwilioCredentials(BaseModel):
    auth_token: str
