# Evaluation: how a verdict is reached

What a `ctrlrtn` verdict claims, how it is computed, and where it stops.
Options and defaults are in `uv run ctrlrtn <command> --help`.

## The question

The evaluator answers one question. Is the cheaper candidate non-inferior
to the incumbent on this use-case, within a stated margin, on this
application's own recorded inputs? It does not answer "which model is
best": the verdict is per use-case, and it can differ by role.

The inputs are the requests the gateway recorded for that use-case:
successful calls only, newest first, `--limit` 50 by default. The incumbent
is inferred from what the use-case has been served, or set with
`--baseline`. Replay re-sends recorded requests, so the use-case must be
replayable: a text transform qualifies, an agent whose tool calls act on
the world does not.

## Paired offline replay

`replay-eval` is the primary path and the only one that produces a
margin-based verdict. It is a dry run until you pass `--yes`. Replay and
judge calls go to the Anthropic API directly, not through the proxy, so the
sampled prompts and both outputs leave the box.

```bash
uv run ctrlrtn replay-eval <use-case> <candidate-model> --margin 1.0 --yes
```

Each sampled input is sent unchanged to both models (only the `model` field
swapped, streaming disabled, any recorded `thinking` block dropped). Each
output is reduced to judgeable text: text blocks verbatim, and every tool
call as `[tool_use: name]` plus its sorted-key JSON arguments, since in
agent traffic the arguments usually are the output.

**Pairing.** The two outputs for one input form a pairing, and the statistic
is the per-pairing difference `candidate - baseline` on the judge's scale.
Both models saw the same input, so variance from the input cancels in the
difference. An unpaired comparison would need volume to overcome it.

**The judge.** Each pairing is scored by a separate judge model,
`claude-opus-4-8` by default (`--judge-model`). It sees the task and two
responses labelled A and B and returns a 0.0 to 10.0 score for each, never
an arm label or a model name. The task shown is the user-turn text of the
recorded request plus its tool-result payloads, so the judge can check
claims against the source data.

Judges favour a position, so each pairing is judged `--replicates` times
(default 2, must be even) with the arm shown as A alternating, so the bias
cancels; scores are de-blinded and averaged. Blinding cannot stop a judge
recognising an arm by its style; that residual is what calibration measures.

**Units.** The independent unit is a task (`x-ctrlrtn-task`). Several
calls to one use-case within a task are correlated and form one cluster;
the test resamples clusters, never pairings. An untagged input is its own.

**The test.** The candidate is non-inferior if its mean difference is not
below `-margin`. The margin is in judge points on the 0 to 10 scale,
default 1.0 (`--margin`): the largest average quality drop you accept in
exchange for the saving. The test computes a one-sided 95% lower confidence
bound on the mean difference and concludes only when that bound clears
`-margin`, never from the mean alone.

The bound is a BCa bootstrap: 5000 resamples of whole clusters,
bias-corrected from the share of bootstrap means below the observed mean
and accelerated by a jackknife over clusters. A plain percentile bootstrap
undercovers at these sizes, always toward waving a downgrade through.

Two guards refuse to conclude. If fewer than 20 clusters are usable, the
bound is not computed, so a lucky small batch cannot cross the boundary. If
more than 20% of samples failed or came back blank on both arms, the batch
is too degraded, and the distinct failure reasons are printed. Both cases
are reported as underpowered.

Three verdicts are possible: `NON-INFERIOR`, `NOT non-inferior`, and
`UNDERPOWERED (cannot conclude)`. In `--json` output they are
`NON_INFERIOR`, `NOT_NON_INFERIOR` and `UNDERPOWERED`; exit code 0, 1 or 3.

## Live A/B: a tripwire, not a certificate

`experiment start` splits a use-case's live traffic by task and serves the
candidate to one arm. `experiment status` reads the result.

```bash
uv run ctrlrtn experiment start <use-case> <candidate-model> --split 50
uv run ctrlrtn experiment status <experiment-id>
```

A task's arm, the baseline model or the candidate, is a stable hash of
experiment id and task id against `--split` (default 50, range 1 to 99),
so every call of one task lands on one arm.
The gateway checks a running experiment first, then a persistent route,
then passes the request through. Untagged calls never enter an experiment.

Live traffic cannot produce a paired verdict: each task ran on one arm, so
there is no second output for the same input to judge. It does give real
cost per task per arm from actual usage, and the application's own outcome
per task, posted to `/ctrlrtn/outcome` as a success flag and optional score.

Each closed task is one unit. It is a failure if the application reported
one, if its score is below `--fail-below`, or if the candidate hit the
per-task call ceiling (`--max-calls`, default 60, enforced). A task closes
after `--idle-minutes` without calls (default 45). A closed task with no
outcome is unreported and kept, so a candidate that fails quietly cannot
vanish. A task spanning two arms or two experiments is excluded.

Each arm's failure rate gets a 95% Wilson score interval for sampling
error, widened by Manski bounds that impute every unreported task as
all-success at one end and all-failure at the other. Against
`--gross-margin`, an absolute failure-rate gap with default 0.20, the
verdict is `GROSS_REGRESSION` when even the candidate's best case exceeds
the baseline's worst case by more than the margin, `NO_GROSS_REGRESSION`
when even its worst case is within the margin of the baseline's best case,
and `INCONCLUSIVE` in between. `--min-tasks` (default and floor 30)
reported tasks per arm are required first, otherwise `UNDERPOWERED`.
`NOT_EXERCISED` means the candidate arm served the baseline's model or
took no tasks; `NO_DATA` means no experiment traffic at all.

