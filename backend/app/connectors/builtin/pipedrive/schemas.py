from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class PipedriveConfig(BaseModel):
    company_domain: str = Field(description="Your Pipedrive subdomain, e.g. 'acme' for acme.pipedrive.com")
    api_base_url: HttpUrl | None = Field(default=None, description="Override API base (testing)")


class PipedriveCredentials(BaseModel):
    api_token: str
