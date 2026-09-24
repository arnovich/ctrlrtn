"""Argument registration for processes, jobs, and maintenance."""

from __future__ import annotations


def register_processes(sub, runtime, operations) -> None:
    serve = sub.add_parser("serve", help="run the proxy")
    # None lets an unset flag fall through to YAML/env/default settings.
    serve.add_argument(
        "--host", default=None, help="bind address (default 127.0.0.1)"
    )
    serve.add_argument(
        "--port", type=int, default=None, help="bind port (default 4000)"
    )
    serve.add_argument(
        "--log-requests",
        action="store_true",
        default=None,
        help="print one line per call as it is recorded",
    )
    serve.add_argument(
        "--log-level",
        default=None,
        help="verbosity for uvicorn and ctrlrtn: "
        "debug / info / warning / error (default info)",
    )
    serve.set_defaults(func=runtime._serve)

    console = sub.add_parser(
        "console",
        help="live TUI: monitor traffic and run confirmed control actions "
        "(needs the 'tui' extra)",
    )
    console.add_argument(
        "--refresh",
        type=float,
        default=3.0,
        help="seconds between refreshes (default 3)",
    )
    console.add_argument(
        "--routing-config",
        default="routing.yaml",
        help="desired Git-backed routing file used by the g action",
    )
    console.add_argument(
        "--routing-repo",
        default=".",
        help="Git repository containing --routing-config",
    )
    console.set_defaults(func=runtime._console)

    worker = sub.add_parser(
        "worker", help="run durable offline experiment jobs"
    )
    worker.add_argument(
        "--once", action="store_true", help="claim at most one job and exit"
    )
    worker.add_argument(
        "--poll", type=float, default=2.0, help="idle poll interval; default 2s"
    )
    worker.add_argument(
        "--stale-after",
        type=float,
        default=120.0,
        dest="stale_after",
        help="reclaim a job after this many seconds without heartbeat",
    )
    worker.set_defaults(func=operations._worker)

    jobs = sub.add_parser("jobs", help="list or cancel durable background jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_list = jobs_sub.add_parser("list", help="list jobs, newest first")
    jobs_list.add_argument("--limit", type=int, default=50)
    jobs_list.set_defaults(func=operations._jobs_list)
    jobs_cancel = jobs_sub.add_parser("cancel", help="request job cancellation")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.set_defaults(func=operations._jobs_cancel)
    jobs_export = jobs_sub.add_parser(
        "export", help="write a completed job's evidence JSON"
    )
    jobs_export.add_argument("job_id")
    jobs_export.add_argument("path")
    jobs_export.add_argument(
        "--allow-invalidated",
        action="store_true",
        help="export immutable evidence even when source payloads were pruned",
    )
    jobs_export.set_defaults(func=operations._jobs_export)


def register_maintenance(sub, operations) -> None:
    scrub = sub.add_parser(
        "scrub-credentials",
        help="redact credential headers from traces recorded before "
        "capture-time redaction existed (one-time; new traces never store "
        "them)",
    )
    scrub.set_defaults(func=operations._scrub_credentials)

    prune = sub.add_parser(
        "prune",
        help="plan or erase old trace payloads while retaining metrics",
    )
    prune.add_argument(
        "--older-than-days",
        type=int,
        required=True,
        help="select trace payloads strictly older than this many days",
    )
    prune.add_argument(
        "--apply",
        action="store_true",
        help="perform the deletion (default is a read-only dry run)",
    )
    prune.add_argument(
        "--compact",
        action="store_true",
        help="require exclusive maintenance, checkpoint WAL, and VACUUM",
    )
    prune.set_defaults(func=operations._prune)
