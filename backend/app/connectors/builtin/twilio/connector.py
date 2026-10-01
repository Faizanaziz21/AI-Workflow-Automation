from __future__ import annotations

import httpx

from app.connectors.builtin.twilio.schemas import TwilioConfig, TwilioCredentials
from app.connectors.capabilities import SendSmsInput, SendSmsOutput
from app.connectors.sdk import AuthSpec, AuthType, Connector, ConnectorContext, TestResult, action, raise_for_status


class TwilioConnector(Connector):
    key = "twilio"
    name = "Twilio SMS"
    description = "Send SMS messages through Twilio Programmable Messaging."
    category = "communication"
    icon = "message"
    auth = AuthSpec(AuthType.BASIC, credentials_model=TwilioCredentials, config_model=TwilioConfig)

    def _url(self, ctx: ConnectorContext, path: str) -> str:
        return f"{str(ctx.config.api_base_url).rstrip('/')}/2010-04-01/Accounts/{ctx.config.account_sid}{path}"

    def _auth(self, ctx: ConnectorContext) -> httpx.BasicAuth:
        return httpx.BasicAuth(ctx.config.account_sid, ctx.credentials.auth_token)

    @action("send_sms", "Send SMS", input=SendSmsInput, output=SendSmsOutput, capability="sms.send")
    async def send_sms(self, ctx: ConnectorContext, data: SendSmsInput) -> SendSmsOutput:
        headers = {"I-Twilio-Idempotency-Token": ctx.idempotency_key} if ctx.idempotency_key else {}
        resp = await ctx.http.post(
            self._url(ctx, "/Messages.json"),
            auth=self._auth(ctx),
            headers=headers,
            data={"To": data.to, "From": ctx.config.from_number, "Body": data.body},
        )
        raise_for_status(resp, "Twilio")
        body = resp.json()
        return SendSmsOutput(message_id=body["sid"], status=body.get("status", "queued"), provider="twilio")

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        resp = await ctx.http.get(self._url(ctx, ".json"), auth=self._auth(ctx))
        if resp.status_code != 200:
            return TestResult(False, f"Twilio rejected credentials (HTTP {resp.status_code})")
        return TestResult(True, f"Account {resp.json().get('friendly_name')} is {resp.json().get('status')}")
