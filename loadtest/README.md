# Load and resilience tests

`flowforge_load.py` drives a running FlowForge deployment through its public API: it registers an isolated
organization, creates 100 users and 500 workflows, then runs steady, burst and chaos phases (injected API and LLM
latency, errors and hangs via the sandbox). Results land in `results/` as JSON and Markdown.

```bash
docker compose -f docker-compose.yml -f loadtest/compose.loadtest.yml up -d
python loadtest/flowforge_load.py --label my-run
```

`compose.loadtest.yml` only raises the per-IP edge and login limits (all traffic comes from one IP) and exposes
the sandbox's chaos API. Per-identity API limits stay at their defaults.

See [docs/TESTING.md](../docs/TESTING.md#load-testing) for the scenario design and
[docs/BENCHMARKS.md](../docs/BENCHMARKS.md) for recorded results.
