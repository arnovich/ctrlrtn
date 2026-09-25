# Run an experiment: from a recorded use-case to a switched route

The evidence chain from an offline replay to a route in production, and back
out again. [Evaluation](evaluation.md) explains the statistics behind each
verdict.

## Before you start

- The use-case has recorded traffic: it appears in `ctrlrtn usecases`.
- Calls carry `x-ctrlrtn-task`, so a job is one unit: `ctrlrtn propagation`
  reports `PROPAGATING`.
- The application reports outcomes to `/ctrlrtn/outcome`; a live A/B is
  judged by nothing else.
- The use-case has at least 20 recorded tasks; below that a replay can only
  conclude underpowered.
- `ANTHROPIC_API_KEY` is set where replays run. Replay and the judge
  (`claude-opus-4-8` by default, `--judge-model`) call the Anthropic API
  directly, not through the proxy, whatever provider the application uses;
  recorded prompts and outputs leave the box on those calls.

## 1. Replay offline

`replay-eval` sends recent recorded inputs to both models, has a blinded
judge score each pair with positions swapped, and runs a paired
non-inferiority test. It bills your key: the plan prints first and nothing
is spent without `--yes`:

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --margin 1.0
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --margin 1.0 --yes \
  --json editor-haiku.json
```

- The margin is in judge points on a 0 to 10 scale.
- Verdicts: `NON-INFERIOR`, `NOT non-inferior`, `UNDERPOWERED (cannot
  conclude)`; in the JSON artifact `NON_INFERIOR`, `NOT_NON_INFERIOR` and
  `UNDERPOWERED`.
- Underpowered means the lower confidence bound did not clear the margin at
  this sample size, not that the candidate is worse; `--limit` raises the
  number of inputs.
- `--replicates` sets judge calls per pair, `--baseline` overrides the
  inferred incumbent, and `--max-tokens` clamps a smaller candidate's output
  cap.

Long replays can run in the background:

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --yes --background
uv run ctrlrtn worker            # in another shell; runs queued jobs
uv run ctrlrtn jobs list
uv run ctrlrtn jobs export <job-id> editor-haiku.json
```

The JSON artifact is the input to `campaign-report` and to budget fallbacks.

## 2. Check the judge

`calibration-set` replays a use-case on both arms and writes blinded pairs;
you score them by hand; `calibrate` reports whether the judge agrees with
you:

```bash
uv run ctrlrtn calibration-set tag:editor claude-haiku-4-5 \
  --out labels.jsonl --n 40 --yes
# add score_a and score_b (0 to 10) to each line of labels.jsonl
uv run ctrlrtn calibrate labels.jsonl --margin 1.0
```

The report gives agreement with a confidence bound, whether the judge
compresses the gaps you see, and whether it favours the candidate at parity.
It is advisory and point-in-time; replay verdicts do not read it. The labels
file holds prompts and both outputs in plain text: treat it like the
database.

## 3. Shadow live inputs

A shadow mirrors a sample of live requests to the candidate in a bounded
background pool. The user gets the incumbent's response; the candidate's
output is recorded, never served. That gives the candidate's cost, latency
and failure rate on live inputs with no exposure:

```bash
uv run ctrlrtn shadow start tag:editor claude-haiku-4-5 --sample 10
uv run ctrlrtn shadow list      # done, failed and dropped per shadow
uv run ctrlrtn shadow stop <shadow-id>
```

A mirror and its actual call share a pair id, so the console shows them side
by side. Mirrors dropped by a full queue or failed at the provider count as
attrition. Shadowing sends live data to the candidate provider and bills its
credentials.

## 4. Split live traffic

A live experiment serves the candidate to a share of the use-case's tasks.
Assignment is per task and sticky: every call of one job sees the same arm:

```bash
uv run ctrlrtn experiment start tag:editor claude-haiku-4-5 --split 50
uv run ctrlrtn experiment list
uv run ctrlrtn experiment status <experiment-id>
```

`status` is a tripwire, not a certificate: it catches a gross regression in
the per-arm failure rate the application reported and cannot certify a fine
quality margin. Its verdicts and exit codes are in
[Evaluation](evaluation.md).

