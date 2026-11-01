# Tasks

Planned work is one markdown file per task, so a contributor can see what is
queued without leaving the repository.

The current task files live on the
[`taskvalet/tasks`](https://github.com/arnovich/ctrlrtn/tree/taskvalet/tasks/tasks)
branch, not on `main`. The maintainer's task board commits there: it creates
tasks, records claims, plans and conversations, and moves files between folders.
`main` requires passing checks, so task updates stay off it. The copy under
`tasks/` on `main` is a snapshot from when the board took over and is not kept
up to date.

Do not edit task files by hand or move them between folders in a pull request.
To propose work or comment on a task, open a GitHub issue.

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
title: Enforce the per-task cost ceiling on experiments
state: open
priority: low
labels: [experiments, budget]
---

# Enforce the per-task cost ceiling on experiments

## Context

Why this matters and what is true today.

## Outcome

What done looks like, stated so someone else could check whether it happened.
```

`state` is `open`, `ongoing` or `closed` and mirrors the folder. `priority` is
`high`, `medium` or `low`; default to `medium`. The board may add `## Plan`,
`## Attempts` and a final `## Conversation` section. Larger discussions belong
in GitHub Issues; a task file is the agreed plan, not the debate.
