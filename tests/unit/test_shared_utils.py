"""Unit tests for the server-agnostic utility modules.

Covers ``utils/constants.py``, ``utils/config.py`` and the parts of
``utils/context.py`` that any server uses. Anything that resolves a
Couchbase cluster or touches the ``couchbase`` SDK lives in
``tests/unit/operational/test_utils.py`` instead — the same split the source
tree now has between ``cb_mcp.utils`` and ``cb_mcp.utils.operational``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cb_mcp.utils.config import get_settings
from cb_mcp.utils.constants import (
    ALLOWED_TRANSPORTS,
    DEFAULT_READ_ONLY_MODE,
    DEFAULT_TRANSPORT,
    LOGGER_ROOT,
    NETWORK_TRANSPORTS,
)
from cb_mcp.utils.context import AppContext


class TestConstants:
    """Unit tests for constants.py."""

    def test_mcp_server_name(self) -> None:
        """Verify MCP server name constant."""
        assert LOGGER_ROOT == "couchbase"

    def test_default_transport(self) -> None:
        """Verify default transport constant."""
        assert DEFAULT_TRANSPORT == "stdio"

    def test_allowed_transports(self) -> None:
        """Verify allowed transports include expected values."""
        assert "stdio" in ALLOWED_TRANSPORTS
        assert "http" in ALLOWED_TRANSPORTS
        assert "sse" in ALLOWED_TRANSPORTS

    def test_network_transports(self) -> None:
        """Verify network transports are subset of allowed."""
        for transport in NETWORK_TRANSPORTS:
            assert transport in ALLOWED_TRANSPORTS

    def test_default_read_only_mode(self) -> None:
        """Verify default read-only mode is True for safety."""
        assert DEFAULT_READ_ONLY_MODE is True


class TestConfigModule:
    """Unit tests for config.py module."""

    def test_get_settings_reads_from_lifespan_context(self) -> None:
        """get_settings returns the mapping attached to AppContext.settings."""
        payload = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
        }
        mock_ctx = MagicMock()
        mock_ctx.request_context.lifespan_context.settings = payload

        assert get_settings(mock_ctx) is payload

    def test_get_settings_returns_empty_when_unset(self) -> None:
        """Before the lifespan populates settings, the default empty dict is returned."""
        mock_ctx = MagicMock()
        mock_ctx.request_context.lifespan_context.settings = {}

        assert get_settings(mock_ctx) == {}


class TestAppContext:
    """The lifespan context itself, independent of any backing service."""

    def test_app_context_default_values(self) -> None:
        """Verify AppContext has correct default values."""
        ctx = AppContext()
        assert ctx.cluster_provider is None
        assert ctx.read_only_mode is True

    def test_app_context_with_provider(self) -> None:
        """Verify AppContext can hold a cluster provider."""
        mock_provider = MagicMock()
        ctx = AppContext(cluster_provider=mock_provider, read_only_mode=False)

        assert ctx.cluster_provider is mock_provider
        assert ctx.read_only_mode is False
