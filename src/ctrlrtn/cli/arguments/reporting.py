"""Argument registration for recorded-traffic reports."""

from __future__ import annotations


def register(sub, commands) -> None:
    usecases = sub.add_parser(
        "usecases", help="print use-cases ranked by spend"
    )
    usecases.set_defaults(func=commands._usecases)

    spend = sub.add_parser(
        "spend", help="show recorded total and today's spend"
    )
    spend.set_defaults(func=commands._spend)

    budget = sub.add_parser(
        "budget", help="show safeguards, remaining spend, and blocked calls"
    )
    budget.set_defaults(func=commands._budget)

    calls = sub.add_parser("calls", help="list recent calls")
    calls.add_argument("--limit", type=int, default=20)
    calls.set_defaults(func=commands._calls)

    tasks = sub.add_parser(
        "tasks", help="cost per task (x-ctrlrtn-task header)"
    )
    tasks.add_argument("--limit", type=int, default=50)
    tasks.set_defaults(func=commands._tasks)

    sessions = sub.add_parser(
        "sessions", help="spend per session (x-ctrlrtn-session header)"
    )
    sessions.add_argument("--limit", type=int, default=50)
    sessions.set_defaults(func=commands._sessions)

    show = sub.add_parser("show", help="show one call in full detail")
    show.add_argument("id", type=int)
    show.set_defaults(func=commands._show)

    reenrich = sub.add_parser(
        "reenrich",
        help="recompute identities/model/tokens for stored rows "
        "(stop the gateway first)",
    )
    reenrich.set_defaults(func=commands._reenrich)
