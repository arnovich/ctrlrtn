# The cost-saving campaign

An end-to-end experiment that answers "which agent roles can run on a cheaper
model?" with real multi-agent traffic, a paired offline eval, and a live A/B —
and produces a README-ready table + chart at the end. It doubles as the
router's full-pipeline integration test: every phase exercises recording,
use-case keying, outcome ingestion, experiments and evals.

The worked example uses gimle-hugin's `financial_newspaper` app (roles like
journalist / analyst / editor, each keyed as its own `tag:` use-case via
`x-ctrlrtn-route`), Sonnet as the incumbent and Haiku as the candidate — swap in
your own app and models.

**Why two eval styles?** In a live A/B each edition runs on ONE arm, so there
is no paired output to judge — live data shows real cost and outcome parity,
but quality comparisons on it are unpaired (weak at small n). The paired,
sample-efficient quality verdict comes from `replay-eval`: the same recorded
inputs on both models, scored by a blinded pairwise judge, tested for
non-inferiority. The campaign uses replay for verdicts and the live A/B for
in-vivo confirmation and the cost numbers.

## Phase 1 — record a baseline corpus

```bash
# terminal 1, repo root (the DB lives where serve runs — see docs/configure.md)
uv run ctrlrtn serve --log-requests

# terminal 2 (optional but fun): watch it live
uv run ctrlrtn console

# terminal 3: ~20 editions through the router, everything on the incumbent
HUGIN_DIR=../gimle-hugin scripts/campaign_run.sh 20
```

Sanity-check the recording before spending more: `ctrlrtn usecases`
should list one `tag:` row per role, and `ctrlrtn propagation` should
show tagged tasks.

## Phase 2 — paired offline verdicts (the quality map)

Per role, shadow-replay the recorded inputs on the candidate and run the NI
test; save the machine-readable verdict for the report:

```bash
mkdir -p campaign
for role in journalist analyst editor; do
  uv run ctrlrtn replay-eval "tag:${role}" claude-haiku-4-5 \
    --margin 1.0 --limit 40 --yes --json "campaign/${role}.json" || true
done
```

Then check the judge itself on ONE role before believing the verdicts
(blinded pairs, scored by you, compared to the judge — see README "Offline
evals"):

```bash
uv run ctrlrtn calibration-set tag:editor claude-haiku-4-5 \
  --out campaign/labels.jsonl --yes
# ...score labels.jsonl by hand (0-10 per response), then:
uv run ctrlrtn calibrate campaign/labels.jsonl
```

## Phase 3 — live A/B confirmation

Start 50/50 experiments for the roles replay blessed, then run another batch:

```bash
scripts/campaign_experiments.sh claude-haiku-4-5 journalist editor
HUGIN_DIR=../gimle-hugin scripts/campaign_run.sh 60
uv run ctrlrtn experiment status <exp-id>   # tripwire per experiment
```

Sizing: the tripwire needs **30 reported tasks per arm** before it concludes,
and a 50% split halves each arm — so budget **~60+ editions** for this phase.
With fewer, the live column honestly reads UNDERPOWERED (that is the tripwire
working, not failing).

The tripwire compares app-reported outcomes (Hugin posts each edition's
outcome to `/ctrlrtn/outcome`) and real cost/task per arm. Watch the latency and
cost graphs in the console move as the candidate arm takes traffic.

## Phase 4 — the report

```bash
uv run ctrlrtn campaign-report \
  --replay-json campaign/*.json \
  --md campaign/report.md --svg campaign/chart.svg
```

One row per role: recorded spend, the same token mix repriced at the
candidate's rates, the replay verdict, and the live per-arm cost/task +
tripwire verdict where an experiment ran. The SVG colors a saving green only
when the paired eval passed — unevaluated savings render muted grey, so the
chart never paints an unproven saving as a win. Embed both in the README.

## Phase 5 — adopt the winner, watch the savings become real

For each role the campaign blessed:

```bash
uv run ctrlrtn route adopt <experiment-id>   # stop the A/B, switch 100% of the role
uv run ctrlrtn route list                    # was -> now · since · realized saving
```

From here the saving is no longer a projection: `route list` and the
console's use-case detail price the calls the route actually swapped at the
OLD model and subtract actual spend. Keep an eye on the role's outcomes and
error rate in the console for a while — a route has no per-task runaway
ceiling (the experiment's guard ends at adopt), and it is one `route clear`
away from rollback.

## Honesty notes (put these next to the chart)

- The repriced number is the identical recorded token mix at the candidate's
  price table — real workload, hypothetical price. It assumes the candidate
  would use the same tokens; in practice a cheaper model is often more
  verbose (and output tokens are the expensive component), so treat it as an
  upper bound on the saving. The live per-arm $/task is actual spend — that
  is the ground truth.
- The "recorded cost" column/bar counts baseline-arm traffic only: a running
  experiment's candidate calls are excluded, so phase 3 doesn't dilute the
  incumbent's cost.
- A NON-INFERIOR verdict means "not worse than the margin at 95% one-sided
  confidence on this batch," judged by an LLM whose agreement with a human was
  checked (`calibrate`) — not a blanket quality guarantee.
- UNDERPOWERED roles need more recorded traffic, not a coin flip: rerun
  phase 1 longer or raise `--limit`.
