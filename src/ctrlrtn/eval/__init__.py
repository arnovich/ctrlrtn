"""Offline eval engine: decide honestly whether a cheaper model is a safe swap.

The primary path is **paired shadow-replay** (``replay`` — to come): re-run a
recorded use-case's inputs through baseline and candidate, score each pair with a
**blinded judge** (``judge``), and run a **paired non-inferiority test**
(``ni``). See ``docs/history/eval-design.md``.
"""
