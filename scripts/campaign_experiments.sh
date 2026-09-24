#!/usr/bin/env bash
# Phase 3 setup: start a 50/50 live A/B per role that passed replay-eval.
#
#   scripts/campaign_experiments.sh claude-haiku-4-5 journalist editor ...
#
# Each role becomes `experiment start tag:<role> <candidate> --split 50`.
# Stop them later with `ctrlrtn experiment stop <id>`.
set -euo pipefail

CANDIDATE="${1:?usage: campaign_experiments.sh <candidate-model> <role>...}"
shift
[ "$#" -ge 1 ] || { echo "no roles given" >&2; exit 2; }

failed=0
for role in "$@"; do
  # Keep going past one failure (e.g. a role that already has a running
  # experiment) so the rest still start; report at the end.
  if ! uv run ctrlrtn experiment start "tag:${role}" "$CANDIDATE" --split 50; then
    echo "could not start tag:${role} (already running?)" >&2
    failed=1
  fi
done
uv run ctrlrtn experiment list
exit "$failed"
