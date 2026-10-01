from __future__ import annotations

from typing import Any

from app.connectors.builtin.teams.schemas import TeamsConfig, TeamsCredentials, TeamsMessageInput, TeamsMessageOutput
from app.connectors.sdk import AuthSpec, AuthType, Connector, ConnectorContext, TestResult, action, raise_for_status


def build_adaptive_card(data: TeamsMessageInput) -> dict[str, Any]:
    body: list[dict[str, Any]] = []
    if data.title:
        body.append({"type": "TextBlock", "text": data.title, "weight": "Bolder", "size": "Medium", "wrap": True})
    body.append({"type": "TextBlock", "text": data.text, "wrap": True})
    if data.facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in data.facts.items()]})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if data.link_url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": data.link_title, "url": data.link_url}]
    return card


class TeamsConnector(Connector):
    key = "teams"
    name = "Microsoft Teams"
    description = "Post Adaptive Card messages to Microsoft Teams channels via Workflows/Incoming Webhooks."
    category = "communication"
    icon = "teams"
    auth = AuthSpec(
        AuthType.API_KEY,
        credentials_model=TeamsCredentials,
        config_model=TeamsConfig,
        description="In Teams, add a 'Post to a channel when a webhook request is received' workflow and paste its URL.",
    )

    @action("post_message", "Post message", input=TeamsMessageInput, output=TeamsMessageOutput)
    async def post_message(self, ctx: ConnectorContext, data: TeamsMessageInput) -> TeamsMessageOutput:
        card = data.adaptive_card or build_adaptive_card(data)
        payload = {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
        }
        resp = await ctx.http.post(str(ctx.credentials.webhook_url), json=payload)
        raise_for_status(resp, "Microsoft Teams")
        return TeamsMessageOutput(delivered=True, status=resp.status_code)

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        url = str(ctx.credentials.webhook_url)
        trusted = any(
            h in url for h in (".webhook.office.com", ".logic.azure.com", "environment.api.powerplatform.com")
        )
        if not url.startswith("https://"):
            return TestResult(False, "Webhook URL must use HTTPS")
        msg = "Webhook URL is well-formed" + ("" if trusted else " (non-Microsoft host — verify it is intended)")
        return TestResult(True, msg, {"trusted_host": trusted})
