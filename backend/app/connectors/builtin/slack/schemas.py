from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class SlackConfig(BaseModel):
    api_base_url: HttpUrl = Field(
        default=HttpUrl("https://slack.com/api"), description="Override for Enterprise Grid/testing"
    )
    default_channel: str | None = Field(default=None, description="Channel used when an action omits one")


class SlackCredentials(BaseModel):
    bot_token: str = Field(description="Bot User OAuth token (xoxb-…)")


class PostMessageInput(BaseModel):
    channel: str | None = Field(default=None, description="Channel ID or #name; defaults to the connection's channel")
    text: str = Field(min_length=1, max_length=40000)
    blocks: list[dict[str, Any]] | None = Field(default=None, description="Block Kit blocks")
    thread_ts: str | None = None
    unfurl_links: bool = False


class PostMessageOutput(BaseModel):
    channel: str
    ts: str
    permalink: str | None = None


class LookupUserInput(BaseModel):
    email: str


class LookupUserOutput(BaseModel):
    found: bool
    user_id: str | None = None
    real_name: str | None = None


class DirectMessageInput(BaseModel):
    email: str = Field(description="Email address of the Slack user")
    text: str = Field(min_length=1, max_length=40000)
