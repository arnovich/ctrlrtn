# Contributing

One maintainer; issues and small pull requests are welcome, and responses may
take a few days. For anything larger than a fix, open an issue first so the
outcome is agreed before the work.

## Setup

```bash
uv sync --locked --extra test --extra tui --extra provider-test   # the set CI installs
uv run pre-commit install        # black, isort, ruff, mypy on every commit
uv run pytest -q --fail-on-skip  # under a minute, offline, no API keys
```

Python 3.12 and 3.13 are both tested. A nightly workflow adds randomised
ordering, repeated stress runs and mutation testing of the header code.

## Making a change

1. Fork, then branch from `main` with a prefix and snake_case:
   `fix/`, `feat/`, `docs/`, `refactor/`, for example `fix/route_clamp`.
2. Keep the hooks green (`black`, `isort`, `ruff`, `mypy`) and add a test for
   any behaviour change. Branch coverage must stay at or above 85 %.
3. Live-API code takes injected functions (see `src/ctrlrtn/eval/live.py`), so
   the suite stays offline; do not add a test that needs a key.
4. Open the pull request with the three sections the template provides:
   Summary (why), Changes (what), Testing (how it was verified).

## Versioning

Versions are `0.x` until the wire surface (headers, control-plane paths,
configuration keys, SDK names) has been stable for a while; a change to any
of those bumps the minor version and is listed under a Breaking heading in
`CHANGELOG.md`. Patch releases only fix.

## Releasing

1. Bump `version` in `pyproject.toml` and move the `Unreleased` entries in
   `CHANGELOG.md` under the new version and date, in one pull request.
2. After it merges, tag `main`: `git tag -a vX.Y.Z -m "ctrlrtn X.Y.Z"` and
   `git push origin vX.Y.Z`, then `gh release create vX.Y.Z --notes-from-tag`.

## Code map

[docs/architecture.md](docs/architecture.md) has the package map, the
request flow and the dependency rules the test suite enforces.
