#!/usr/bin/env python3
"""FlowForge AI load & resilience harness.

Drives a running FlowForge deployment through its public API only (no database access), in four phases:

1. **setup**   - register an isolated organization, provision an AI connection to the sandbox, create N users
                 (workflow developers), log every user in, and have them author + publish M workflows
                 (three shapes: pure logic, integration + AI, parallel fan-out) with signed webhook triggers.
2. **steady**  - N concurrent virtual users for a fixed duration: signed webhooks, execution list/inspect,
                 dashboard, workflow catalog. Measures API latency percentiles and engine throughput.
3. **burst**   - thousands of simultaneous webhooks across all workflows; measures ingest latency, queue depth
                 and the time the worker pool needs to drain the backlog.
4. **chaos**   - the sandbox injects latency, 5xx failures and hung requests (incl. the LLM endpoint) while
                 webhooks keep arriving; measures retries, failure rate and end-to-end latency under faults.

Results are written as JSON (raw numbers) and Markdown (report) to ``--out``.

    pip install httpx
    python loadtest/flowforge_load.py --base-url http://localhost:8080 --sandbox-control http://localhost:9000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import platform
import random
import secrets
import statistics
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
ACTIVE = ["PENDING", "RUNNING", "RETRYING"]


# ----------------------------------------------------------------------------- metrics


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 1) if values else 0.0,
        "p50": round(pct(values, 0.50), 1),
        "p95": round(pct(values, 0.95), 1),
        "p99": round(pct(values, 0.99), 1),
        "max": round(max(values), 1) if values else 0.0,
    }


@dataclass
class Recorder:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[str, dict[int, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, label: str, ms: float, status: int) -> None:
        self.latencies[label].append(ms)
        self.statuses[label][status] += 1

    def report(self, elapsed: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for label, values in sorted(self.latencies.items()):
            st = self.statuses[label]
            bad = sum(c for code, c in st.items() if code >= 400 or code == 0)
            out[label] = {
                **summarize(values),
                "rps": round(len(values) / elapsed, 1) if elapsed else 0.0,
                "error_rate": round(bad / max(1, len(values)), 4),
                "statuses": {str(k): v for k, v in sorted(st.items())},
            }
        return out


class Api:
    """Thin httpx wrapper that records latency per logical endpoint label."""

    def __init__(self, client: httpx.AsyncClient, rec: Recorder, token: str | None = None) -> None:
        self.client, self.rec, self.token = client, rec, token

    async def call(self, label: str, method: str, path: str, *, expect: tuple[int, ...] = (200,), **kw: Any) -> Any:
        headers = kw.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(6):
            t0 = time.perf_counter()
            try:
                r = await self.client.request(method, path, headers=headers, **kw)
            except httpx.HTTPError as exc:
                self.rec.add(label, (time.perf_counter() - t0) * 1000, 0)
                self.rec.errors[f"{label}: {type(exc).__name__}"] += 1
                if attempt == 5:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            self.rec.add(label, (time.perf_counter() - t0) * 1000, r.status_code)
            if r.status_code == 429 and attempt < 5:  # honour the server's rate limiter like a well-behaved client
                await asyncio.sleep(float(r.headers.get("Retry-After", "1")))
                continue
            if r.status_code not in expect:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise RuntimeError(f"{method} {path}: retries exhausted")


# ----------------------------------------------------------------------------- workflow shapes


def _retry(attempts: int, initial: float) -> dict[str, Any]:
    return {"max_attempts": attempts, "initial_interval_seconds": initial, "backoff_coefficient": 2.0}


def _n(nid: str, type_: str, config: dict[str, Any], x: int, y: int = 0, **extra: Any) -> dict[str, Any]:
    return {"id": nid, "type": type_, "name": nid.replace("_", " ").title(), "config": config,
            "position": {"x": x, "y": y}, **extra}


def _e(src: str, dst: str, handle: str = "out") -> dict[str, Any]:
    return {"source": src, "target": dst, "source_handle": handle}


def wf_logic() -> dict[str, Any]:
    """Pure engine work: 5 nodes, one branch."""
    return {
        "nodes": [
            _n("hook", "trigger.webhook", {}, 0),
            _n("normalize", "logic.set_variable", {"assignments": {
                "order_id": "{{ trigger.body.id }}", "amount": "{{ trigger.body.amount }}"}}, 260),
            _n("is_large", "logic.if", {"condition": "{{ trigger.body.amount > 1000 }}"}, 520),
            _n("high", "logic.set_variable", {"assignments": {"tier": "high"}}, 780, -80),
            _n("low", "logic.set_variable", {"assignments": {"tier": "low"}}, 780, 80),
        ],
        "edges": [_e("hook", "normalize"), _e("normalize", "is_large"),
                  _e("is_large", "high", "true"), _e("is_large", "low", "false")],
        "settings": {},
    }


def wf_integration(sandbox: str, ai_conn: str) -> dict[str, Any]:
    """External API + LLM classification + write-back, with retries and per-node timeouts."""
    return {
        "nodes": [
            _n("hook", "trigger.webhook", {}, 0),
            _n("enrich", "data.http_request", {
                "url": f"{sandbox}/enrichment/companies", "query": {"domain": "{{ trigger.body.domain }}"},
                "timeout_seconds": 5}, 260, retry=_retry(4, 0.5)),
            _n("classify", "ai.classify", {
                "text": "{{ trigger.body.message }}",
                "categories": [{"name": "hot", "description": "Ready to buy"},
                               {"name": "warm", "description": "Interested, needs nurturing"},
                               {"name": "cold", "description": "No buying intent"}],
                "model": {"connection_id": ai_conn, "max_repair_attempts": 1}}, 520,
               retry=_retry(3, 1.0), timeout_seconds=10),
            _n("record", "data.http_request", {
                "method": "POST", "url": f"{sandbox}/erp/leads", "timeout_seconds": 5,
                "body": {"domain": "{{ trigger.body.domain }}",
                         "employees": "{{ nodes.enrich.output.body.employees }}",
                         "category": "{{ nodes.classify.output.category }}"}}, 780, retry=_retry(4, 0.5)),
        ],
        "edges": [_e("hook", "enrich"), _e("enrich", "classify"), _e("classify", "record")],
        "settings": {},
    }


def wf_fanout(sandbox: str) -> dict[str, Any]:
    """Parallel fan-out to three API calls joined by a merge."""
    nodes = [_n("hook", "trigger.webhook", {}, 0), _n("split", "logic.parallel", {"branches": 3}, 260)]
    edges = [_e("hook", "split")]
    for i in range(1, 4):
        nid = f"notify_{i}"
        nodes.append(_n(nid, "data.http_request", {
            "method": "POST", "url": f"{sandbox}/erp/events", "timeout_seconds": 5,
            "body": {"channel": i, "ref": "{{ trigger.body.id }}"}}, 520, (i - 2) * 120, retry=_retry(4, 0.5)))
        edges += [_e("split", nid, f"branch_{i}"), _e(nid, "join")]
    nodes += [_n("join", "logic.merge", {"mode": "list"}, 780),
              _n("done", "logic.set_variable", {"assignments": {"fanned_out": 3}}, 1040)]
    edges.append(_e("join", "done"))
    return {"nodes": nodes, "edges": edges, "settings": {}}


@dataclass
class Hook:
    workflow_id: str
    shape: str
    path: str
    secret: str


@dataclass
class VirtualUser:
    email: str
    api: Api
    hooks: list[Hook] = field(default_factory=list)


MESSAGES = [
    "We'd like a quote for 200 seats and a demo next week.",
    "Just browsing, maybe next year.",
    "Our team is evaluating vendors; can you send pricing for the enterprise plan?",
    "Please remove me from your list.",
]
DOMAINS = ["globex.com", "initech.com", "umbrella.com", "example.org", "acme.io"]


def webhook_body(shape: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "amount": random.randint(10, 5000),  # noqa: S311 - load generation, not security
        "domain": random.choice(DOMAINS),  # noqa: S311
        "message": random.choice(MESSAGES),  # noqa: S311
        "shape": shape,
    }


async def fire(api: Api, hook: Hook, label: str) -> str | None:
    raw = json.dumps(webhook_body(hook.shape)).encode()
    ts = int(time.time())
    sig = "v1=" + hmac.new(hook.secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    headers = {"X-FlowForge-Signature": sig, "X-FlowForge-Timestamp": str(ts),
               "Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex}
    try:
        out = await Api(api.client, api.rec).call(label, "POST", hook.path, content=raw, headers=headers,
                                                   expect=(202, 200))
        return out["execution_id"]
    except Exception as exc:  # noqa: BLE001 - recorded, keep generating load
        api.rec.errors[f"{label}: {str(exc)[:120]}"] += 1
        return None


# ----------------------------------------------------------------------------- harness


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rec = Recorder()
        limits = httpx.Limits(max_connections=args.max_connections, max_keepalive_connections=args.max_connections)
        self.client = httpx.AsyncClient(base_url=args.base_url, timeout=httpx.Timeout(60.0), limits=limits)
        self.admin: Api | None = None
        self.workspace_id = ""
        self.users: list[VirtualUser] = []
        self.results: dict[str, Any] = {}

    # -- setup -------------------------------------------------------------------------------------------------

    async def setup(self) -> None:
        a = self.args
        rec = Recorder()
        t0 = time.perf_counter()
        run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
        self.password = "Ld-" + secrets.token_urlsafe(12) + "9!"
        anon = Api(self.client, rec)
        reg = await anon.call("POST /auth/register-org", "POST", "/api/v1/auth/register-org", expect=(201,), json={
            "organization_name": f"Load Test {run_id}", "email": f"admin-{run_id}@loadtest.flowforge.io",
            "full_name": "Load Admin", "password": self.password})
        self.admin = Api(self.client, self.rec, reg["access_token"])
        admin_setup = Api(self.client, rec, reg["access_token"])
        self.workspace_id = (await admin_setup.call("GET /workspaces", "GET", "/api/v1/workspaces"))[0]["id"]
        ws = f"/api/v1/workspaces/{self.workspace_id}"
        conn = await admin_setup.call("POST /connections", "POST", f"{ws}/connections", expect=(201,), json={
            "name": "Sandbox LLM", "connector_key": "openai_compatible",
            "config": {"base_url": f"{a.sandbox_internal}/v1", "default_model": "sandbox-llm",
                       "json_mode": "json_object"}, "credentials": {}})
        ai_conn = conn["id"]
        print(f"  org registered ({run_id}); workspace {self.workspace_id}", flush=True)

        sem = asyncio.Semaphore(20)

        async def make_user(i: int) -> VirtualUser:
            email = f"dev{i:03d}-{run_id}@loadtest.flowforge.io"
            async with sem:
                await admin_setup.call("POST /users", "POST", "/api/v1/users", expect=(201,), json={
                    "email": email, "full_name": f"Developer {i}", "password": self.password,
                    "role": "workflow_developer"})
                tok = await anon.call("POST /auth/login", "POST", "/api/v1/auth/login",
                                      json={"email": email, "password": self.password})
            return VirtualUser(email, Api(self.client, self.rec, tok["access_token"]))

        self.users = list(await asyncio.gather(*(make_user(i) for i in range(a.users))))
        print(f"  {len(self.users)} users created and logged in", flush=True)

        shapes = ["logic", "integration", "fanout"]

        async def make_workflow(j: int) -> None:
            user = self.users[j % len(self.users)]
            shape = shapes[j % len(shapes)]
            definition = {"logic": wf_logic, "integration": lambda: wf_integration(a.sandbox_internal, ai_conn),
                          "fanout": lambda: wf_fanout(a.sandbox_internal)}[shape]()
            api = Api(self.client, rec, user.api.token)
            async with sem:
                wf = await api.call("POST /workflows", "POST", f"{ws}/workflows", expect=(201,), json={
                    "name": f"{shape.title()} flow {j:03d}", "description": "load test", "definition": definition})
                await api.call("POST /workflows/{id}/publish", "POST", f"{ws}/workflows/{wf['id']}/publish",
                               json={"change_note": "load test"})
                info = await api.call("GET /workflows/{id}/trigger", "GET", f"{ws}/workflows/{wf['id']}/trigger",
                                      params={"reveal_secret": "true"})
            token = info["webhook_url"].rsplit("/", 1)[-1]
            user.hooks.append(Hook(wf["id"], shape, f"/api/v1/hooks/{token}", info["signing_secret"]))

        await asyncio.gather(*(make_workflow(j) for j in range(a.workflows)))
        elapsed = time.perf_counter() - t0
        print(f"  {a.workflows} workflows authored, published and armed in {elapsed:.1f}s", flush=True)
        self.results["setup"] = {"seconds": round(elapsed, 1), "users": a.users, "workflows": a.workflows,
                                 "api": rec.report(elapsed)}

    # -- observation -------------------------------------------------------------------------------------------

    async def queue_sampler(self, stop: asyncio.Event, samples: list[dict[str, Any]]) -> None:
        assert self.admin
        t0 = time.perf_counter()
        quiet = Api(self.client, Recorder(), self.admin.token)  # keep monitoring out of the measured API mix
        while not stop.is_set():
            try:
                q = await quiet.call("queue", "GET", f"/api/v1/workspaces/{self.workspace_id}/queue")
                running = sum(r["count"] for r in q["by_status"] if r["status"] == "running")
                samples.append({"t": round(time.perf_counter() - t0, 1), "ready": q["ready"],
                                "delayed": q["delayed"], "running": running,
                                "oldest_ready_s": round(q["oldest_ready_age_seconds"], 2)})
            except Exception:  # noqa: BLE001 - sampling is best effort
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.args.sample_interval)
            except TimeoutError:
                pass

    async def count(self, since: str, statuses: list[str] | None = None) -> int:
        assert self.admin
        quiet = Api(self.client, Recorder(), self.admin.token)
        params: list[tuple[str, str]] = [("since", since), ("limit", "1"), ("workspace_id", self.workspace_id)]
        params += [("status", s) for s in statuses or []]
        return (await quiet.call("count", "GET", "/api/v1/executions", params=params))["total"]

    async def wait_drained(self, since: str, timeout: float) -> float:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            if await self.count(since, ACTIVE) == 0:
                return time.perf_counter() - t0
            await asyncio.sleep(1.0)
        return -1.0

    async def collect_executions(self, since: str) -> list[dict[str, Any]]:
        assert self.admin
        quiet = Api(self.client, Recorder(), self.admin.token)
        items: list[dict[str, Any]] = []
        before: str | None = None
        while True:
            params = {"since": since, "limit": "200", "workspace_id": self.workspace_id}
            if before:
                params["before"] = before
            page = await quiet.call("collect", "GET", "/api/v1/executions", params=params)
            items += page["items"]
            before = page["next_cursor"]
            if not before:
                return items

    async def dashboard(self) -> dict[str, Any]:
        assert self.admin
        quiet = Api(self.client, Recorder(), self.admin.token)
        return await quiet.call("dash", "GET", "/api/v1/dashboard/summary",
                                params={"window": "24h", "workspace_id": self.workspace_id})

    def execution_report(self, items: list[dict[str, Any]], dash_before: dict[str, Any],
                         dash_after: dict[str, Any]) -> dict[str, Any]:
        def ts(s: str) -> float:
            return datetime.fromisoformat(s).timestamp()

        by_status: dict[str, int] = defaultdict(int)
        e2e, run = [], []
        first_created, last_finished = float("inf"), 0.0
        for ex in items:
            by_status[ex["status"]] += 1
            if ex["status"] in TERMINAL and ex["finished_at"] and ex["started_at"]:
                c, s, f = ts(ex["created_at"]), ts(ex["started_at"]), ts(ex["finished_at"])
                e2e.append((f - c) * 1000)
                run.append((f - s) * 1000)
                first_created, last_finished = min(first_created, c), max(last_finished, f)
        done = sum(by_status[s] for s in TERMINAL)
        span = last_finished - first_created if done else 0.0

        def delta(path: list[str]) -> int:
            def get(d: dict[str, Any]) -> int:
                for k in path:
                    d = d.get(k, {}) if isinstance(d, dict) else {}
                return int(d) if isinstance(d, (int, float)) else 0
            return get(dash_after) - get(dash_before)

        return {
            "total": len(items),
            "by_status": dict(sorted(by_status.items())),
            "failure_rate": round(by_status["FAILED"] / max(1, done), 4),
            "throughput_per_s": round(done / span, 1) if span else 0.0,
            "end_to_end_ms": summarize(e2e),
            "run_time_ms": summarize(run),
            "node_retries_scheduled": delta(["retries", "scheduled"]),
            "integration_calls": delta(["integrations", "calls"]),
            "integration_errored_attempts": delta(["integrations", "errored_attempts"]),
        }

    @staticmethod
    def queue_report(samples: list[dict[str, Any]]) -> dict[str, Any]:
        if not samples:
            return {}
        return {
            "samples": len(samples),
            "max_ready": max(s["ready"] for s in samples),
            "max_running": max(s["running"] for s in samples),
            "max_delayed": max(s["delayed"] for s in samples),
            "max_oldest_ready_s": max(s["oldest_ready_s"] for s in samples),
            "series": samples,
        }

    async def observe(self, name: str, body: Any) -> dict[str, Any]:
        """Run ``body`` while sampling the queue; then wait for drain and collect execution stats."""
        since = datetime.now(UTC).isoformat()
        dash_before = await self.dashboard()
        stop, samples = asyncio.Event(), []
        sampler = asyncio.create_task(self.queue_sampler(stop, samples))
        self.rec = Recorder()
        for u in self.users:
            u.api.rec = self.rec
        t0 = time.perf_counter()
        extra = await body()
        load_seconds = time.perf_counter() - t0
        drain = await self.wait_drained(since, self.args.drain_timeout)
        stop.set()
        await sampler
        await asyncio.sleep(1.0)
        items = await self.collect_executions(since)
        dash_after = await self.dashboard()
        res = {
            "load_seconds": round(load_seconds, 1),
            "drain_seconds_after_load": round(drain, 1) if drain >= 0 else "timeout",
            "api": self.rec.report(load_seconds),
            "client_errors": dict(self.rec.errors),
            "executions": self.execution_report(items, dash_before, dash_after),
            "queue": self.queue_report(samples),
            **(extra or {}),
        }
        self.results[name] = res
        ex = res["executions"]
        print(f"  {name}: {ex['total']} executions {ex['by_status']} | throughput {ex['throughput_per_s']}/s | "
              f"e2e p95 {ex['end_to_end_ms']['p95']} ms | drain {res['drain_seconds_after_load']}s", flush=True)
        return res

    # -- phases ------------------------------------------------------------------------------------------------

    async def steady(self) -> None:
        a = self.args
        ws = f"/api/v1/workspaces/{self.workspace_id}"

        async def vu(user: VirtualUser, deadline: float) -> None:
            recent: list[str] = []
            await asyncio.sleep(random.random() * 2)  # noqa: S311 - ramp-up
            while time.perf_counter() < deadline:
                r = random.random()  # noqa: S311
                try:
                    if r < 0.55:
                        ex = await fire(user.api, random.choice(user.hooks), "POST /hooks/{token}")  # noqa: S311
                        if ex:
                            recent = [ex, *recent[:9]]
                    elif r < 0.70:
                        await user.api.call("GET /executions", "GET", "/api/v1/executions",
                                            params={"workspace_id": self.workspace_id, "limit": 25})
                    elif r < 0.80 and recent:
                        await user.api.call("GET /executions/{id}", "GET", f"/api/v1/executions/{recent[0]}")
                        await user.api.call("GET /executions/{id}/nodes", "GET",
                                            f"/api/v1/executions/{recent[0]}/nodes")
                    elif r < 0.88:
                        await user.api.call("GET /dashboard/summary", "GET", "/api/v1/dashboard/summary",
                                            params={"window": "1h", "workspace_id": self.workspace_id})
                    elif r < 0.96:
                        await user.api.call("GET /workflows", "GET", f"{ws}/workflows", params={"limit": 25})
                    else:
                        wf = random.choice(user.hooks).workflow_id  # noqa: S311
                        await user.api.call("GET /workflows/{id}", "GET", f"{ws}/workflows/{wf}")
                except Exception as exc:  # noqa: BLE001
                    self.rec.errors[str(exc)[:120]] += 1
                await asyncio.sleep(random.uniform(a.think_min, a.think_max))  # noqa: S311

        async def body() -> dict[str, Any]:
            deadline = time.perf_counter() + a.steady_seconds
            await asyncio.gather(*(vu(u, deadline) for u in self.users))
            return {"virtual_users": len(self.users)}

        await self.observe("steady", body)

    async def _blast(self, count: int, shapes: set[str], label: str) -> dict[str, Any]:
        hooks = [(u, h) for u in self.users for h in u.hooks if h.shape in shapes]
        sem = asyncio.Semaphore(self.args.burst_concurrency)
        accepted = 0

        async def one(i: int) -> None:
            nonlocal accepted
            user, hook = hooks[i % len(hooks)]
            async with sem:
                if await fire(user.api, hook, label):
                    accepted += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(count)))
        ingest = time.perf_counter() - t0
        return {"webhooks_sent": count, "webhooks_accepted": accepted, "ingest_seconds": round(ingest, 2),
                "ingest_rate_per_s": round(count / ingest, 1), "target_workflows": len(hooks)}

    async def burst(self) -> None:
        await self.observe("burst", lambda: self._blast(self.args.burst, {"logic", "integration", "fanout"},
                                                         "POST /hooks/{token} (burst)"))

    async def chaos(self) -> None:
        a = self.args
        chaos_cfg = {"latency_ms": a.chaos_latency_ms, "jitter_ms": a.chaos_jitter_ms,
                     "failure_rate": a.chaos_failure_rate, "timeout_rate": a.chaos_timeout_rate,
                     "timeout_ms": a.chaos_timeout_ms, "paths": []}
        async with httpx.AsyncClient(base_url=a.sandbox_control, timeout=10) as sb:
            (await sb.post("/_chaos", json=chaos_cfg)).raise_for_status()
            try:
                async def body() -> dict[str, Any]:
                    out = await self._blast(a.chaos, {"integration", "fanout"}, "POST /hooks/{token} (chaos)")
                    return {**out, "faults": chaos_cfg}
                await self.observe("chaos", body)
            finally:
                await sb.post("/_reset")

    async def run(self) -> dict[str, Any]:
        a = self.args
        started = datetime.now(UTC)
        print("setup", flush=True)
        await self.setup()
        if a.steady_seconds > 0:
            print(f"steady: {a.users} virtual users for {a.steady_seconds}s", flush=True)
            await self.steady()
        if a.burst > 0:
            print(f"burst: {a.burst} simultaneous webhooks", flush=True)
            await self.burst()
        if a.chaos > 0:
            print(f"chaos: {a.chaos} webhooks against a degraded sandbox", flush=True)
            await self.chaos()
        await self.client.aclose()
        self.results["meta"] = {
            "started_at": started.isoformat(), "base_url": a.base_url, "label": a.label,
            "client_host": f"{platform.system()} {platform.machine()}, python {platform.python_version()}",
            "params": {k: v for k, v in vars(a).items() if k not in {"out"}},
        }
        return self.results


# ----------------------------------------------------------------------------- report


def markdown(res: dict[str, Any]) -> str:
    m = res["meta"]
    lines = [f"# FlowForge load test — {m['label']}", "", f"Started {m['started_at']} against `{m['base_url']}`.", ""]
    s = res["setup"]
    lines += [f"**Setup:** {s['users']} users and {s['workflows']} published workflows in {s['seconds']} s.", ""]
    lines += ["## Execution engine", "",
              "| Phase | Executions | Completed | Failed | Failure rate | Throughput | E2E p50 | E2E p95 | "
              "Node retries | Drain after load |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for phase in ("steady", "burst", "chaos"):
        if phase not in res:
            continue
        p, ex = res[phase], res[phase]["executions"]
        lines.append(
            f"| {phase} | {ex['total']:,} | {ex['by_status'].get('COMPLETED', 0):,} | "
            f"{ex['by_status'].get('FAILED', 0):,} | {ex['failure_rate']:.2%} | {ex['throughput_per_s']}/s | "
            f"{ex['end_to_end_ms']['p50']:,.0f} ms | {ex['end_to_end_ms']['p95']:,.0f} ms | "
            f"{ex['node_retries_scheduled']:,} | "
            f"{p['drain_seconds_after_load']} s |")
    lines += ["", "## API latency", "", "| Phase | Endpoint | Requests | RPS | p50 | p95 | p99 | Errors |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for phase in ("steady", "burst", "chaos"):
        for label, st in res.get(phase, {}).get("api", {}).items():
            lines.append(f"| {phase} | `{label}` | {st['count']:,} | {st['rps']} | {st['p50']} ms | {st['p95']} ms | "
                         f"{st['p99']} ms | {st['error_rate']:.2%} |")
    lines += ["", "## Queue behaviour", "", "| Phase | Max ready | Max running | Max delayed (timers + retry backoff) | "
              "Oldest ready job |", "|---|---:|---:|---:|---:|"]
    for phase in ("steady", "burst", "chaos"):
        q = res.get(phase, {}).get("queue")
        if q:
            lines.append(f"| {phase} | {q['max_ready']:,} | {q['max_running']} | {q['max_delayed']:,} | "
                         f"{q['max_oldest_ready_s']} s |")
    for phase in ("burst", "chaos"):
        if phase in res:
            p = res[phase]
            lines += ["", f"**{phase}:** {p['webhooks_sent']:,} webhooks to {p['target_workflows']} workflows, "
                      f"{p['webhooks_accepted']:,} accepted in {p['ingest_seconds']} s "
                      f"({p['ingest_rate_per_s']}/s ingest)."]
    if "chaos" in res:
        f = res["chaos"]["faults"]
        lines += ["", f"Chaos faults on every sandbox call (HTTP APIs and the LLM): +{f['latency_ms']} ms "
                  f"(+0–{f['jitter_ms']} ms jitter), {f['failure_rate']:.0%} HTTP 503, "
                  f"{f['timeout_rate']:.0%} hung for {f['timeout_ms'] / 1000:.0f} s."]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--sandbox-control", default="http://localhost:9000", help="Sandbox URL reachable from here")
    p.add_argument("--sandbox-internal", default="http://sandbox:9000", help="Sandbox URL reachable from workers")
    p.add_argument("--users", type=int, default=100)
    p.add_argument("--workflows", type=int, default=500)
    p.add_argument("--steady-seconds", type=int, default=120)
    p.add_argument("--think-min", type=float, default=2.0)
    p.add_argument("--think-max", type=float, default=4.0)
    p.add_argument("--burst", type=int, default=3000)
    p.add_argument("--burst-concurrency", type=int, default=250)
    p.add_argument("--chaos", type=int, default=1000)
    p.add_argument("--chaos-latency-ms", type=int, default=300)
    p.add_argument("--chaos-jitter-ms", type=int, default=700)
    p.add_argument("--chaos-failure-rate", type=float, default=0.15)
    p.add_argument("--chaos-timeout-rate", type=float, default=0.05)
    p.add_argument("--chaos-timeout-ms", type=int, default=20000)
    p.add_argument("--drain-timeout", type=float, default=900)
    p.add_argument("--sample-interval", type=float, default=1.0)
    p.add_argument("--max-connections", type=int, default=300)
    p.add_argument("--label", default="local")
    p.add_argument("--out", default="loadtest/results")
    args = p.parse_args()

    results = asyncio.run(Harness(args).run())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{args.label}"
    (out / f"{stem}.json").write_text(json.dumps(results, indent=2))
    (out / f"{stem}.md").write_text(markdown(results))
    print(f"\nwrote {out / stem}.json and .md\n")
    print(markdown(results))


if __name__ == "__main__":
    sys.exit(main())
