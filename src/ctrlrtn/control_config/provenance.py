"""Prove that a desired-state document belongs to a clean Git revision."""

from __future__ import annotations

import hashlib
import os
import subprocess

from ctrlrtn.control_config.models import (
    ControlConfigError,
    ControlRevision,
)


def document_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _git(repo: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise ControlConfigError("git executable not found") from None
    except subprocess.CalledProcessError as exc:
        message = (
            exc.stderr.strip() or exc.stdout.strip() or "git command failed"
        )
        raise ControlConfigError(message) from None
    return result.stdout.strip()


def verify_git_revision(repo: str, path: str) -> ControlRevision:
    root = os.path.realpath(_git(repo, "rev-parse", "--show-toplevel"))
    absolute = os.path.realpath(path)
    try:
        relative = os.path.relpath(absolute, root)
    except ValueError:
        relative = ".."
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        raise ControlConfigError(
            f"routing config {path} is outside Git repo {root}"
        )
    _git(root, "ls-files", "--error-unmatch", "--", relative)
    if _git(root, "status", "--porcelain", "--", relative):
        raise ControlConfigError(
            f"routing config {relative} has uncommitted changes; commit it first"
        )
    revision = _git(root, "rev-parse", "HEAD")
    return ControlRevision(
        revision=revision,
        source_path=relative,
        document_sha256=document_sha256(absolute),
    )