- It needs at least 30 tasks with a reported outcome per arm.
- `--fail-below` treats a reported score under a threshold as a failure.
- `--idle-minutes` decides when a task with no new calls is closed.
- A candidate that makes more than `--max-calls` calls in one task is cut
  off with a counted failure, a backstop against loops.

Experiments are immutable; to change the split, stop and start a new one:

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

`route list` prices the rerouted calls at the old model and subtracts what
they cost: the saving shown is realized, not projected. `route set` installs
a route without an experiment, for example on a replay verdict alone:

```bash
uv run ctrlrtn route set tag:editor claude-haiku-4-5 \
  --note "replay-eval NON_INFERIOR"
uv run ctrlrtn route clear tag:editor    # rollback
```

- The note is stored with the route and shown by `route list` and the
  console.
- `calls` shows the model the application asked for; `show <call-id>` shows
  `served_model`, the one a route or experiment actually served.
- Precedence per call: running experiment, then route, then pass-through.
- A `max_tokens` above the routed model's cap is clamped.
- Routes have no per-task call ceiling, so watch outcomes after switching.

## 6. Candidates on another provider

A candidate may live on a named provider, such as a local server speaking
the OpenAI API. Name it and the candidate arm or route switches both model
and upstream:

```bash
uv run ctrlrtn experiment start tag:editor qwen2.5:7b --provider ollama --split 50
uv run ctrlrtn route set tag:editor qwen2.5:7b --provider ollama
```

Both must declare the same `api`; the proxy never translates between
provider APIs and rejects a mismatch before sending. When the provider
changes, the client's credential headers are dropped and the named
provider's own credential from its environment is used if it has one.
[Configuration](configure.md) describes the `providers` block.

## 7. Scope evidence to one step

When calls carry workflow identity, every command above accepts
`--workflow NAME --workflow-version REV --step STEP` and works on that exact
step only. Step evidence stays step evidence, never a claim about the whole
workflow. See [Instrument a workflow](instrument-a-workflow.md).

## 8. Put the control plane in Git

Routes and running experiments can be declared in a committed YAML file and
activated atomically, so switches have a history and a reviewer. Keep the
file in your operational configuration repository and name it with
`--repo`:

```bash
cp examples/routing.yaml /srv/ops/routing.yaml
cd /srv/ops && $EDITOR routing.yaml
git add routing.yaml && git commit -m "Route the editor to Haiku"
uv run ctrlrtn routing-config validate routing.yaml
uv run ctrlrtn routing-config diff routing.yaml --repo /srv/ops
uv run ctrlrtn routing-config activate routing.yaml --repo /srv/ops
uv run ctrlrtn routing-config status
```

- Activation requires a tracked, clean file. It records the Git revision and
  the file's SHA-256 with the state it installed, and keeps the last
  known-good state if anything fails.
- The file is authoritative for routes and running experiments; recorded
  traces, finished experiments and jobs stay in SQLite.
- Rollback is a commit that removes the route, then `activate` again.
- A `route clear` or `experiment stop` from the CLI takes effect at once but
  leaves the active state out of step with the activated revision; `diff`
  shows the drift.
- A route to a provider with a different API fails every call of that
  use-case until it is cleared.
- Never commit credentials or recordings to that repository; `note` values
  are stored and displayed.

## 9. Use a verdict under budget pressure

Daily budgets, global or per use-case, and lifetime session budgets block
calls at a ceiling. A use-case can have an approved fallback: a cheaper
model from a `NON_INFERIOR` replay artifact, served at a lower threshold
before the hard ceiling blocks:

```bash
uv run ctrlrtn fallback approve editor-haiku.json
uv run ctrlrtn fallback list
uv run ctrlrtn budget
```

Absent or stale evidence never guesses; without an approval the ceiling
simply blocks. [Configuration](configure.md) has the `budgets` block.

## 10. Report a campaign

Across several roles, fold the recorded spend, replay verdicts and live
results into one table and chart:

```bash
uv run ctrlrtn campaign-report --replay-json editor.json journalist.json \
  --md campaign-report.md --svg campaign-chart.svg
```

[The campaign guide](campaign.md) scripts the whole loop for a multi-agent
application.
