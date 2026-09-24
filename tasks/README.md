# Tasks

Planned work lives here as one markdown file per task, so a contributor can
see what is queued without leaving the repository.

```
tasks/
├── open/      not started
├── ongoing/   claimed, in progress
└── closed/    done or abandoned
```

The folder is the source of truth for a task's state. Files are named
`NNN-kebab-slug.md`; the number is the task's identity and never changes.

Each file has a small frontmatter block and two required sections:

```markdown
---
title: Batched GPU simulation
state: open
priority: medium
labels: [enhancement, runtime]
---

# Batched GPU simulation

## Context

Why this matters and what is true today.

## Outcome

What done looks like, stated so someone else could check whether it happened.
```

`state` is `open`, `ongoing` or `closed` and mirrors the folder. `priority` is
`high`, `medium` or `low`; default to `medium`. Larger discussions belong in
GitHub Issues; a task file is the agreed plan, not the debate.
