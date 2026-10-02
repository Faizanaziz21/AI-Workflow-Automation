#!/usr/bin/env bash
# Measure webhook ingest capacity with several load-generator processes.
# One Python generator tops out around 80 signed requests/s on a small host, which is below what the API accepts,
# so a single process measures the generator, not the platform. Stop the workers first to isolate ingest:
#   docker compose stop worker && loadtest/parallel_ingest.sh 3 1500 && docker compose start worker
set -euo pipefail
GENERATORS=${1:-3}
PER_GENERATOR=${2:-1500}
OUT=${OUT:-loadtest/results}
pids=()
for i in $(seq 1 "$GENERATORS"); do
  python3 loadtest/flowforge_load.py --users 10 --workflows 60 --steady-seconds 0 --chaos 0 \
    --burst "$PER_GENERATOR" --burst-concurrency 120 --drain-timeout 2 \
    --label "ingest-${GENERATORS}-generators-g${i}" --out "$OUT" > "/tmp/ingest-g${i}.log" 2>&1 &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done
grep -h "accepted in" /tmp/ingest-g*.log
python3 - "$OUT" "$GENERATORS" <<'PY'
import glob, json, sys
out, n = sys.argv[1], sys.argv[2]
runs = sorted(glob.glob(f"{out}/*ingest-{n}-generators-g*.json"))[-int(n):]
total = sum(json.load(open(r))["burst"]["webhooks_accepted"] / json.load(open(r))["burst"]["ingest_seconds"] for r in runs)
print(f"aggregate ingest: {total:.0f} webhooks/s across {n} generators")
PY
