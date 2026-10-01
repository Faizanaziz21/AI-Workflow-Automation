from __future__ import annotations

from app.connectors.builtin.vonage.schemas import VonageConfig, VonageCredentials
from app.connectors.capabilities import SendSmsInput, SendSmsOutput
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

_RETRYABLE_STATUSES = {"1", "5"}  # throttled, internal error


class VonageConnector(Connector):
    key = "vonage"
    name = "Vonage SMS"
    description = "Send SMS messages through the Vonage (Nexmo) SMS API."
    category = "communication"
    icon = "message"
    auth = AuthSpec(AuthType.API_KEY, credentials_model=VonageCredentials, config_model=VonageConfig)

    @action("send_sms", "Send SMS", input=SendSmsInput, output=SendSmsOutput, capability="sms.send")
    async def send_sms(self, ctx: ConnectorContext, data: SendSmsInput) -> SendSmsOutput:
        cfg: VonageConfig = ctx.config
        payload = {
            "api_key": cfg.api_key,
            "api_secret": ctx.credentials.api_secret,
            "from": cfg.sender,
            "to": data.to.lstrip("+"),
            "text": data.body,
        }
        if ctx.idempotency_key:
            payload["client-ref"] = ctx.idempotency_key[:100]
        resp = await ctx.http.post(f"{str(cfg.api_base_url).rstrip('/')}/sms/json", data=payload)
        raise_for_status(resp, "Vonage")
        messages = resp.json().get("messages", [])
        if not messages:
            raise ConnectorError("Vonage returned no message status", retryable=True)
        first = messages[0]
        if first.get("status") != "0":
            raise ConnectorError(
                f"Vonage error {first.get('status')}: {first.get('error-text')}",
                retryable=first.get("status") in _RETRYABLE_STATUSES,
            )
        return SendSmsOutput(message_id=first.get("message-id", ""), status="submitted", provider="vonage")

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        cfg: VonageConfig = ctx.config
        resp = await ctx.http.get(
            f"{str(cfg.api_base_url).rstrip('/')}/account/get-balance",
            params={"api_key": cfg.api_key, "api_secret": ctx.credentials.api_secret},
        )
        if resp.status_code != 200:
            return TestResult(False, f"Vonage rejected credentials (HTTP {resp.status_code})")
        return TestResult(True, f"Balance: {resp.json().get('value')}")
