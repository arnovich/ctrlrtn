"""Render docs/console.png from a synthetic recording.

Seeds a temporary database that looks like the README workload (three roles
over three weeks, a running live A/B on the editor, one task in flight) and
renders the console headless. Nothing in the picture is real traffic.

    uv run --extra tui python scripts/console_screenshot.py /tmp/console.svg
    rsvg-convert -w 1400 /tmp/console.svg -o docs/console.png

The terminal is 150x52: narrower or shorter and the sidebar tables lose rows.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
import time
from pathlib import Path

from ctrlrtn.cli.console import ConsoleApp
from ctrlrtn.policy.experiment import BASELINE, CANDIDATE, Experiment
from ctrlrtn.recorder.store import Outcome, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace

SONNET, HAIKU = "claude-sonnet-4-5", "claude-haiku-4-5"
# role -> (calls per task, mean input tokens, mean output tokens, model)
ROLES = {
    "tag:journalist": (8, 14000, 900, SONNET),
    "tag:analyst": (4, 6000, 700, HAIKU),
    "tag:editor": (2, 9000, 1400, SONNET),
}
PRICE = {SONNET: (3.0, 15.0), HAIKU: (1.0, 5.0)}  # $/M input, $/M output
DAYS = 21
TASKS_PER_DAY = 3
SIZE = (150, 52)


def _call(rng, role, model, task_id, ts, experiment_id=None, arm=None):
    _, tin, tout, _ = ROLES[role]
    it = max(int(rng.gauss(tin, tin * 0.25)), 200)
    ot = max(int(rng.gauss(tout, tout * 0.3)), 50)
    pin, pout = PRICE[model]
    body = {"model": model, "max_tokens": 4096, "messages": []}
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=json.dumps(body).encode(),
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=rng.uniform(1800, 9000),
        model=model,
        input_tokens=it,
        output_tokens=ot,
        cost_usd=(it * pin + ot * pout) / 1e6,
        use_case_key=role,
        task_id=task_id,
        experiment_id=experiment_id,
        arm=arm,
        served_model=model,
        ts=ts,
    )


def seed(db: str) -> None:
    rng = random.Random(7)
    now = time.time()
    store = SqliteTraceStore(db)
    store.create_experiment(
        Experiment("tag:editor", HAIKU, 50, experiment_id="exp:editor-haiku")
    )
    t = now - DAYS * 86400
    n = 0
    while t < now - 600:
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        task_id = f"{day}/{n % TASKS_PER_DAY + 1}"
        arm = rng.choice([BASELINE, CANDIDATE])
        for role, (calls, _, _, model) in ROLES.items():
            for _ in range(calls):
                served, exp, served_arm = model, None, None
                if role == "tag:editor":
                    exp, served_arm = "exp:editor-haiku", arm
                    served = HAIKU if arm == CANDIDATE else SONNET
                ts = t + rng.uniform(0, 1500)
                store._insert(_call(rng, role, served, task_id, ts, exp, served_arm))
        if t < now - 3600:
            store._insert_outcome(
                Outcome(
                    task_id,
                    success=rng.random() > 0.08,
                    score=round(rng.uniform(6.5, 9.5), 1),
                )
            )
        n += 1
        t += 86400 / TASKS_PER_DAY
    # One task in flight, so the live graphs have something to show.
    live = time.strftime("%Y-%m-%d", time.gmtime(now)) + "/live"
    for k, (role, (calls, _, _, model)) in enumerate(ROLES.items()):
        for j in range(calls):
            ts = now - 1500 + (k * 8 + j) * 90 + rng.uniform(0, 60)
            store._insert(_call(rng, role, model, live, ts))
    store.close()


async def render(db: str, out: str) -> None:
    reader = SqliteTraceStore(db, read_only=True)
    app = ConsoleApp(reader, db_path="ctrlrtn.db", refresh_seconds=999)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(6):
            await pilot.pause(0.3)
        app.set_focus(None)
        await pilot.pause(0.3)
        app.save_screenshot(out)
    reader.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "console.db")
        seed(db)
        asyncio.run(render(db, argv[1]))
    print(f"wrote {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
