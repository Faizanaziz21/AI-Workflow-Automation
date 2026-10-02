# FlowForge load test — ingest-only-workers-stopped

Started 2026-10-01T22:07:59.054388+00:00 against `http://localhost:8080`.

**Setup:** 100 users and 500 published workflows in 11.2 s.

## Execution engine

| Phase | Executions | Completed | Failed | Failure rate | Throughput | E2E p50 | E2E p95 | Node retries | Drain after load |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| burst | 3,000 | 0 | 0 | 0.00% | 0.0/s | 0 ms | 0 ms | 0 | timeout s |

## API latency

| Phase | Endpoint | Requests | RPS | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|
| burst | `POST /hooks/{token} (burst)` | 3,000 | 78.2 | 2260.5 ms | 7795.4 ms | 9229.3 ms | 0.00% |

## Queue behaviour

| Phase | Max ready | Max running | Max delayed (timers + retry backoff) | Oldest ready job |
|---|---:|---:|---:|---:|
| burst | 15,000 | 0 | 15,008 | 768.74 s |

**burst:** 3,000 webhooks to 500 workflows, 3,000 accepted in 38.38 s (78.2/s ingest).
