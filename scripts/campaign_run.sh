#!/usr/bin/env bash
# Phase 1 / Phase 3 runner: drive N Hugin editions through the router.
#
#   ROUTER_URL=http://127.0.0.1:4000 HUGIN_DIR=../gimle-hugin \
#     scripts/campaign_run.sh 20 [app-name] [app args...]
#
# Anything after the app name is passed through to the app (after `--`), e.g.
#   scripts/campaign_run.sh 20 financial_newspaper --symbols AAPL MSFT
# Apps run headless by default (monitors are opt-in flags).
#
# Every edition tags its calls (x-ctrlrtn-task = session, x-ctrlrtn-route = agent
# role) and reports its outcome to the router — that is what phases 2 and 3
# analyze. The router must already be running (`ctrlrtn serve`).
set -euo pipefail

N="${1:?usage: campaign_run.sh <editions> [app] [app args...]}"
APP="${2:-financial_newspaper}"
shift $(( $# > 1 ? 2 : 1 ))  # remaining args go to the app verbatim
ROUTER_URL="${ROUTER_URL:-http://127.0.0.1:4000}"
HUGIN_DIR="${HUGIN_DIR:-../gimle-hugin}"

# Refuse to fire N paid editions at a dead router.
if ! curl -sf "${ROUTER_URL}/healthz" >/dev/null; then
  echo "no router at ${ROUTER_URL} — start it with \`ctrlrtn serve\`" >&2
  exit 2
fi

for i in $(seq 1 "$N"); do
  echo "=== edition ${i}/${N} ($(date +%H:%M:%S)) ==="
  (
    cd "$HUGIN_DIR"
    export HUGIN_CTRLRTN=1 ANTHROPIC_BASE_URL="$ROUTER_URL"
    if [ "$#" -gt 0 ]; then
      uv run hugin app "$APP" -- "$@"
    else
      uv run hugin app "$APP"
    fi
  ) || echo "edition ${i} failed — continuing (failures are data too)" >&2
done
echo "done: ${N} editions. Next: ctrlrtn usecases"
