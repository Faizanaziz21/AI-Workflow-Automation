from __future__ import annotations

from app.connectors.builtin.slack.client import SlackClient
from app.connectors.builtin.slack.schemas import (
    DirectMessageInput,
    LookupUserInput,
    LookupUserOutput,
    PostMessageInput,
    PostMessageOutput,
    SlackConfig,
    SlackCredentials,
)
from app.connectors.sdk import AuthSpec, AuthType, Connector, ConnectorContext, ConnectorError, TestResult, action


class SlackConnector(Connector):
    key = "slack"
    name = "Slack"
    description = "Post messages and notifications to Slack channels and users."
    category = "communication"
    icon = "slack"
    docs_url = "https://api.slack.com/methods"
    auth = AuthSpec(
        AuthType.BEARER,
        credentials_model=SlackCredentials,
        config_model=SlackConfig,
        description="Create a Slack app, add chat:write and users:read.email scopes, install it and paste the bot token.",
    )

    def _client(self, ctx: ConnectorContext) -> SlackClient:
        return SlackClient(ctx.http, str(ctx.config.api_base_url), ctx.credentials.bot_token)

    @action(
        "post_message",
        "Post message",
        input=PostMessageInput,
        output=PostMessageOutput,
        description="Post a message (optionally Block Kit) to a channel or thread.",
    )
    async def post_message(self, ctx: ConnectorContext, data: PostMessageInput) -> PostMessageOutput:
        channel = data.channel or ctx.config.default_channel
        if not channel:
            raise ConnectorError("No channel given and the connection has no default channel")
        payload = {"channel": channel, "text": data.text, "unfurl_links": data.unfurl_links}
        if data.blocks:
            payload["blocks"] = data.blocks
        if data.thread_ts:
            payload["thread_ts"] = data.thread_ts
        client = self._client(ctx)
        result = await client.call("chat.postMessage", payload)
        permalink = None
        try:
            link = await client.call(
                "chat.getPermalink", {"channel": result["channel"], "message_ts": result["ts"]}, get=True
            )
            permalink = link.get("permalink")
        except ConnectorError:
            permalink = None
        return PostMessageOutput(channel=result["channel"], ts=result["ts"], permalink=permalink)

    @action("lookup_user", "Find user by email", input=LookupUserInput, output=LookupUserOutput, idempotent=True)
    async def lookup_user(self, ctx: ConnectorContext, data: LookupUserInput) -> LookupUserOutput:
        try:
            result = await self._client(ctx).call("users.lookupByEmail", {"email": data.email}, get=True)
        except ConnectorError as exc:
            if "users_not_found" in exc.message:
                return LookupUserOutput(found=False)
            raise
        user = result.get("user", {})
        return LookupUserOutput(found=True, user_id=user.get("id"), real_name=user.get("real_name"))

    @action(
        "direct_message",
        "Direct message user",
        input=DirectMessageInput,
        output=PostMessageOutput,
        description="Send a DM to the Slack user with the given email address.",
    )
    async def direct_message(self, ctx: ConnectorContext, data: DirectMessageInput) -> PostMessageOutput:
        user = await self.lookup_user(ctx, LookupUserInput(email=data.email))
        if not user.found or not user.user_id:
            raise ConnectorError(f"No Slack user with email {data.email}")
        return await self.post_message(ctx, PostMessageInput(channel=user.user_id, text=data.text))

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            data = await self._client(ctx).call("auth.test")
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        return TestResult(
            True,
            f"Authenticated to workspace {data.get('team')} as {data.get('user')}",
            {"team": data.get("team"), "bot_id": data.get("bot_id")},
        )
