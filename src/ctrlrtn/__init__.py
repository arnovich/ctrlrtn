"""ctrlrtn — the learning LLM router.

Supported import surface
------------------------
Only these paths are covered by the version policy below. Everything else is
internal: it may move without a deprecation cycle, as the 0.2.0 layout change
did.

* ``ctrlrtn.sdk`` — the client integration
* ``ctrlrtn.cli.commands:main`` — the ``ctrlrtn`` entry point
* ``ctrlrtn.recorder.store`` — the recorder persistence surface
* ``ctrlrtn.recorder.repositories`` — storage contracts for custom
  adapters

Deep imports of anything else (``ctrlrtn.policy.route``,
``ctrlrtn.telemetry.pricing``, …) work but are not promised across minor
versions while the project is pre-1.0.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed distribution metadata, so this can
    # never drift from pyproject.toml the way a hand-written literal did.
    __version__ = version("ctrlrtn")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
