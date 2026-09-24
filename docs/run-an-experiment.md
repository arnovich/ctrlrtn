# Run an experiment: from a recorded use-case to a switched route

The proxy answers one question per use-case: is a cheaper model non-inferior
to the incumbent on this application's own inputs? This page walks the
evidence chain from an offline replay to a route in production, and back
out again. [Evaluation](evaluation.md) explains the statistics behind each
verdict.

## Before you start

- The use-case has recorded traffic: it appears in `ctrlrtn usecases`.
- Calls carry `x-ctrlrtn-task`, so a job is one unit: `ctrlrtn propagation`
  reports `PROPAGATING`.
- The application reports outcomes to `/ctrlrtn/outcome`; a live A/B is
  judged by nothing else.
- `ANTHROPIC_API_KEY` is set in the shell that runs replays and judges. The
  replay path currently speaks the Anthropic Messages API only.

## 1. Replay offline

`replay-eval` takes recent recorded inputs of the use-case, runs each
through the incumbent and the candidate, has a blinded judge score every
pair with positions swapped, and runs a paired non-inferiority test. It
bills your key, so it prints the plan first and only spends with `--yes`:

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --margin 1.0
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --margin 1.0 --yes \
  --json editor-haiku.json
```

The margin is in judge points on a 0 to 10 scale. The verdict is one of
`NON-INFERIOR`, `NOT non-inferior`, or `UNDERPOWERED (cannot conclude)`. An
underpowered result means the lower confidence bound did not clear the
margin at this sample size, not that the candidate is worse; `--limit`
raises the number of inputs. `--replicates` sets judge calls per pair,
`--baseline` overrides the inferred incumbent, and `--max-tokens` clamps a
smaller candidate's output cap.

Long replays can run in the background:

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --yes --background
uv run ctrlrtn worker            # in another shell; runs queued jobs
uv run ctrlrtn jobs list
uv run ctrlrtn jobs export <job-id> editor-haiku.json
```

The JSON artifact is the input to `campaign-report` and to budget fallbacks.

## 2. Check the judge

A verdict is only as good as its judge. `calibration-set` replays a use-case
on both arms and writes blinded pairs; you score them by hand; `calibrate`
reports whether the judge agrees with you:

```bash
uv run ctrlrtn calibration-set tag:editor claude-haiku-4-5 \
  --out labels.jsonl --n 40 --yes
# add score_a and score_b (0 to 10) to each line of labels.jsonl
uv run ctrlrtn calibrate labels.jsonl --margin 1.0
```

The report gives agreement with a confidence bound, whether the judge
compresses the gaps you see, and whether it favours the candidate at parity.
It is advisory and point-in-time: replay verdicts do not read it.

## 3. Shadow live inputs

A shadow mirrors a sample of live requests for the use-case to the candidate
in a bounded background pool. The user gets the incumbent's response
unchanged; the candidate's output is recorded, never served. It shows the
candidate's cost, latency and failure rate on live inputs with no exposure:

```bash
uv run ctrlrtn shadow start tag:editor claude-haiku-4-5 --sample 10
uv run ctrlrtn shadow list      # submitted, completed, failed, dropped
uv run ctrlrtn shadow stop <shadow-id>
```

Mirrored and actual traces share a pair id, so the console can show them
side by side. Queue saturation and provider failures count as attrition
instead of disappearing. Shadowing sends live data to the candidate provider
and bills its credentials.

## 4. Split live traffic

A live experiment serves the candidate to a share of the use-case's tasks.
Assignment is by task and sticky, so every call of one job sees the same
model:

```bash
uv run ctrlrtn experiment start tag:editor claude-haiku-4-5 --split 50
uv run ctrlrtn experiment list
uv run ctrlrtn experiment status <experiment-id>
```

`status` is a tripwire, not a certificate. It compares the failure rate the
application reported per arm and reports `NO_GROSS_REGRESSION`,
`GROSS_REGRESSION`, `INCONCLUSIVE` or `UNDERPOWERED`, with exit codes 0, 1,
3, and 4 when there is no data, so a script can act on it. It needs at least 30 tasks with a reported
outcome per arm. `--fail-below` treats a reported score under a threshold as
a failure; `--idle-minutes` decides when a task with no new calls is closed.
A candidate that makes more than `--max-calls` calls in one task is cut off
with a counted failure, a backstop against loops.

