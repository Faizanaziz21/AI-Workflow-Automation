from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class HubSpotConfig(BaseModel):
    api_base_url: HttpUrl = Field(
        default=HttpUrl("https://api.hubapi.com"), description="Override for sandboxes/testing"
    )
    portal_id: str | None = Field(default=None, description="HubSpot account ID used to build record links")


class HubSpotCredentials(BaseModel):
    access_token: str = Field(description="Private app access token (pat-…)")


class CreateDealInput(BaseModel):
    name: str = Field(max_length=500)
    amount: float | None = None
    stage: str = "appointmentscheduled"
    pipeline: str = "default"
    contact_id: str | None = None
    close_date: str | None = None
    properties: dict[str, Any] = {}


class CreateDealOutput(BaseModel):
    deal_id: str
    url: str | None = None


class NewContactsConfig(BaseModel):
    batch_size: int = Field(default=50, ge=1, le=100)
