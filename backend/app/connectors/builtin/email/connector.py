from __future__ import annotations

from app.connectors.builtin.email.client import build_message, imap_fetch_new, smtp_check, smtp_send
from app.connectors.builtin.email.schemas import EmailConfig, EmailCredentials, NewEmailTriggerConfig, ReceivedEmail
from app.connectors.capabilities import SendEmailInput, SendEmailOutput
from app.connectors.sdk import (
    AuthSpec,
    AuthType,
    Connector,
    ConnectorContext,
    ConnectorError,
    PollResult,
    TestResult,
    action,
    trigger,
)


class EmailConnector(Connector):
    key = "email"
    name = "Email (SMTP/IMAP)"
    description = "Send email over SMTP and trigger workflows from new messages in an IMAP mailbox."
    category = "communication"
    icon = "mail"
    auth = AuthSpec(AuthType.BASIC, credentials_model=EmailCredentials, config_model=EmailConfig)

    @action(
        "send_email",
        "Send email",
        input=SendEmailInput,
        output=SendEmailOutput,
        capability="email.send",
        description="Send a plain-text and/or HTML email.",
    )
    async def send_email(self, ctx: ConnectorContext, data: SendEmailInput) -> SendEmailOutput:
        cfg: EmailConfig = ctx.config
        if not data.text and not data.html:
            raise ConnectorError("Email requires a text or html body")
        msg = build_message(
            from_address=str(cfg.from_address),
            from_name=data.from_name or cfg.from_name,
            to=[str(a) for a in data.to],
            cc=[str(a) for a in data.cc],
            subject=data.subject,
            text=data.text,
            html=data.html,
            reply_to=str(data.reply_to) if data.reply_to else None,
            headers=data.headers,
            idempotency_key=ctx.idempotency_key,
        )
        recipients = [str(a) for a in (*data.to, *data.cc, *data.bcc)]
        await smtp_send(cfg, ctx.credentials, msg, recipients, timeout=ctx.timeout)
        return SendEmailOutput(message_id=msg["Message-ID"], accepted=recipients, provider="smtp")

    @trigger(
        "new_email",
        "Email received",
        config=NewEmailTriggerConfig,
        output=ReceivedEmail,
        interval=60,
        description="Poll the IMAP mailbox for new messages.",
    )
    async def new_email(self, ctx: ConnectorContext, cfg: NewEmailTriggerConfig, state: dict) -> PollResult:
        conn_cfg: EmailConfig = ctx.config
        if not conn_cfg.imap_host:
            raise ConnectorError("IMAP host not configured on this connection")
        last_uid = int(state.get("last_uid", 0))
        messages = await imap_fetch_new(
            conn_cfg, ctx.credentials, cfg.folder, last_uid, cfg.max_per_poll, cfg.mark_seen
        )
        items = []
        for m in messages:
            last_uid = max(last_uid, m["uid"])
            if cfg.from_filter and cfg.from_filter.lower() not in (m["from_address"] or "").lower():
                continue
            if cfg.subject_filter and cfg.subject_filter.lower() not in m["subject"].lower():
                continue
            items.append(m)
        return PollResult(items=items, state={"last_uid": last_uid})

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            msg = await smtp_check(ctx.config, ctx.credentials)
        except Exception as exc:
            return TestResult(False, f"SMTP check failed: {type(exc).__name__}: {exc}")
        return TestResult(True, msg)
