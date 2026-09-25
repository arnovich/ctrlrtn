# Evaluation: how a verdict is reached

What a verdict claims, how it is computed, and where it stops. Options and
defaults are in `uv run ctrlrtn <command> --help`.

## The question

Is the cheaper candidate non-inferior to the incumbent on this use-case,
within a stated margin, on the application's own recorded inputs? The
verdict is per use-case and can differ by role; it never says which model
is best.

Inputs are the use-case's successful recorded requests, newest first,
`--limit` 50 by default. The incumbent is inferred from what the use-case
has been served, or set with `--baseline`. The use-case must be replayable:
a text transform is, an agent whose tool calls act on the world is not.

## Paired offline replay

`replay-eval` is the only path with a margin-based verdict, and a dry run
until `--yes`. Replay and judge calls go to the Anthropic API directly, not
through the proxy: the sampled prompts and both outputs leave the box.

```bash
uv run ctrlrtn replay-eval <use-case> <candidate-model> --margin 1.0 --yes
```

Each input goes to both models with only the `model` field swapped,
streaming disabled and any recorded `thinking` block dropped. Outputs are
reduced to judgeable text: text blocks verbatim, each tool call as
`[tool_use: name]` plus its sorted-key JSON arguments.

**Pairing.** The statistic is the per-pairing difference
`candidate - baseline` on the judge's scale. Both models saw the same input,
so its variance cancels.

**The judge.** `claude-opus-4-8` by default (`--judge-model`) sees the task
and two responses labelled A and B and returns a 0.0 to 10.0 score for
each, never an arm label or a model name. The task is the recorded
user-turn text plus its tool-result payloads. The default judge is from the
same vendor as both arms: calibrate it on your use-case before trusting a
verdict that rests on it.

Judges favour a position, so each pairing is judged `--replicates` times
(default 2, must be even) with the arm shown as A alternating, then
de-blinded and averaged. Blinding cannot stop a judge recognising an arm by
its style; calibration measures that residual.

**Units.** A task (`x-ctrlrtn-task`) is one cluster, since its calls to the
use-case are correlated. The test resamples clusters, never pairings. An
untagged input is its own cluster.

**The test.** Non-inferior means the mean difference is not below
`-margin`, in judge points on the 0 to 10 scale, default 1.0 (`--margin`):
the largest average quality drop you accept for the saving. The verdict
turns on a one-sided 95% lower confidence bound on the mean difference
clearing `-margin`, never on the mean alone.

The bound is a BCa bootstrap: 5000 resamples of whole clusters,
bias-corrected from the share of bootstrap means below the observed mean,
accelerated by a jackknife over clusters. A percentile bootstrap
undercovers at these sizes.

Two guards refuse to conclude, both reported as underpowered:

- fewer than 20 usable clusters: no bound is computed;
- more than 20% of samples failed or blank on both arms: the batch is too
  degraded, and the distinct failure reasons are printed.

| Verdict | `--json` | Exit |
| --- | --- | ---: |
| `NON-INFERIOR` | `NON_INFERIOR` | 0 |
| `NOT non-inferior` | `NOT_NON_INFERIOR` | 1 |
| `UNDERPOWERED (cannot conclude)` | `UNDERPOWERED` | 3 |

## Live A/B: a tripwire, not a certificate

`experiment start` splits a use-case's live traffic by task and serves the
candidate to one arm; `experiment status` reads the result.

```bash
uv run ctrlrtn experiment start <use-case> <candidate-model> --split 50
uv run ctrlrtn experiment status <experiment-id>
```

A task's arm is a stable hash of experiment id and task id against `--split`
(default 50, range 1 to 99), so all its calls land on one arm. The gateway
checks a running experiment before a persistent route, then passes the
request through. Untagged calls never enter an experiment.

There is no paired verdict, since each task ran on one arm. The experiment
gives real cost per task per arm, and the application's own outcome per
task, posted to `/ctrlrtn/outcome` as a success flag and optional score.

Each closed task is one unit. It is a failure if the application reported
one, if its score is below `--fail-below`, or if the candidate hit the
per-task call ceiling (`--max-calls`, default 60, enforced). A task closes
after `--idle-minutes` without calls (default 45). A closed task with no
outcome is unreported and kept. A task spanning two arms or two
experiments is excluded.

Each arm's failure rate gets a 95% Wilson score interval, widened by Manski
bounds that impute every unreported task as all-success at one end and
all-failure at the other. `--gross-margin` is an absolute failure-rate gap,
default 0.20. `--min-tasks` (default and floor 30) reported tasks per arm
are required first.

| Verdict | Meaning | Exit |
| --- | --- | ---: |
| `NO_GROSS_REGRESSION` | Candidate's worst case is within the margin of the baseline's best case | 0 |
| `GROSS_REGRESSION` | Candidate's best case exceeds the baseline's worst case by more than the margin | 1 |
| `INCONCLUSIVE` | The bounds overlap the margin | 3 |
| `UNDERPOWERED` | Fewer than `--min-tasks` reported tasks on an arm | 3 |
| `NOT_EXERCISED` | The candidate arm served the baseline's model or took no tasks | 4 |
| `NO_DATA` | No traffic for the experiment | 4 |

