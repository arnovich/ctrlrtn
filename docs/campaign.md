# The cost-saving campaign

An end-to-end experiment that answers "which agent roles can run on a cheaper
model?" with real multi-agent traffic, a paired offline eval, and a live A/B,
and produces a README-ready table and chart at the end. It doubles as the
router's full-pipeline integration test: every phase exercises recording,
use-case keying, outcome ingestion, experiments, and evals.

This page is the worked example. The general procedure for one use-case is in
[run-an-experiment.md](run-an-experiment.md); the statistics behind the
verdicts are in [evaluation.md](evaluation.md).

The worked example uses the `financial_newspaper` app from Hugin
(`github.com/arnovich/gimle-hugin`), whose roles (financial_journalist, technical_analyst, editor)
are each keyed as their own `tag:` use-case via `x-ctrlrtn-route`, with Sonnet
as the incumbent and Haiku as the candidate. Swap in your own app and models.

**Why two eval styles?** In a live A/B each edition runs on one arm, so there
is no paired output to judge. Live data shows real cost and outcome parity, but
quality comparisons on it are unpaired and weak at small n. The paired,
sample-efficient quality verdict comes from `replay-eval`: the same recorded
inputs on both models, scored by a blinded pairwise judge, tested for
non-inferiority. The campaign uses replay for verdicts and the live A/B for
in-vivo confirmation and the cost numbers.

## Phase 1: record a baseline corpus

```bash
# terminal 1, repo root (the DB lives where serve runs; see docs/configure.md)
uv run ctrlrtn serve --log-requests

# terminal 2 (optional): watch it live
uv run ctrlrtn console

# terminal 3: ~20 editions through the router, everything on the incumbent
HUGIN_DIR=../gimle-hugin scripts/campaign_run.sh 20
```

`scripts/campaign_run.sh` runs each edition with `HUGIN_GIMLE_ROUTER=1` and
`ANTHROPIC_BASE_URL` pointed at the router, so the app tags every call
(`x-ctrlrtn-task` per edition, `x-ctrlrtn-route` per role) and posts each
edition's outcome to `/ctrlrtn/outcome`. It refuses to start when `/healthz`
does not answer.

Sanity-check the recording before spending more: `ctrlrtn usecases` should
list one `tag:` row per role, and `ctrlrtn propagation` should show tagged
tasks.

## Phase 2: paired offline verdicts (the quality map)

Per role, replay the recorded inputs on the candidate and run the
non-inferiority test; save the machine-readable verdict for the report:

```bash
mkdir -p campaign
for role in financial_journalist technical_analyst editor; do
  uv run ctrlrtn replay-eval "tag:${role}" claude-haiku-4-5 \
    --margin 1.0 --limit 40 --yes --json "campaign/${role}.json" || true
done
```

Then check the judge itself on one role before believing the verdicts. The
router writes blinded pairs, you score them, and `calibrate` compares the
judge to your scores (the method is in [evaluation.md](evaluation.md)):

```bash
uv run ctrlrtn calibration-set tag:editor claude-haiku-4-5 \
  --out campaign/labels.jsonl --yes
# ...score labels.jsonl by hand (0-10 per response), then:
uv run ctrlrtn calibrate campaign/labels.jsonl
```

## Phase 3: live A/B confirmation

Start 50/50 experiments for the roles replay blessed, then run another batch:

```bash
scripts/campaign_experiments.sh claude-haiku-4-5 financial_journalist editor
HUGIN_DIR=../gimle-hugin scripts/campaign_run.sh 60
uv run ctrlrtn experiment status <exp-id>   # tripwire per experiment
```

Sizing: the tripwire needs **30 reported tasks per arm** before it concludes,
and a 50% split halves each arm, so budget **60 or more editions** for this
phase. With fewer, the live column honestly reads UNDERPOWERED. That is the
tripwire working, not failing.

The tripwire compares app-reported outcomes (each edition's outcome is posted
to `/ctrlrtn/outcome`) and real cost per task per arm. Watch the latency and
cost graphs in the console move as the candidate arm takes traffic.

## Phase 4: the report

```bash
uv run ctrlrtn campaign-report \
  --replay-json campaign/*.json \
  --md campaign/report.md --svg campaign/chart.svg
```

One row per role: recorded spend, the same token mix repriced at the
candidate's rates, the replay verdict, and the live per-arm cost per task plus
tripwire verdict where an experiment ran. The SVG colors a saving green only
when the paired eval passed; unevaluated savings render muted grey, so the
chart never paints an unproven saving as a win. A sample is in
[campaign-report.md](campaign-report.md) and
[campaign-chart.svg](campaign-chart.svg). Use `--only` to limit the report to
the listed use-cases, for example to exclude fingerprint-keyed `fp:` keys.

## Phase 5: adopt the winner, watch the savings become real

For each role the campaign blessed:

```bash
uv run ctrlrtn route adopt <experiment-id>   # stop the A/B, switch 100% of the role
uv run ctrlrtn route list                    # was -> now, since, realized saving
```

From here the saving is no longer a projection: `route list` and the
console's use-case detail price the calls the route actually swapped at the
old model and subtract actual spend. Keep an eye on the role's outcomes and
error rate in the console for a while. A route has no per-task runaway ceiling
(the experiment's guard ends at adopt), and it is one `route clear` away from
rollback.

## Honesty notes (put these next to the chart)

- The repriced number is the identical recorded token mix at the candidate's
  price table: real workload, hypothetical price. It assumes the candidate
  would use the same tokens; in practice a cheaper model is often more
  verbose (and output tokens are the expensive component), so treat it as an
  upper bound on the saving. The live per-arm $/task is actual spend; that is
  the ground truth.
- The "recorded cost" column and bar count baseline-arm traffic only: a running
  experiment's candidate calls are excluded, so phase 3 does not dilute the
  incumbent's cost.
- A NON-INFERIOR verdict means "not worse than the margin at 95% one-sided
  confidence on this batch," judged by an LLM whose agreement with a human was
  checked (`calibrate`). It is not a blanket quality guarantee.
- UNDERPOWERED roles need more recorded traffic, not a coin flip: rerun
  phase 1 longer or raise `--limit`.
