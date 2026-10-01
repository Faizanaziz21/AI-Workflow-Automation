from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class JiraConfig(BaseModel):
    site_url: HttpUrl = Field(description="https://your-domain.atlassian.net")
    default_project: str | None = None


class JiraCredentials(BaseModel):
    email: str
    api_token: str


class GetIssueInput(BaseModel):
    issue: str = Field(description="Issue key or id")


class IssueOut(BaseModel):
    id: str
    key: str
    summary: str
    status: str
    priority: str | None = None
    assignee: str | None = None
    labels: list[str] = []
    url: str
    fields: dict[str, Any] = {}


class SearchInput(BaseModel):
    jql: str = Field(max_length=4000)
    max_results: int = Field(default=50, ge=1, le=100)


class SearchOutput(BaseModel):
    issues: list[IssueOut]
    total: int


class TransitionInput(BaseModel):
    issue: str
    transition: str = Field(description="Transition name, e.g. 'Done', or numeric id")
    comment: str | None = None


class TransitionOutput(BaseModel):
    issue: str
    transition_id: str


class IssuesUpdatedConfig(BaseModel):
    jql: str = Field(default="", description="Extra JQL filter, e.g. project = SUP")
    batch_size: int = Field(default=50, ge=1, le=100)
