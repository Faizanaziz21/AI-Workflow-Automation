from __future__ import annotations

from typing import Any

from app.connectors.builtin.sendgrid.schemas import SendGridConfig, SendGridCredentials
from app.connectors.capabilities import SendEmailInput, SendEmailOutput
from app.connectors.sdk import (
    AuthSpec,
    AuthType,
    Connector,
    ConnectorContext,
    ConnectorError,
    TestResult,
    action,
    raise_for_status,
)


class SendGridConnector(Connector):
    key = "sendgrid"
    name = "SendGrid"
    description = "Transactional email via the SendGrid v3 Mail Send API."
    category = "communication"
    icon = "mail"
    auth = AuthSpec(AuthType.API_KEY, credentials_model=SendGridCredentials, config_model=SendGridConfig)

    def _headers(self, ctx: ConnectorContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.credentials.api_key}"}

    @action("send_email", "Send email", input=SendEmailInput, output=SendEmailOutput, capability="email.send")
    async def send_email(self, ctx: ConnectorContext, data: SendEmailInput) -> SendEmailOutput:
        cfg: SendGridConfig = ctx.config
        if not data.text and not data.html:
            raise ConnectorError("Email requires a text or html body")
        personalization: dict[str, Any] = {"to": [{"email": str(a)} for a in data.to]}
        if data.cc:
            personalization["cc"] = [{"email": str(a)} for a in data.cc]
        if data.bcc:
            personalization["bcc"] = [{"email": str(a)} for a in data.bcc]
        content = []
        if data.text:
            content.append({"type": "text/plain", "value": data.text})
        if data.html:
            content.append({"type": "text/html", "value": data.html})
        payload: dict[str, Any] = {
            "personalizations": [personalization],
            "from": {"email": str(cfg.from_address), "name": data.from_name or cfg.from_name or None},
            "subject": data.subject,
            "content": content,
            "headers": data.headers or None,
            "custom_args": {"flowforge_idempotency_key": ctx.idempotency_key} if ctx.idempotency_key else None,
        }
        if data.reply_to:
            payload["reply_to"] = {"email": str(data.reply_to)}
        payload = {k: v for k, v in payload.items() if v is not None}
        resp = await ctx.http.post(
            f"{str(cfg.api_base_url).rstrip('/')}/v3/mail/send", json=payload, headers=self._headers(ctx)
        )
        raise_for_status(resp, "SendGrid")
        return SendEmailOutput(
            message_id=resp.headers.get("X-Message-Id", ""),
            accepted=[str(a) for a in (*data.to, *data.cc, *data.bcc)],
            provider="sendgrid",
        )

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        resp = await ctx.http.get(f"{str(ctx.config.api_base_url).rstrip('/')}/v3/scopes", headers=self._headers(ctx))
        if resp.status_code != 200:
            return TestResult(False, f"SendGrid rejected the API key (HTTP {resp.status_code})")
        scopes = resp.json().get("scopes", [])
        ok = "mail.send" in scopes
        return TestResult(ok, "API key valid" + ("" if ok else " but lacks mail.send scope"), {"scopes": len(scopes)})
