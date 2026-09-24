"""Experiment analysis, calibration, replay, and campaign CLI commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable
from typing import Any, NoReturn

from ctrlrtn.analysis.campaign import (
    build_campaign_report,
    render_campaign_markdown,
    render_campaign_svg,
)
from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
)
from ctrlrtn.eval.tripwire import (
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
StoreFactory = Callable[..., SqliteTraceStore]
LiveFunctionFactory = Callable[..., Callable[..., Any]]

_TRIPWIRE_EXIT = {
    NO_GROSS_REGRESSION: 0,
    GROSS_REGRESSION: 1,
    INCONCLUSIVE: 3,
    UNDERPOWERED: 3,
    NOT_EXERCISED: 4,
    NO_DATA: 4,
}
_CALIBRATION_EXIT = {ALIGNED: 0, MISALIGNED: 1, INSUFFICIENT: 3}


class CampaignReportCommands:
    """Assemble bounded campaign reports from replay artifacts."""

    def _campaign_report(self, args: argparse.Namespace) -> None:
        replay_reports = []
        seen_use_cases: dict[str, str] = {}
        for path in args.replay_json:
            try:
                with open(path, encoding="utf-8") as handle:
                    document = json.load(handle)
                if not isinstance(document, dict):
                    raise TypeError("not a JSON object")
                use_case = document["use_case"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._fail(f"cannot read replay json {path}: {exc}")
            if use_case in seen_use_cases:
                self._fail(
                    f"duplicate replay json for {use_case}: {path} and "
                    f"{seen_use_cases[use_case]}"
                )
            seen_use_cases[use_case] = path
            replay_reports.append(document)
        database_path = self._database_path()
        try:
            store = self._store_factory(database_path, read_only=True)
        except sqlite3.OperationalError:
            self._fail(
                f"no readable database at {database_path} — record traffic first."
            )
        try:
            rows = build_campaign_report(store, replay_reports)
            if args.only:
                wanted = set(args.only)
                missing = wanted - {row.use_case for row in rows}
                if missing:
                    self._fail(
                        f"--only names unknown use-cases: {sorted(missing)}"
                    )
                rows = [row for row in rows if row.use_case in wanted]
        except sqlite3.OperationalError as exc:
            self._fail(f"database at {database_path} is not a router db: {exc}")
        finally:
            store.close()
        markdown = render_campaign_markdown(rows)
        if args.md:
            with open(args.md, "w", encoding="utf-8") as handle:
                handle.write(markdown + "\n")
            print(f"Wrote {args.md}.")
        else:
            print(markdown)
        if args.svg:
            with open(args.svg, "w", encoding="utf-8") as handle:
                handle.write(render_campaign_svg(rows) + "\n")
            print(f"Wrote {args.svg}.")
