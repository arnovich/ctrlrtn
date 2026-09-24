"""Runtime settings for the gateway.

Configuration is layered from defaults through an optional YAML file and
environment variables to explicit overrides. The package facade preserves the
original ``ctrlrtn.config`` import surface while capability modules keep
schema, source, and structured parsing concerns separate.
"""

from ctrlrtn.config.loader import load_settings
from ctrlrtn.config.models import ConfigError, Settings

__all__ = ["ConfigError", "Settings", "load_settings"]
