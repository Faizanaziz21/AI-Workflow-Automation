from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class EmailConfig(BaseModel):
    smtp_host: str = Field(description="SMTP server hostname")
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "tls", "none"] = "starttls"
    from_address: EmailStr
    from_name: str = ""
    imap_host: str | None = Field(default=None, description="IMAP server for the 'Email received' trigger")
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_ssl: bool = True


class EmailCredentials(BaseModel):
    username: str | None = None
    password: str | None = None


class NewEmailTriggerConfig(BaseModel):
    folder: str = "INBOX"
    from_filter: str | None = Field(default=None, description="Only emails whose From contains this text")
    subject_filter: str | None = Field(default=None, description="Only emails whose Subject contains this text")
    mark_seen: bool = True
    max_per_poll: int = Field(default=25, ge=1, le=200)


class ReceivedEmail(BaseModel):
    uid: int
    message_id: str | None
    from_address: str
    from_name: str | None
    to: list[str]
    subject: str
    date: str | None
    text: str
    html: str | None
    attachments: list[dict] = []
