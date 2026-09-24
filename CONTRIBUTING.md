# Contributing

Solo-maintainer project — issues and small PRs welcome.
Responses may take a few days.

## Setup

```bash
uv sync --extra test --extra tui   # the same locked set CI installs
uv run pre-commit install          # black, isort, ruff, mypy on every commit
uv run pytest -q                   # under a minute, offline, no API keys
```

## Conventions (enforced by CI)

- `black` (line length 80), `isort` (profile=black), `ruff` and `mypy`
  run as pre-commit hooks: `uv run pre-commit install` once, and every
  commit is checked with the same pinned tools CI uses.
- Tests accompany behavior changes; `pytest` runs with `asyncio_mode=auto`.
- Live-API code paths take injected functions (see `eval/live.py`) so the
  suite stays offline.

## Code map

[docs/architecture.md](docs/architecture.md) has the package map, the
request flow and the dependency rules the test suite enforces.
