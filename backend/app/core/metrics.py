"""Prometheus metrics shared by API and worker processes."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("flowforge_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "flowforge_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

EXECUTIONS_STARTED = Counter("flowforge_executions_started_total", "Executions created", ["trigger_type"])
EXECUTIONS_FINISHED = Counter("flowforge_executions_finished_total", "Executions finished", ["status"])
EXECUTION_DURATION = Histogram(
    "flowforge_execution_duration_seconds",
    "End-to-end execution duration",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, 1800, 3600, 86400),
)
NODE_RUNS = Counter("flowforge_node_runs_total", "Node executions", ["node_type", "status"])
NODE_DURATION = Histogram(
    "flowforge_node_duration_seconds",
    "Node handler duration",
    ["node_type"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
NODE_RETRIES = Counter("flowforge_node_retries_total", "Node retry attempts scheduled", ["node_type"])
JOBS_PROCESSED = Counter("flowforge_jobs_processed_total", "Queue jobs processed", ["kind", "outcome"])
JOB_LATENCY = Histogram(
    "flowforge_job_queue_latency_seconds",
    "Delay between job availability and claim",
    ["kind"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
QUEUE_DEPTH = Gauge("flowforge_queue_depth", "Ready jobs waiting in queue", ["queue"])
WORKER_INFLIGHT = Gauge("flowforge_worker_inflight_jobs", "Jobs currently executing in this worker")
AI_CALLS = Counter("flowforge_ai_calls_total", "LLM calls", ["provider", "model", "status"])
AI_TOKENS = Counter("flowforge_ai_tokens_total", "LLM tokens", ["provider", "model", "direction"])
AI_COST = Counter("flowforge_ai_cost_usd_total", "LLM cost in USD", ["provider", "model"])
AI_LATENCY = Histogram(
    "flowforge_ai_latency_seconds", "LLM call latency", ["provider"], buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)
)
CONNECTOR_CALLS = Counter(
    "flowforge_connector_calls_total", "Connector action calls", ["connector", "action", "status"]
)
WEBHOOKS_RECEIVED = Counter("flowforge_webhooks_received_total", "Inbound webhooks", ["outcome"])
APPROVALS = Counter("flowforge_approvals_total", "Approval decisions", ["decision"])
