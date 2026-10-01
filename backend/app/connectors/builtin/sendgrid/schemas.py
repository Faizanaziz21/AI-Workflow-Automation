from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, HttpUrl


class SendGridConfig(BaseModel):
    from_address: EmailStr
    from_name: str = ""
    api_base_url: HttpUrl = Field(default=HttpUrl("https://api.sendgrid.com"))


class SendGridCredentials(BaseModel):
    api_key: str = Field(description="SendGrid API key with Mail Send permission")