| Verdict | Meaning | Exit |
| --- | --- | ---: |
| `NO_GROSS_REGRESSION` | Candidate's worst case is within the margin of the baseline's best case | 0 |
| `GROSS_REGRESSION` | Candidate's best case exceeds the baseline's worst case by more than the margin | 1 |
| `INCONCLUSIVE` | The bounds overlap the margin | 3 |
| `UNDERPOWERED` | Fewer than `--min-tasks` reported tasks on an arm | 3 |
| `NOT_EXERCISED` | The candidate arm never served the candidate | 4 |
| `NO_DATA` | No traffic for the experiment | 4 |

A 20 point gap on a binary outcome, with 30 reported tasks per arm, is a
check for gross breakage. It cannot certify a one-point margin on a 10
point quality scale. The interval is fixed-sample, so repeated `status`
runs are not corrected for peeking, and tasks are treated as independent.
Read `NO_GROSS_REGRESSION` as "nothing obviously broke at realistic
volume, and here is what it cost".

## Judge calibration

The judge is a model: blinded and position-balanced, but still able to
prefer a style it recognises. `calibration-set` and `calibrate` check it
against a human.

```bash
uv run ctrlrtn calibration-set <use-case> <candidate-model> --out labels.jsonl --yes
uv run ctrlrtn calibrate labels.jsonl --margin 1.0
```

`calibration-set` replays `--n` inputs (default 40) on both arms and writes
one JSON line per pairing: the task and two outputs labelled A and B, with
A randomised per pair (`--seed`, default 0) and the mapping in a sidecar
`.key` file. You add `score_a` and `score_b` on the same 0 to 10 scale to
every line, without opening the key.

`calibrate` de-blinds the file, scores the same stored outputs with the
same judge code `replay-eval` uses, and compares. Agreement is how often
the judge picks the human's winner on directional pairs (where the human
did not tie), with a one-sided Wilson lower bound that must clear 0.5 to
beat a coin flip. Slope is the regression of the judge's difference on the
human's; below 0.5 the judge compresses real quality gaps, which a
mean-bias check would miss. Bias at parity is that regression's intercept,
the judge's difference when humans tie; a positive value favours the
candidate and must not exceed half the margin (0.5 points when no
`--margin` is given). Spearman correlation is printed but not gated.

The verdict is `ALIGNED`, `MISALIGNED`, or `INSUFFICIENT` when fewer than
20 directional pairs are available, since near-ties carry no signal.

This is advisory and point-in-time. `replay-eval` does not read it, nothing
corrects the judge's scores, and the decision stays with you. It holds for
one judge model on one use-case's prompts; change either and re-check. A
misaligned judge is cheapest to fix with `calibrate --judge-model <other>`
on the same file, which re-judges the stored outputs without new labels.

## What the verdict does not cover

**The saving is a projection until the switch.** `campaign-report` and
`route list` reprice the recorded token mix at the candidate's rates. That
assumes the candidate would use the same tokens; a cheaper model is often
more verbose, so the repriced number is an upper bound. Per-arm cost per
task from a live experiment, and the realised saving after `route adopt`,
are the ground truth.

**Held-out inputs.** If the same recorded traffic is used elsewhere, for
example to tune prompts or fit a local model, an evaluation on it is
contaminated. `dataset create` freezes a lineage manifest instead: trace
ids and SHA-256 digests of each request and response, no payloads, with
whole task clusters assigned to a train or an evaluation split by a salted
hash (`--train-percent`, default 80). `dataset verify` rechecks the digest,
partition and live payload bindings. `replay-eval --dataset-manifest`
refuses `--limit`, verifies the manifest and payloads before planning,
evaluates only the evaluation split, and records the digest in the verdict.

```bash
uv run ctrlrtn dataset create <use-case> dataset.json
```

**Step evidence stays step evidence.** `--workflow`, `--workflow-version`
and `--step` narrow a replay or an experiment to one exact step of one
workflow version. The JSON verdict then carries `scope` and
`claim: workflow_step`; `campaign-report` ignores scoped verdicts, budget
fallbacks refuse scoped evidence, and `experiment status` labels the
result as step evidence only.

## Reading a verdict

A `replay-eval` run prints something like this:

```
replay eval: claude-sonnet-4-5 -> claude-haiku-4-5
  pairings=40  units=22  failed=0  blank=0
  mean score diff (cand - base) = +0.070  (0-10 judge scale; margin 1.0, 95% lower bound -0.090)
  verdict: NON-INFERIOR
  -> non-inferior on this replay batch, but this trusts the judge blindly; before switching, check it agrees with humans: `ctrlrtn calibration-set` then `ctrlrtn calibrate`.
```

Read the second line first: `pairings` is how many inputs produced a
usable pair, `units` how many independent tasks they span (at least 20 for
any conclusion), `failed` and `blank` how much of the batch was lost. The
third line gives the mean difference and the lower bound the verdict turns
on: here -0.09, inside the 1.0 margin.

`NON-INFERIOR`: calibrate the judge on this use-case if you have not, then
confirm live with `experiment start` and, when the tripwire is clean,
switch with `route adopt`. The claim covers this batch and this use-case.

`NOT non-inferior`: keep the incumbent. Raise `--margin` only if a larger
quality drop is acceptable for this role, never to make a verdict pass.

`UNDERPOWERED (cannot conclude)`: look at `units` and at the failure
reasons. Fewer than 20 units means record more tasks. A high `failed` count
means fix the cause first, usually a key, an `anthropic-beta` header that
replay does not re-emit, or a `max_tokens` cap the candidate rejects. A
mean difference from an underpowered batch is not a result.
