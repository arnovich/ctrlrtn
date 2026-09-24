# Contributing

Solo-maintainer project — issues and small PRs welcome.
Responses may take a few days.

## Setup

```bash
uv venv && uv pip install -e ".[test,tui]"
uv run pytest -q          # under a minute, no network, no API keys needed
```

## Conventions (enforced by CI)

- `black` (line length 80), `isort` (profile=black), `ruff` and `mypy`
  run as pre-commit hooks: `uv run pre-commit install` once, and every
  commit is checked with the same pinned tools CI uses.
- Tests accompany behavior changes; `pytest` runs with `asyncio_mode=auto`.
- Live-API code paths take injected functions (see `eval/live.py`) so the
  suite stays offline.

## Code map

- `gateway/` — the hot path: proxy, serving snapshot, injection, shadow mirror
- `recorder/` — trace queue, capability contracts (`repositories.py`), memory
  and SQLite stores; `recorder/sqlite/` splits schema from queries
- `policy/` — the switching primitives: experiment, route, fallback, shadow,
  plus budget admission
- `identify/` — use-case keying (tag header / prompt fingerprint)
- `telemetry/` — usage, pricing, enrichment · `analysis/` — reports and
  recommendations · `eval/` — replay, blinded judge, non-inferiority,
  calibration, tripwire
- `workflow/` — identity, discovery, graphs, tool-call correlation
- `cli/` — `parser.py` / `render.py` / `commands.py`, and `console.py`, the
  read-only TUI
- `docs/architecture.md` — current boundaries and request flow
