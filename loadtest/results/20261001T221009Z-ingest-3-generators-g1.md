# FlowForge load test — par1

Started 2026-10-01T22:09:40.740207+00:00 against `http://localhost:8080`.

**Setup:** 10 users and 60 published workflows in 7.3 s.

## Execution engine

| Phase | Executions | Completed | Failed | Failure rate | Throughput | E2E p50 | E2E p95 | Node retries | Drain after load |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| burst | 1,500 | 0 | 0 | 0.00% | 0.0/s | 0 ms | 0 ms | 0 | timeout s |

## API latency

| Phase | Endpoint | Requests | RPS | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|
| burst | `POST /hooks/{token} (burst)` | 1,500 | 82.7 | 786.0 ms | 4033.8 ms | 7013.2 ms | 0.00% |

## Queue behaviour

| Phase | Max ready | Max running | Max delayed (timers + retry backoff) | Oldest ready job |
|---|---:|---:|---:|---:|
| burst | 22,500 | 0 | 22,508 | 845.26 s |

**burst:** 1,500 webhooks to 60 workflows, 1,500 accepted in 18.13 s (82.7/s ingest).
