"""fbadmin - a pgAdmin-style administration tool for Firebird SQL."""

__version__ = "2.1.0"

from . import driver_patches as _driver_patches  # noqa: E402

_driver_patches.apply()
