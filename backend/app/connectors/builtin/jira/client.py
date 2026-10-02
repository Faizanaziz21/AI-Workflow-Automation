from __future__ import annotations

from typing import Any

import httpx

from app.connectors.sdk import raise_for_status


def to_adf(text: str) -> dict[str, Any]:
    """Plain text -> Atlassian Document Format (one paragraph per line block)."""
    paragraphs = [p for p in text.split("\n\n")] if text else [""]
    content = []
    for para in paragraphs:
        lines = para.split("\n")
        nodes: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if line:
                nodes.append({"type": "text", "text": line})
            if i < len(lines) - 1:
                nodes.append({"type": "hardBreak"})
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(doc: Any) -> str:
    if not isinstance(doc, dict):
        return str(doc or "")
    out: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        elif node.get("type") == "hardBreak":
            out.append("\n")
        for child in node.get("content", []) or []:
            walk(child)
        if node.get("type") == "paragraph":
            out.append("\n\n")

    walk(doc)
    return "".join(out).strip()


class JiraClient:
    def __init__(self, http: httpx.AsyncClient, site: str, email: str, token: str) -> None:
        self.http = http
        self.site = site.rstrip("/")
        self.auth = httpx.BasicAuth(email, token)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self.http.request(
            method, f"{self.site}/rest/api/3{path}", auth=self.auth, headers={"Accept": "application/json"}, **kwargs
        )
        raise_for_status(resp, "Jira")
        return resp.json() if resp.content else None
