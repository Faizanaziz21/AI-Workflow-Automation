# FlowForge load test — ingest-only-workers-stopped

Started 2026-10-01T21:34:34.361797+00:00 against `http://localhost:8080`.

**Setup:** 100 users and 500 published workflows in 14.4 s.

## Execution engine

| Phase | Executions | Completed | Failed | Failure rate | Throughput | E2E p50 | E2E p95 | Node retries | Drain after load |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| burst | 3,000 | 0 | 0 | 0.00% | 0.0/s | 0 ms | 0 ms | 0 | timeout s |

## API latency

| Phase | Endpoint | Requests | RPS | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|
| burst | `POST /hooks/{token} (burst)` | 3,000 | 70.9 | 2244.5 ms | 8494.2 ms | 9922.8 ms | 0.00% |

## Queue behaviour

| Phase | Max ready | Max running | Max delayed (timers + retry backoff) | Oldest ready job |
|---|---:|---:|---:|---:|
| burst | 3,000 | 0 | 3,006 | 45.05 s |

**burst:** 3,000 webhooks to 500 workflows, 3,000 accepted in 42.34 s (70.9/s ingest).
