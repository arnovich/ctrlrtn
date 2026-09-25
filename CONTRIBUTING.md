# Contributing

This is a one-maintainer project. Bug reports and questions are welcome as
issues; small fixes are welcome as pull requests. For anything larger, open an
issue first so the outcome is agreed before the work, since responses can
take a few days.

## Setup

```bash
uv sync --locked --extra test --extra tui --extra provider-test   # the set CI installs
uv run pre-commit install        # black, isort, ruff, mypy on every commit
uv run pytest -q --fail-on-skip  # under a minute, offline, no API keys
```

Python 3.12 and 3.13 are both tested. Keep the hooks green, add a test for
any behaviour change, and keep branch coverage at or above 85 %. Live-API
code takes injected functions (see `src/ctrlrtn/eval/live.py`) so the suite
stays offline; do not add a test that needs a key. Write the pull request
with three sections: Summary (why), Changes (what), Testing (how it was
verified).

## Versioning and releases

Versions are `0.x` until the wire surface (headers, control-plane paths,
configuration keys, SDK names) has been stable for a while; a change to any
of those bumps the minor version and is listed under Breaking in
`CHANGELOG.md`. To release: bump `version` in `pyproject.toml` and move the
`Unreleased` entries under the new version in one pull request, then tag
`main` (`git tag -a vX.Y.Z -m "ctrlrtn X.Y.Z"`, push the tag, and
`gh release create vX.Y.Z --notes-from-tag`).

[docs/architecture.md](docs/architecture.md) has the package map, the
request flow and the dependency rules the test suite enforces.
