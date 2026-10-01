"""Provider-neutral capability contracts.

Abstraction nodes (``comm.email``, ``comm.sms``, ``crm.*``, ``ticket.*``) are written against these schemas.
Any connector action declaring ``capability="crm.upsert_contact"`` (etc.) must accept/return them, so a
workflow can switch from HubSpot to Pipedrive — or Twilio to Vonage — by changing only the connection.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field


class SendEmailInput(BaseModel):
    to: list[EmailStr] = Field(min_length=1, max_length=100)
    cc: list[EmailStr] = []
    bcc: list[EmailStr] = []
    subject: str = Field(max_length=998)
    text: str | None = Field(default=None, description="Plain-text body")
    html: str | None = Field(default=None, description="HTML body")
    reply_to: EmailStr | None = None
    from_name: str | None = None
    headers: dict[str, str] = {}


class SendEmailOutput(BaseModel):
    message_id: str
    accepted: list[str]
    provider: str


class SendSmsInput(BaseModel):
    to: str = Field(pattern=r"^\+[1-9]\d{6,14}$", description="E.164 phone number")
    body: str = Field(min_length=1, max_length=1600)


class SendSmsOutput(BaseModel):
    message_id: str
    status: str
    provider: str


class CrmContact(BaseModel):
    id: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    phone: str | None = None
    job_title: str | None = None
    lifecycle_stage: str | None = None
    properties: dict[str, Any] = {}
    url: str | None = None


class FindContactInput(BaseModel):
    email: EmailStr


class FindContactOutput(BaseModel):
    found: bool
    contact: CrmContact | None = None


class UpsertContactInput(BaseModel):
    email: EmailStr
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    phone: str | None = None
    job_title: str | None = None
    lifecycle_stage: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict, description="Provider-specific extra properties")


class UpsertContactOutput(BaseModel):
    contact: CrmContact
    created: bool


class CreateTaskInput(BaseModel):
    contact_id: str | None = None
    subject: str = Field(max_length=500)
    body: str = ""
    due_at: str | None = Field(default=None, description="ISO-8601 due date")
    owner_id: str | None = None
    priority: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")


class CreateTaskOutput(BaseModel):
    task_id: str
    url: str | None = None


class AddNoteInput(BaseModel):
    contact_id: str
    body: str = Field(min_length=1, max_length=65000)


class AddNoteOutput(BaseModel):
    note_id: str


class CreateTicketInput(BaseModel):
    project: str = Field(description="Project key / queue")
    summary: str = Field(max_length=255)
    description: str = ""
    issue_type: str = "Task"
    priority: str | None = None
    labels: list[str] = []
    assignee: str | None = None
    fields: dict[str, Any] = {}


class Ticket(BaseModel):
    id: str
    key: str | None = None
    url: str | None = None
    status: str | None = None
    summary: str | None = None


class CreateTicketOutput(BaseModel):
    ticket: Ticket


class CommentTicketInput(BaseModel):
    ticket: str = Field(description="Ticket id or key")
    body: str = Field(min_length=1)


class CommentTicketOutput(BaseModel):
    comment_id: str


CAPABILITIES: dict[str, tuple[type[BaseModel], type[BaseModel], str]] = {
    "email.send": (SendEmailInput, SendEmailOutput, "Send email"),
    "sms.send": (SendSmsInput, SendSmsOutput, "Send SMS"),
    "crm.find_contact": (FindContactInput, FindContactOutput, "Find CRM contact"),
    "crm.upsert_contact": (UpsertContactInput, UpsertContactOutput, "Create/update CRM contact"),
    "crm.create_task": (CreateTaskInput, CreateTaskOutput, "Create CRM task"),
    "crm.add_note": (AddNoteInput, AddNoteOutput, "Add CRM note"),
    "ticket.create": (CreateTicketInput, CreateTicketOutput, "Create ticket"),
    "ticket.comment": (CommentTicketInput, CommentTicketOutput, "Comment on ticket"),
}
