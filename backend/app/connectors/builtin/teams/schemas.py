from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class TeamsConfig(BaseModel):
    channel_name: str = Field(default="", description="Display name of the target channel (informational)")


class TeamsCredentials(BaseModel):
    webhook_url: HttpUrl = Field(
        description="Teams Workflows / Incoming Webhook URL for the channel (treated as a secret)"
    )


class TeamsMessageInput(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    text: str = Field(min_length=1, max_length=28000)
    facts: dict[str, str] = Field(default_factory=dict, description="Key/value facts rendered as a table")
    link_url: str | None = None
    link_title: str = "Open"
    adaptive_card: dict[str, Any] | None = Field(
        default=None, description="Raw Adaptive Card overrides the generated card"
    )


class TeamsMessageOutput(BaseModel):
    delivered: bool
    status: int