Experiments are immutable. To change the split, stop the experiment and
start a new one:

```bash
uv run ctrlrtn experiment stop <experiment-id>
```

A shadow and a live split cannot run on the same use-case at once.

## 5. Switch, and switch back

A route serves the candidate to all of the use-case's traffic. Adopting an
experiment stops it and installs the route in one step:

```bash
uv run ctrlrtn route adopt <experiment-id>
uv run ctrlrtn route list
```

`route list` prices the calls the route actually rerouted at the old model
and subtracts what they cost, so the saving shown is realized, not
projected. `route set` installs a route without an experiment, for example
from a replay verdict alone:

```bash
uv run ctrlrtn route set tag:editor claude-haiku-4-5 \
  --note "replay-eval NON_INFERIOR"
uv run ctrlrtn route clear tag:editor    # rollback
```

Precedence per call is: running experiment, then route, then pass-through.
A `max_tokens` above the routed model's cap is clamped. Routes have no
per-task call ceiling, so watch outcomes after switching.

## 6. Candidates on another provider

A candidate may live on a named provider, for example a local server that
speaks the OpenAI API. Name it, and the proxy switches both model and
upstream on the candidate arm or route:

```bash
uv run ctrlrtn experiment start tag:editor qwen2.5:7b --provider ollama --split 50
uv run ctrlrtn route set tag:editor qwen2.5:7b --provider ollama
```

The baseline and the candidate must declare the same `api`. The proxy does
not translate between provider APIs; it rejects a mismatch before sending.
When the provider changes, the client's credential headers are removed, and
the named provider's own credential from its environment is used if it has
one. [Configure](configure.md) describes the `providers` block.

## 7. Scope evidence to one step

When calls carry workflow identity, every command above accepts
`--workflow NAME --workflow-version REV --step STEP` and works on that exact
step only. Step-scoped evidence is reported as step evidence; it is never
promoted to a claim about the whole workflow. See
[instrument a workflow](instrument-a-workflow.md).

## 8. Put the control plane in Git

Routes and running experiments can be declared in a committed YAML file and
activated atomically, so switches have a history and a reviewer:

```bash
cp examples/routing.yaml routing.yaml
$EDITOR routing.yaml
git add routing.yaml && git commit -m "Route the editor to Haiku"
uv run ctrlrtn routing-config validate routing.yaml
uv run ctrlrtn routing-config diff routing.yaml
uv run ctrlrtn routing-config activate routing.yaml
uv run ctrlrtn routing-config status
```

The example routes to a provider named `ollama`; declare it under
`providers` in the configuration, or edit the file. Activation requires a
tracked, clean file. It records the Git revision and
the file's SHA-256 with the state it installed, and keeps the last known-good
state if anything fails. The file is authoritative for routes and running
experiments; recorded traces, finished experiments and jobs stay in SQLite.
Never commit credentials or recordings to that repository.

## 9. Use a verdict under budget pressure

Daily budgets per use-case or globally, and lifetime budgets per session,
block calls at a ceiling. A use-case can be given an approved fallback: a
cheaper model, taken from a `NON_INFERIOR` replay artifact, that the proxy
serves at a lower threshold before the hard ceiling blocks:

```bash
uv run ctrlrtn fallback approve editor-haiku.json
uv run ctrlrtn fallback list
uv run ctrlrtn budget
```

Absent or stale evidence never guesses; without an approval the ceiling
simply blocks. [Configure](configure.md) has the `budgets` block.

## 10. Report a campaign

Across several roles, fold the recorded spend, replay verdicts and live
results into one table and chart:

```bash
uv run ctrlrtn campaign-report --replay-json editor.json journalist.json \
  --md campaign-report.md --svg campaign-chart.svg
```

[The campaign guide](campaign.md) scripts the whole loop for a multi-agent
application.
