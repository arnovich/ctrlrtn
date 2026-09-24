"""Keyboard-friendly modal forms used by the Textual console."""

from __future__ import annotations

from textual.binding import Binding
from textual.widgets import (
    Static,
)

from ctrlrtn.config import load_settings
from ctrlrtn.control.service import (
    provider_error,
)


def _unknown_provider(name: str | None) -> str | None:
    return provider_error(name, _provider_exists)


def _provider_exists(name: str) -> bool:
    return load_settings().resolver().resolve_provider(name, "/") is not None


class ConsoleUnavailable(Exception):
    """The console can't run — e.g. no database at the resolved path. The CLI
    turns this into a clean failure instead of a traceback."""


class KeyboardForm:
    """Shared keyboard manners for the modal forms: escape closes, up/down
    walk the fields. Mixed in ahead of ModalScreen so these bindings and the
    dismissal behaviour apply to every dialog alike — a Cancel button you have
    to tab past is a keystroke the escape key already spends better.

    Screens returning something other than ``None`` for "nothing happened"
    (a bool, say) override ``action_close``."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("up", "previous", "Previous field", show=False),
        Binding("down", "next", "Next field", show=False),
    ]

    def __init_subclass__(cls, **kwargs: object) -> None:
        # Textual merges BINDINGS only from classes that are themselves
        # DOMNode subclasses, and only from each class's OWN __dict__
        # (DOMNode._merge_bindings). A plain mixin is skipped entirely, so
        # copy the shared bindings into the screen before Textual's
        # __init_subclass__ runs the merge — otherwise every dialog silently
        # ignores escape, which is exactly the bug this class exists to fix.
        cls.BINDINGS = [  # type: ignore[attr-defined]
            *KeyboardForm.BINDINGS,
            *cls.__dict__.get("BINDINGS", []),
        ]
        super().__init_subclass__(**kwargs)  # type: ignore[arg-type]

    # A line every dialog ends with, so the keys are on screen where they are
    # needed rather than only in the commands panel.
    HINT = "↑↓ / tab to move · enter to submit · esc to close"

    def action_close(self) -> None:
        self.dismiss(None)  # type: ignore[attr-defined]

    def action_next(self) -> None:
        self.focus_next()  # type: ignore[attr-defined]

    def action_previous(self) -> None:
        self.focus_previous()  # type: ignore[attr-defined]


def form_hint() -> Static:
    return Static(KeyboardForm.HINT, classes="formhint", markup=False)


_MATCH_LIMIT = 4


def field_matches(typed: str, options: tuple[str, ...]) -> str:
    """What to show under a type-ahead field. The inline ghost completion can
    only ever be a PREFIX — Textual renders `suggestion[len(value):)` — but the
    names here are prefixed by convention (`tag:`, `claude-`), so a prefix
    match is nearly useless for finding anything. This line matches anywhere in
    the name instead, so "news" finds "tag:newspaper_editor". Empty while the
    field is empty (the whole list is not a hint) or once what you typed is
    exactly a known name."""
    typed = typed.strip().lower()
    if not typed or typed in {option.lower() for option in options}:
        return ""
    hits = [option for option in options if typed in option.lower()]
    if not hits:
        return "no match — will be used as typed"
    shown = " · ".join(hits[:_MATCH_LIMIT])
    return shown + (
        f" (+{len(hits) - _MATCH_LIMIT} more)"
        if len(hits) > _MATCH_LIMIT
        else ""
    )
