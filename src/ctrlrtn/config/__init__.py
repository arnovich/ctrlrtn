"""Runtime settings for the gateway.

Configuration is layered from defaults through an optional YAML file and
environment variables to explicit overrides. Schema, sources, and structured
parsing live in separate modules; this package re-exports the public names.
"""

from ctrlrtn.config.loader import load_settings
from ctrlrtn.config.models import ConfigError, Settings

__all__ = ["ConfigError", "Settings", "load_settings"]
