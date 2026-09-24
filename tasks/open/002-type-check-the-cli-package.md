---
title: Bring the CLI and console under mypy
state: open
priority: medium
labels: [quality, typing, cli]
---

# Bring the CLI and console under mypy

## Context

mypy checks every package except `ctrlrtn.cli`, which `pyproject.toml`
excludes explicitly. The CLI is the largest package (about 11k lines) and
holds the Textual console, whose widget attributes and mixin composition
produce most of the remaining errors: roughly 500 when the exclusion is
lifted, dominated by `attr-defined` on the TUI mixins and untyped
`argparse.Namespace` handlers.

The typed base class the recorder mixins use (`recorder/sqlite/capability.py`,
described in `docs/architecture.md`) applies to the console mixins (`ConsoleActions`, the table mixins, the control
actions), and the argument handlers can take a small typed view of the
namespace instead of `argparse.Namespace`.

## Outcome

- `pyproject.toml` no longer excludes `src/ctrlrtn/cli/` and `uv run mypy`
  reports zero errors.
- No `# type: ignore` or `cast` added without a comment saying why.
- The console structure tests (`tests/cli/test_tui_structure.py`) and the
  CLI manifest test still pass unchanged.
