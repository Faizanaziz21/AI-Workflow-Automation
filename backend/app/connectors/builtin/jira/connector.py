from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.connectors.builtin.jira.client import JiraClient, adf_to_text, to_adf
from app.connectors.builtin.jira.schemas import (
    GetIssueInput,
    IssueOut,
    IssuesUpdatedConfig,
    JiraConfig,
    JiraCredentials,
    SearchInput,
    SearchOutput,
    TransitionInput,
    TransitionOutput,
)
from app.connectors.capabilities import (
    CommentTicketInput,
    CommentTicketOutput,
    CreateTicketInput,
    CreateTicketOutput,
    Ticket,
)
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

ISSUE_FIELDS = "summary,status,priority,assignee,labels,description,updated,created,issuetype,reporter"


class JiraConnector(Connector):
    key = "jira"
    name = "Jira"
    description = "Create, search, comment on and transition Jira issues (Cloud REST v3)."
    category = "ticketing"
    icon = "jira"
    docs_url = "https://developer.atlassian.com/cloud/jira/platform/rest/v3/"
    auth = AuthSpec(
        AuthType.BASIC,
        credentials_model=JiraCredentials,
        config_model=JiraConfig,
        description="Use an Atlassian account email and API token.",
    )

    def _client(self, ctx: ConnectorContext) -> JiraClient:
        return JiraClient(ctx.http, str(ctx.config.site_url), ctx.credentials.email, ctx.credentials.api_token)

    def _issue(self, ctx: ConnectorContext, raw: dict[str, Any]) -> IssueOut:
        f = raw.get("fields", {})
        return IssueOut(
            id=str(raw["id"]),
            key=raw["key"],
            summary=f.get("summary", ""),
            status=(f.get("status") or {}).get("name", ""),
            priority=(f.get("priority") or {}).get("name"),
            assignee=(f.get("assignee") or {}).get("displayName"),
            labels=f.get("labels") or [],
            url=f"{str(ctx.config.site_url).rstrip('/')}/browse/{raw['key']}",
            fields={
                "description": adf_to_text(f.get("description")),
                "updated": f.get("updated"),
                "created": f.get("created"),
                "issuetype": (f.get("issuetype") or {}).get("name"),
            },
        )

    @action(
        "create_issue", "Create issue", input=CreateTicketInput, output=CreateTicketOutput, capability="ticket.create"
    )
    async def create_issue(self, ctx: ConnectorContext, data: CreateTicketInput) -> CreateTicketOutput:
        project = data.project or ctx.config.default_project
        if not project:
            raise ConnectorError("Project key required")
        fields: dict[str, Any] = {
            "project": {"key": project},
            "summary": data.summary,
            "issuetype": {"name": data.issue_type},
            "description": to_adf(data.description),
            "labels": data.labels,
            **data.fields,
        }
        if data.priority:
            fields["priority"] = {"name": data.priority}
        if data.assignee:
            fields["assignee"] = {"accountId": data.assignee}
        created = await self._client(ctx).request("POST", "/issue", json={"fields": fields})
        url = f"{str(ctx.config.site_url).rstrip('/')}/browse/{created['key']}"
        return CreateTicketOutput(
            ticket=Ticket(id=str(created["id"]), key=created["key"], url=url, status="Open", summary=data.summary)
        )

    @action(
        "add_comment", "Add comment", input=CommentTicketInput, output=CommentTicketOutput, capability="ticket.comment"
    )
    async def add_comment(self, ctx: ConnectorContext, data: CommentTicketInput) -> CommentTicketOutput:
        result = await self._client(ctx).request(
            "POST", f"/issue/{data.ticket}/comment", json={"body": to_adf(data.body)}
        )
        return CommentTicketOutput(comment_id=str(result["id"]))

    @action("get_issue", "Get issue", input=GetIssueInput, output=IssueOut, idempotent=True)
    async def get_issue(self, ctx: ConnectorContext, data: GetIssueInput) -> IssueOut:
        raw = await self._client(ctx).request("GET", f"/issue/{data.issue}", params={"fields": ISSUE_FIELDS})
        return self._issue(ctx, raw)

    @action("search_issues", "Search issues (JQL)", input=SearchInput, output=SearchOutput, idempotent=True)
    async def search_issues(self, ctx: ConnectorContext, data: SearchInput) -> SearchOutput:
        raw = await self._client(ctx).request(
            "POST",
            "/search/jql",
            json={"jql": data.jql, "maxResults": data.max_results, "fields": ISSUE_FIELDS.split(",")},
        )
        issues = [self._issue(ctx, i) for i in raw.get("issues", [])]
        return SearchOutput(issues=issues, total=raw.get("total", len(issues)))

    @action("transition_issue", "Transition issue", input=TransitionInput, output=TransitionOutput)
    async def transition_issue(self, ctx: ConnectorContext, data: TransitionInput) -> TransitionOutput:
        client = self._client(ctx)
        available = await client.request("GET", f"/issue/{data.issue}/transitions")
        match = next(
            (
                t
                for t in available.get("transitions", [])
                if t["id"] == data.transition or t["name"].lower() == data.transition.lower()
            ),
            None,
        )
        if match is None:
            names = [t["name"] for t in available.get("transitions", [])]
            raise ConnectorError(f"Transition '{data.transition}' not available; options: {names}")
        body: dict[str, Any] = {"transition": {"id": match["id"]}}
        if data.comment:
            body["update"] = {"comment": [{"add": {"body": to_adf(data.comment)}}]}
        await client.request("POST", f"/issue/{data.issue}/transitions", json=body)
        return TransitionOutput(issue=data.issue, transition_id=match["id"])

    @trigger("issues_updated", "Issue created or updated", config=IssuesUpdatedConfig, output=IssueOut, interval=60)
    async def issues_updated(self, ctx: ConnectorContext, cfg: IssuesUpdatedConfig, state: dict) -> PollResult:
        since = state.get("since") or (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        jql = f'updated >= "{since}"' + (f" AND ({cfg.jql})" if cfg.jql else "") + " ORDER BY updated ASC"
        result = await self.search_issues(ctx, SearchInput(jql=jql, max_results=cfg.batch_size))
        seen = set(state.get("seen", []))
        items = []
        for issue in result.issues:
            marker = f"{issue.key}@{issue.fields.get('updated')}"
            if marker in seen:
                continue
            seen.add(marker)
            items.append(issue.model_dump())
        new_since = since
        if result.issues and result.issues[-1].fields.get("updated"):
            new_since = datetime.fromisoformat(result.issues[-1].fields["updated"].replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M"
            )
        return PollResult(items=items, state={"since": new_since, "seen": sorted(seen)[-500:]})

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            me = await self._client(ctx).request("GET", "/myself")
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        return TestResult(True, f"Authenticated as {me.get('displayName')}", {"account_id": me.get("accountId")})
