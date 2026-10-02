"""FlowForge Sandbox — protocol-compatible simulators for demos, end-to-end tests and load tests.

This is *environment*, not platform logic: FlowForge talks to these endpoints through its normal, unmodified
connectors (HubSpot, Jira, Slack, SendGrid, REST, OpenAI-compatible LLM) exactly as it would to the real
services — only the base URLs differ. It lets the platform run end-to-end without third-party accounts.

Simulated services (all in-memory, reset on restart):

* ``/v1/*``                 OpenAI-compatible LLM ("sandbox-llm"): schema-aware, deterministic responses
* ``/crm/v3/*``             HubSpot CRM v3 subset (contacts search/create/update, notes, tasks, deals)
* ``/rest/api/3/*``         Jira Cloud v3 subset (issues, comments, search, myself)
* ``/api/*``                Slack Web API subset (chat.postMessage, getPermalink, auth.test, users.lookupByEmail)
* ``/v3/*``                 SendGrid v3 subset (mail/send, scopes)
* ``/helpdesk/*``           Helpdesk REST API (ticket history, knowledge-base search)
* ``/enrichment/*``, ``/erp/*``  Company enrichment and ERP stubs
* ``/_chaos``               Inject latency, failures and timeouts (load / resilience testing)
* ``/_inspect/{kind}``      Inspect side effects (emails, slack messages, tickets, notes, ...)
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import random
import re
import time
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FlowForge Sandbox", version="1.0.0")

STORE: dict[str, list[dict[str, Any]]] = defaultdict(list)
CHAOS: dict[str, Any] = {
    "latency_ms": 0,
    "jitter_ms": 0,
    "failure_rate": 0.0,
    "timeout_rate": 0.0,
    "timeout_ms": 30000,
    "paths": [],
}
_ids = itertools.count(1000)

CONTACTS: dict[str, dict[str, Any]] = {}


def _seed() -> None:
    CONTACTS.clear()
    seed = [
        ("jane@globex.com", "Jane", "Cooper", "Globex Corporation", "enterprise", "VP Operations"),
        ("bob@initech.com", "Bob", "Slydell", "Initech", "mid_market", "IT Manager"),
        ("amy@tinyco.io", "Amy", "Lee", "TinyCo", "smb", "Founder"),
        ("li@umbrella.com", "Li", "Wei", "Umbrella Corp", "enterprise", "Director of Support"),
    ]
    for email, first, last, company, tier, title in seed:
        cid = str(next(_ids))
        CONTACTS[email] = {
            "id": cid,
            "properties": {
                "email": email,
                "firstname": first,
                "lastname": last,
                "company": company,
                "tier": tier,
                "jobtitle": title,
                "lifecyclestage": "customer",
                "createdate": "2024-01-15T10:00:00Z",
            },
        }


_seed()

TICKETS = {
    "jane@globex.com": [
        {"id": "T-4411", "subject": "Invoice shows wrong VAT", "status": "solved", "created_at": "2026-08-02"},
        {"id": "T-4520", "subject": "Charged twice for September", "status": "solved", "created_at": "2026-09-03"},
    ],
    "li@umbrella.com": [{"id": "T-3001", "subject": "SSO login loop", "status": "solved", "created_at": "2026-06-11"}],
}

KB = [
    {
        "id": "KB-101",
        "title": "Duplicate charges and how refunds work",
        "body": "If you were charged twice, the duplicate authorization is released automatically within 3-5 business "
        "days. Settled duplicates are refunded to the original payment method within 5 business days.",
    },
    {
        "id": "KB-102",
        "title": "Resetting your password",
        "body": "Use 'Forgot password' on the sign-in page. Links expire after 30 minutes.",
    },
    {
        "id": "KB-103",
        "title": "Updating billing details and invoices",
        "body": "Admins can update billing contacts and download invoices under Settings → Billing.",
    },
    {
        "id": "KB-104",
        "title": "SSO troubleshooting",
        "body": "Verify the IdP certificate and that the ACS URL matches your workspace domain.",
    },
    {
        "id": "KB-105",
        "title": "Exporting data",
        "body": "Use Settings → Data → Export to download CSV exports of your records.",
    },
]


@app.middleware("http")
async def chaos(request: Request, call_next: Any) -> Any:
    path = request.url.path
    if not path.startswith("/_") and (not CHAOS["paths"] or any(path.startswith(p) for p in CHAOS["paths"])):
        delay = CHAOS["latency_ms"] + (random.random() * CHAOS["jitter_ms"] if CHAOS["jitter_ms"] else 0)  # noqa: S311
        if delay:
            await asyncio.sleep(delay / 1000)
        roll = random.random()  # noqa: S311
        if roll < CHAOS["timeout_rate"]:
            await asyncio.sleep(CHAOS["timeout_ms"] / 1000)
        elif roll < CHAOS["timeout_rate"] + CHAOS["failure_rate"]:
            return JSONResponse({"error": {"message": "sandbox injected failure"}}, status_code=503)
    STORE["_requests"].append({"method": request.method, "path": path, "ts": time.time()})
    if len(STORE["_requests"]) > 5000:
        del STORE["_requests"][:1000]
    return await call_next(request)


# ----------------------------------------------------------------------------- control plane


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_chaos")
async def set_chaos(body: dict[str, Any]) -> dict[str, Any]:
    CHAOS.update({k: v for k, v in body.items() if k in CHAOS})
    return CHAOS


@app.post("/_reset")
async def reset() -> dict[str, str]:
    STORE.clear()
    CHAOS.update({"latency_ms": 0, "jitter_ms": 0, "failure_rate": 0.0, "timeout_rate": 0.0, "paths": []})
    _seed()
    return {"status": "reset"}


@app.get("/_inspect/{kind}")
async def inspect(kind: str) -> list[dict[str, Any]]:
    return STORE.get(kind, [])


# ----------------------------------------------------------------------------- sandbox LLM

WORDS = {
    "billing": ["invoice", "charge", "charged", "refund", "payment", "billed", "billing", "vat", "price"],
    "support": ["error", "bug", "broken", "help", "issue", "not working", "login", "password", "sso", "crash"],
    "sales": ["pricing", "demo", "quote", "buy", "purchase", "trial", "plan", "licen"],
    "complaint": ["unacceptable", "terrible", "furious", "worst", "disappointed", "angry", "ridiculous", "cancel"],
    "partnership": ["partner", "partnership", "collaborat", "reseller", "integration partner"],
    "spam": ["lottery", "winner", "crypto giveaway", "click here", "viagra"],
}
NEGATIVE = [
    "unacceptable",
    "terrible",
    "furious",
    "worst",
    "disappointed",
    "angry",
    "ridiculous",
    "frustrat",
    "again",
    "still",
    "cancel",
    "never",
    "awful",
    "charged twice",
]
POSITIVE = ["thanks", "thank you", "great", "love", "awesome", "happy", "appreciate"]
RISKY = ["lawyer", "legal", "lawsuit", "security", "breach", "gdpr", "chargeback", "cancel my contract", "sue"]


def _score(text: str, words: list[str]) -> int:
    return sum(text.count(w) for w in words)


def _sentiment(text: str) -> tuple[str, float]:
    neg, pos = _score(text, NEGATIVE), _score(text, POSITIVE)
    if neg >= 3 or (neg >= 2 and "!" in text):
        return "very_negative", -0.85
    if neg > pos:
        return "negative", -0.45
    if pos > neg:
        return ("very_positive", 0.85) if pos >= 3 else ("positive", 0.5)
    return "neutral", 0.0


def _category(text: str, options: list[str]) -> str:
    scored = sorted(((max(_score(text, WORDS.get(o, [o])), 0), i, o) for i, o in enumerate(options)), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2]
    return "other" if "other" in options else options[-1]


def _employees(text: str) -> int:
    m = re.search(r'"?employees"?\s*[:=]\s*(\d+)', text)
    return int(m.group(1)) if m else 50


def _schema_from_request(body: dict[str, Any]) -> dict[str, Any] | None:
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        return fmt["json_schema"]["schema"]
    for m in body.get("messages", []):
        if m.get("role") == "system" and "JSON Schema" in m.get("content", ""):
            return json.loads(m["content"].rsplit("\n", 1)[-1])
    return {"type": "object"} if fmt.get("type") == "json_object" else None


class Generator:
    def __init__(self, text: str) -> None:
        self.raw = text
        self.text = text.lower()
        self.sentiment, self.sent_score = _sentiment(self.text)
        self.risky = _score(self.text, RISKY) > 0 or ("refund" in self.text and self.sentiment == "very_negative")
        self.has_articles = '"kb-' in self.text or "kb-1" in self.text

    def value(self, name: str, schema: dict[str, Any]) -> Any:
        types = schema.get("type", "string")
        if isinstance(types, list):
            non_null = [t for t in types if t != "null"]
            types = non_null[0] if non_null else "null"
        if "enum" in schema:
            return self.enum(name, schema["enum"])
        if types == "object":
            props = schema.get("properties", {})
            return {k: self.value(k, v) for k, v in props.items()}
        if types == "array":
            return self.array(name, schema.get("items", {}))
        if types in ("number", "integer"):
            v = self.number(name, schema)
            return int(v) if types == "integer" else v
        if types == "boolean":
            return name in ("auto_renewal",) and "renew" in self.text
        if types == "null":
            return None
        return self.string(name)

    def enum(self, name: str, options: list[Any]) -> Any:
        opts = [str(o) for o in options]
        if set(opts) >= {"very_negative", "negative", "neutral"}:
            return self.sentiment
        if name == "risk" or (set(opts) == {"low", "medium", "high"} and name in ("risk", "overall_risk")):
            return "high" if self.risky else ("medium" if self.sentiment == "very_negative" else "low")
        if name == "decision" and "reply" in opts:
            return "needs_agent" if self.risky else "reply"
        if name == "classification":
            n = _employees(self.text)
            return "enterprise" if n >= 1000 else "mid_market" if n >= 100 else "smb"
        if name in ("priority", "urgency"):
            urgent = (
                any(w in self.text for w in ("urgent", "asap", "immediately", "outage", "down"))
                or self.sentiment == "very_negative"
            )
            if "critical" in opts and ("outage" in self.text or "data loss" in self.text):
                return "critical"
            high = "high" if "high" in opts else opts[0]
            return (
                high
                if urgent or _employees(self.text) >= 1000
                else ("normal" if "normal" in opts else "medium" if "medium" in opts else opts[-1])
            )
        if name == "language":
            return opts[0]
        if name in ("document_type",):
            return next((o for o in opts if o in self.text), "other")
        return _category(self.text, opts)

    def number(self, name: str, schema: dict[str, Any]) -> float:
        if name == "confidence":
            return 0.94 if not self.risky and self.sentiment not in ("very_negative",) else 0.72
        if name == "score":
            return self.sent_score
        if name in ("estimated_deal_value",):
            return float(_employees(self.text) * 120)
        if name == "fit_score":
            return 78.0
        m = re.search(r"\$?\b(\d{2,7}(?:\.\d{1,2})?)\b", self.raw)
        if name in ("total", "amount", "subtotal") and m:
            return float(m.group(1))
        lo = schema.get("minimum", 0)
        return float(lo)

    def array(self, name: str, items: dict[str, Any]) -> list[Any]:
        if name == "emotions":
            return ["frustration"] if self.sentiment in ("negative", "very_negative") else []
        if name == "key_points":
            return [s.strip() for s in re.split(r"(?<=[.!?])\s+", self.raw) if len(s.strip()) > 20][:3]
        if name in ("categories",) and "enum" in items:
            return [self.enum("category", items["enum"])]
        return []

    def string(self, name: str) -> str:
        email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", self.raw)
        subject = re.search(r'"subject":\s*"([^"]+)"', self.raw) or re.search(r"Subject:\s*(.+)", self.raw)
        if name == "email":
            return email.group(0) if email else ""
        if name == "name":
            m = re.search(r"(?:I'm|I am|Regards,|Thanks,)\s+([A-Z][a-z]+)", self.raw)
            return m.group(1) if m else ""
        if name == "company":
            return email.group(0).split("@")[1].split(".")[0].title() if email else ""
        if name == "order_id":
            m = re.search(r"\b(?:INV|ORD|#)[-\s]?(\d{3,})", self.raw)
            return m.group(0) if m else ""
        if name == "reply_subject":
            return "Re: " + (subject.group(1).strip() if subject else "your request")
        if name == "reply_body":
            mentioned = sorted(
                (self.text.find(a["id"].lower()), i) for i, a in enumerate(KB) if a["id"].lower() in self.text
            )
            article = KB[mentioned[0][1]] if mentioned else KB[0]
            return (
                f"Hello,\n\nThanks for reaching out. {article['body']}\n\n"
                f'You can read more in our article "{article["title"]}" ({article["id"]}).\n\nBest regards,\nSupport team'
            )
        if name in ("summary",):
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", self.raw) if len(s.strip()) > 15]
            return " ".join(sentences[:2])[:600] or self.raw[:300]
        if name == "reasoning_summary":
            return f"Sandbox model: sentiment={self.sentiment}, risk={'high' if self.risky else 'low'}."
        if name == "language":
            return "en"
        if name == "issue_type":
            return _category(self.text, ["billing", "support", "sales", "complaint"])
        if name == "recommended_action":
            return "Schedule an executive business review and offer a usage workshop."
        return ""


@app.post("/v1/chat/completions")
async def chat(request: Request) -> dict[str, Any]:
    body = await request.json()
    messages = body.get("messages", [])
    user_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    schema = _schema_from_request(body)
    # FlowForge prompts wrap untrusted data in <input> tags; analyse that, not the instructions.
    blocks = re.findall(r"<input>(.*?)</input>", user_text, flags=re.S)
    gen = Generator("\n".join(blocks) if blocks else user_text)
    content = (
        json.dumps(gen.value("root", schema))
        if schema is not None
        else ("Sandbox response: " + (user_text[:200] or "ok"))
    )
    prompt_tokens = sum(len(m.get("content", "")) for m in messages) // 4 + 8
    completion_tokens = len(content) // 4 + 1
    return {
        "id": f"chatcmpl-{next(_ids)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "sandbox-llm"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "sandbox-llm", "object": "model"}, {"id": "sandbox-embed", "object": "model"}],
    }


@app.post("/v1/embeddings")
async def embeddings(body: dict[str, Any]) -> dict[str, Any]:
    inputs = body.get("input", [])
    inputs = [inputs] if isinstance(inputs, str) else inputs
    data = []
    for i, text in enumerate(inputs):
        digest = hashlib.sha256(str(text).encode()).digest()
        data.append({"index": i, "object": "embedding", "embedding": [(b - 128) / 128 for b in digest[:16]]})
    return {
        "object": "list",
        "data": data,
        "model": body.get("model", "sandbox-embed"),
        "usage": {"prompt_tokens": sum(len(str(t)) // 4 for t in inputs)},
    }


# ----------------------------------------------------------------------------- HubSpot subset


@app.post("/crm/v3/objects/contacts/search")
async def contact_search(body: dict[str, Any]) -> dict[str, Any]:
    filters = [f for g in body.get("filterGroups", []) for f in g.get("filters", [])]
    email = next((f["value"] for f in filters if f["propertyName"] == "email"), None)
    if email:
        c = CONTACTS.get(email.lower())
        return {"total": int(c is not None), "results": [c] if c else []}
    return {"total": len(CONTACTS), "results": list(CONTACTS.values())[: body.get("limit", 10)]}


@app.get("/crm/v3/objects/contacts")
async def contacts_list() -> dict[str, Any]:
    return {"results": list(CONTACTS.values())[:1]}


@app.post("/crm/v3/objects/contacts", status_code=201)
async def contact_create(body: dict[str, Any]) -> Any:
    props = body.get("properties", {})
    email = str(props.get("email", "")).lower()
    if email in CONTACTS:
        return JSONResponse({"message": "Contact already exists"}, status_code=409)
    contact = {"id": str(next(_ids)), "properties": {**props, "createdate": time.strftime("%Y-%m-%dT%H:%M:%SZ")}}
    CONTACTS[email] = contact
    STORE["contacts_created"].append(contact)
    return contact


@app.patch("/crm/v3/objects/contacts/{cid}")
async def contact_update(cid: str, body: dict[str, Any]) -> Any:
    for c in CONTACTS.values():
        if c["id"] == cid:
            c["properties"].update(body.get("properties", {}))
            return c
    return JSONResponse({"message": "not found"}, status_code=404)


@app.post("/crm/v3/objects/{obj}", status_code=201)
async def crm_create(obj: str, body: dict[str, Any]) -> dict[str, Any]:
    record = {"id": str(next(_ids)), **body}
    STORE[obj].append(record)
    return record


# ----------------------------------------------------------------------------- Helpdesk / enrichment / ERP


@app.get("/helpdesk/tickets")
async def helpdesk_tickets(email: str = "", limit: int = 5) -> list[dict[str, Any]]:
    return TICKETS.get(email.lower(), [])[:limit]


@app.get("/helpdesk/kb/search")
async def kb_search(q: str = "", limit: int = 3) -> list[dict[str, Any]]:
    terms = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 2]
    scored = sorted(KB, key=lambda a: -sum((a["title"] + " " + a["body"]).lower().count(t) for t in terms))
    return scored[:limit]


@app.get("/helpdesk/")
@app.get("/helpdesk")
async def helpdesk_root() -> dict[str, str]:
    return {"service": "helpdesk", "status": "ok"}


@app.get("/enrichment/companies")
async def enrich(domain: str = "") -> dict[str, Any]:
    known = {"globex.com": 12000, "initech.com": 450, "umbrella.com": 30000}
    return {
        "domain": domain,
        "name": domain.split(".")[0].title(),
        "employees": known.get(domain, 40),
        "industry": "Technology",
        "country": "US",
    }


@app.get("/erp/{collection}")
async def erp_list(collection: str) -> list[dict[str, Any]]:
    return STORE.get(f"erp_{collection}", [])


@app.post("/erp/{collection}", status_code=201)
async def erp_create(collection: str, body: dict[str, Any]) -> dict[str, Any]:
    record = {"id": f"{collection[:3].upper()}-{next(_ids)}", **body}
    STORE[f"erp_{collection}"].append(record)
    return record


# ----------------------------------------------------------------------------- Jira subset


@app.get("/rest/api/3/myself")
async def jira_me() -> dict[str, str]:
    return {"accountId": "sandbox", "displayName": "Sandbox Bot"}


@app.post("/rest/api/3/issue", status_code=201)
async def jira_create(body: dict[str, Any]) -> dict[str, Any]:
    fields = body.get("fields", {})
    n = next(_ids)
    issue = {"id": str(n), "key": f"{fields.get('project', {}).get('key', 'SUP')}-{n}", "fields": fields}
    STORE["tickets"].append(issue)
    return {"id": issue["id"], "key": issue["key"]}


@app.post("/rest/api/3/issue/{key}/comment", status_code=201)
async def jira_comment(key: str, body: dict[str, Any]) -> dict[str, Any]:
    STORE["ticket_comments"].append({"issue": key, **body})
    return {"id": str(next(_ids))}


@app.post("/rest/api/3/search/jql")
async def jira_search(body: dict[str, Any]) -> dict[str, Any]:
    issues = [
        {"id": i["id"], "key": i["key"], "fields": {**i["fields"], "status": {"name": "Open"}}}
        for i in STORE["tickets"]
    ]
    return {"issues": issues[: body.get("maxResults", 50)], "total": len(issues)}


# ----------------------------------------------------------------------------- Slack subset


@app.post("/api/chat.postMessage")
async def slack_post(body: dict[str, Any]) -> dict[str, Any]:
    ts = f"{time.time():.6f}"
    STORE["slack"].append({"channel": body.get("channel"), "text": body.get("text"), "ts": ts})
    return {"ok": True, "channel": body.get("channel"), "ts": ts}


@app.get("/api/chat.getPermalink")
async def slack_permalink(channel: str = "", message_ts: str = "") -> dict[str, Any]:
    return {"ok": True, "permalink": f"https://sandbox.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}


@app.post("/api/auth.test")
async def slack_auth() -> dict[str, Any]:
    return {"ok": True, "team": "Sandbox", "user": "flowforge", "bot_id": "B0SANDBOX"}


@app.get("/api/users.lookupByEmail")
async def slack_lookup(email: str = "") -> dict[str, Any]:
    return {"ok": True, "user": {"id": "U" + hashlib.md5(email.encode()).hexdigest()[:8].upper(), "real_name": email}}  # noqa: S324


# ----------------------------------------------------------------------------- SendGrid subset


@app.post("/v3/mail/send", status_code=202)
async def sendgrid_send(body: dict[str, Any]) -> JSONResponse:
    message_id = f"sg-{next(_ids)}"
    STORE["emails"].append(
        {
            "id": message_id,
            "to": [t["email"] for p in body.get("personalizations", []) for t in p.get("to", [])],
            "subject": body.get("subject"),
            "content": body.get("content"),
            "from": body.get("from"),
        }
    )
    return JSONResponse(None, status_code=202, headers={"X-Message-Id": message_id})


@app.get("/v3/scopes")
async def sendgrid_scopes() -> dict[str, Any]:
    return {"scopes": ["mail.send"]}
