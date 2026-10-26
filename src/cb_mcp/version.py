"""This package's version — what the server reports as its build.

Top-level rather than under :mod:`cb_mcp.utils` because ``utils/__init__``
imports ``scope_enforcement``, which imports :mod:`cb_mcp.audit`; ``cb_mcp``
itself imports nothing, so this is safe to call from any layer.
"""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "couchbase-mcp-server"

#: Reported when distribution metadata is absent (a source checkout).
UNKNOWN_VERSION = "unknown"


def server_version() -> str:
    """Return the installed version, or ``UNKNOWN_VERSION`` if not installed.

    A reported value, never load-bearing: a missing distribution must not stop
    a server from starting.
    """
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
