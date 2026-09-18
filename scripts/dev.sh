#!/usr/bin/env bash
#
# Run the whole system for development: API, worker, and the Vite dev server.
#
# Three processes because they are three concerns: the API answers, the worker processes,
# and Vite rebuilds. Running them under one script rather than in three terminals matters
# for a specific reason -- with no worker running, `sync` enqueues jobs that never execute,
# so the UI shows an empty library and nothing anywhere reports an error. The most confusing
# possible first-run state is the one you get by forgetting a terminal.
#
# Usage:
#   scripts/dev.sh              # API + worker + Vite (frontend on :5173, API on :8787)
#   scripts/dev.sh --no-frontend
#   scripts/dev.sh --seed       # sync and process the fixture corpus first
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_FRONTEND=1
SEED=0
for arg in "$@"; do
  case "$arg" in
    --no-frontend) WITH_FRONTEND=0 ;;
    --seed) SEED=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

DK="python -m douyin_knowledge.cli"
cd "$ROOT/backend"

# Migrations before anything binds a port. An API serving a stale schema fails per-request
# with a confusing SQL error rather than once, clearly, at startup.
$DK db upgrade

if [[ "$SEED" == "1" ]]; then
  $DK sync --process --wait
fi

pids=()
cleanup() {
  # Kill the group, not just the direct children: uvicorn --reload spawns a reloader whose
  # child holds the port, and leaving it behind makes the next run fail with EADDRINUSE.
  for pid in "${pids[@]:-}"; do
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

set -m
$DK serve --reload &
pids+=($!)

$DK worker &
pids+=($!)

if [[ "$WITH_FRONTEND" == "1" ]]; then
  if [[ -d "$ROOT/frontend/node_modules" ]]; then
    (cd "$ROOT/frontend" && npm run dev) &
    pids+=($!)
  else
    echo "frontend/node_modules missing -- run 'npm install' in frontend/, or use --no-frontend" >&2
  fi
fi
set +m

echo
echo "  API       http://127.0.0.1:${DK_API_PORT:-8787}"
[[ "$WITH_FRONTEND" == "1" ]] && echo "  frontend  http://127.0.0.1:5173  (proxies /api to the API)"
echo "  ctrl-c stops everything"
echo

# Exit as soon as any one of them dies. Without this the script would sit there looking
# healthy while, say, the worker had crashed -- which presents as jobs silently not running.
wait -n
echo "a dev process exited; shutting the rest down" >&2
