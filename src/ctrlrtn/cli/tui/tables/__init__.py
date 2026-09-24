"""Composed console table presentation capability."""

from .detail import TableDetailMixin
from .fill import TableFillMixin
from .refresh import TableRefreshMixin


class ConsoleTables(TableRefreshMixin, TableFillMixin, TableDetailMixin):
    """Refresh, populate, and describe console tables through focused mixins."""


__all__ = ["ConsoleTables"]