A 20 point gap on a binary outcome, with 30 reported tasks per arm, catches
gross breakage; it cannot certify a one-point margin on a 10 point quality
scale. The interval is fixed-sample, so repeated `status` runs are not
corrected for peeking, and tasks are treated as independent. Read
`NO_GROSS_REGRESSION` as "nothing obviously broke at realistic volume, and
here is what it cost".

## Judge calibration

A blinded, position-balanced judge can still prefer a style it recognises.
`calibration-set` and `calibrate` check it against a human.

```bash
uv run ctrlrtn calibration-set <use-case> <candidate-model> --out labels.jsonl --yes
uv run ctrlrtn calibrate labels.jsonl --margin 1.0
```

`calibration-set` replays `--n` inputs (default 40) on both arms and writes
one JSON line per pairing: the task and two outputs labelled A and B, A
randomised per pair (`--seed`, default 0), the mapping in a sidecar `.key`
file. You add `score_a` and `score_b` on the same 0 to 10 scale to every
line, without opening the key.

`calibrate` de-blinds the file, scores the same stored outputs with the same
judge code `replay-eval` uses, and compares:

- **Agreement**: how often the judge picks the human's winner on directional
  pairs (where the human did not tie), with a one-sided Wilson lower bound
  that must clear 0.5.
- **Slope**: the regression of the judge's difference on the human's. Below
  0.5 the judge compresses real quality gaps.
- **Bias at parity**: that regression's intercept, the judge's difference
  when humans tie. A positive value favours the candidate and must not
  exceed half the margin (0.5 points without `--margin`).
- **Spearman correlation**: printed, not gated.

The verdict is `ALIGNED`, `MISALIGNED`, or `INSUFFICIENT` when fewer than 20
directional pairs are available.

This is advisory and point-in-time: `replay-eval` does not read it, nothing
corrects the judge's scores, and it holds for one judge model on one
use-case's prompts. Change either and re-check. A misaligned judge is
cheapest to fix with `calibrate --judge-model <other>` on the same file,
which re-judges the stored outputs without new labels.

## What the verdict does not cover

**The saving is a projection until the switch.** `campaign-report` and
`route list` reprice the recorded token mix at the candidate's rates. A
cheaper model is often more verbose, so that number is an upper bound.
Per-arm cost per task from a live experiment, and the realised saving after
`route adopt`, are the ground truth.

**Held-out inputs.** Traffic also used to tune prompts or fit a local model
contaminates an evaluation on it. `dataset create` freezes a lineage
manifest: trace ids and SHA-256 digests of each request and response, no
payloads, with whole task clusters assigned to a train or an evaluation
split by a salted hash (`--train-percent`, default 80). `dataset verify`
rechecks the digest, partition and live payload bindings. `replay-eval
--dataset-manifest` refuses `--limit`, verifies the manifest and payloads
before planning, evaluates only the evaluation split, and records the
digest in the verdict.

```bash
uv run ctrlrtn dataset create <use-case> dataset.json
```

**Step evidence stays step evidence.** `--workflow`, `--workflow-version`
and `--step` narrow a replay or an experiment to one exact step of one
workflow version. The JSON verdict then carries `scope` and
`claim: workflow_step`; `campaign-report` ignores scoped verdicts, budget
fallbacks refuse scoped evidence, and `experiment status` labels the result
as step evidence only.

## Reading a verdict

```
replay eval: claude-sonnet-4-5 -> claude-haiku-4-5
  pairings=40  units=22  failed=0  blank=0
  mean score diff (cand - base) = +0.070  (0-10 judge scale; margin 1.0, 95% lower bound -0.090)
  verdict: NON-INFERIOR
  -> non-inferior on this replay batch, but this trusts the judge blindly; before switching, check it agrees with humans: `ctrlrtn calibration-set` then `ctrlrtn calibrate`.
```

Read the second line first: `pairings` is how many inputs produced a usable
pair, `units` how many independent tasks they span (at least 20 for any
conclusion), `failed` and `blank` how much of the batch was lost. The third
line gives the mean difference and the lower bound the verdict turns on:
here -0.09, inside the 1.0 margin.

- `NON-INFERIOR`: calibrate the judge on this use-case if you have not,
  confirm live with `experiment start`, and when the tripwire is clean
  switch with `route adopt`. The claim covers this batch and this use-case.
- `NOT non-inferior`: keep the incumbent. Raise `--margin` only if a larger
  quality drop is acceptable for this role, never to make a verdict pass.
- `UNDERPOWERED (cannot conclude)`: look at `units` and the failure reasons.
  Fewer than 20 units means record more tasks. A high `failed` count means
  fix the cause first, usually a key, an `anthropic-beta` header that replay
  does not re-emit, or a `max_tokens` cap the candidate rejects. A mean
  difference from an underpowered batch is not a result.
