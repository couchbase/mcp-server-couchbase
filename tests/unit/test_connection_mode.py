"""
Tests for the connection_mode (Operational vs Enterprise Analytics) infra.

This module tests:
- is_capella_connection_string() host detection
- connect_to_analytics_cluster() building the couchbase_analytics SDK objects
  correctly (in particular, overriding the SDK's trust_only_capella default)
- StaticClusterProvider dispatching to the right connect function per mode
- The server startup guard rejecting connection_mode=analytics against a
  Capella connection string
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import cb_mcp.utils.logging as logmod
import mcp_server
from cb_mcp.utils.connection import connect_to_analytics_cluster
from cb_mcp.utils.constants import (
    CONNECTION_MODE_ANALYTICS,
    CONNECTION_MODE_OPERATIONAL,
    DEFAULT_CONNECTION_MODE,
)
from cb_mcp.utils.index_utils import is_capella_connection_string
from providers.static import StaticClusterProvider


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    """See test_cli_options.py — the couchbase SDK's configure_logging is
    one-shot per process; patch it so multiple tests in this module (each
    invoking mcp_server.main) don't collide.
    """
    with patch.object(logmod.couchbase, "configure_logging"):
        yield


class TestIsCapellaConnectionString:
    def test_capella_host_detected(self):
        assert is_capella_connection_string(
            "couchbases://cb.abc123.cloud.couchbase.com"
        )

    def test_capella_host_detected_for_https_scheme(self):
        # Enterprise Analytics connection strings are http(s)://, not
        # couchbase(s):// — the host-suffix check must still work since it's
        # scheme-agnostic (urlparse-based).
        assert is_capella_connection_string(
            "https://analytics.abc123.cloud.couchbase.com:18095"
        )

    def test_self_managed_host_not_capella(self):
        assert not is_capella_connection_string("couchbase://localhost")
        assert not is_capella_connection_string("https://ea.internal.example.com")

    def test_mixed_hosts_not_all_capella(self):
        assert not is_capella_connection_string(
            "couchbase://node1.internal,node2.cloud.couchbase.com"
        )


class TestConnectToAnalyticsCluster:
    def test_disables_trust_only_capella_by_default(self):
        """The SDK defaults SecurityOptions.trust_only_capella to True; for
        self-managed EA clusters this must be explicitly turned off or every
        connection would fail certificate verification."""
        with (
            patch("cb_mcp.utils.connection.AnalyticsCluster") as mock_cluster_cls,
            patch("cb_mcp.utils.connection.AnalyticsCredential") as mock_credential_cls,
            patch(
                "cb_mcp.utils.connection.AnalyticsSecurityOptions"
            ) as mock_security_options_cls,
        ):
            mock_cluster_cls.create_instance.return_value = MagicMock()

            connect_to_analytics_cluster(
                "https://ea.internal.example.com:18095", "user", "pass"
            )

            mock_credential_cls.from_username_and_password.assert_called_once_with(
                "user", "pass"
            )
            mock_security_options_cls.assert_called_once_with(trust_only_capella=False)
            mock_cluster_cls.create_instance.assert_called_once()

    def test_passes_ca_cert_path_as_trust_only_pem_file(self):
        with (
            patch("cb_mcp.utils.connection.AnalyticsCluster") as mock_cluster_cls,
            patch("cb_mcp.utils.connection.AnalyticsCredential"),
            patch(
                "cb_mcp.utils.connection.AnalyticsSecurityOptions"
            ) as mock_security_options_cls,
        ):
            mock_cluster_cls.create_instance.return_value = MagicMock()

            connect_to_analytics_cluster(
                "https://ea.internal.example.com:18095",
                "user",
                "pass",
                ca_cert_path="/etc/ssl/ea-ca.pem",
            )

            mock_security_options_cls.assert_called_once_with(
                trust_only_capella=False,
                trust_only_pem_file="/etc/ssl/ea-ca.pem",
            )

    def test_raises_on_failure(self):
        with patch("cb_mcp.utils.connection.AnalyticsCluster") as mock_cluster_cls:
            mock_cluster_cls.create_instance.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError, match="boom"):
                connect_to_analytics_cluster(
                    "https://ea.internal.example.com:18095", "user", "pass"
                )


class TestStaticClusterProviderConnectionMode:
    def test_operational_mode_calls_operational_connect(self):
        settings = {
            "connection_mode": CONNECTION_MODE_OPERATIONAL,
            "connection_string": "couchbase://localhost",
            "username": "user",
            "password": "pass",
        }
        provider = StaticClusterProvider(settings=settings)

        with (
            patch("providers.static.connect_to_couchbase_cluster") as mock_operational,
            patch("providers.static.connect_to_analytics_cluster") as mock_analytics,
        ):
            mock_operational.return_value = MagicMock()
            provider._connect()

        mock_operational.assert_called_once()
        mock_analytics.assert_not_called()

    def test_analytics_mode_calls_analytics_connect(self):
        settings = {
            "connection_mode": CONNECTION_MODE_ANALYTICS,
            "connection_string": "https://ea.internal.example.com:18095",
            "username": "user",
            "password": "pass",
        }
        provider = StaticClusterProvider(settings=settings)

        with (
            patch("providers.static.connect_to_couchbase_cluster") as mock_operational,
            patch("providers.static.connect_to_analytics_cluster") as mock_analytics,
        ):
            mock_analytics.return_value = MagicMock()
            provider._connect()

        mock_analytics.assert_called_once()
        mock_operational.assert_not_called()

    def test_default_settings_use_operational_connect(self):
        """No connection_mode key at all (e.g. an older settings dict)
        behaves like the operational default."""
        settings = {
            "connection_string": "couchbase://localhost",
            "username": "user",
            "password": "pass",
        }
        provider = StaticClusterProvider(settings=settings)

        with patch("providers.static.connect_to_couchbase_cluster") as mock_operational:
            mock_operational.return_value = MagicMock()
            provider._connect()

        mock_operational.assert_called_once()


class TestConnectionModeStartupGuard:
    """mcp_server.main must reject connection_mode=analytics against a
    Capella connection string before ever attempting to connect."""

    def _invoke(self, args: list[str]):
        runner = CliRunner()
        return runner.invoke(mcp_server.main, args)

    def test_analytics_mode_rejects_capella_connection_string(self):
        result = self._invoke(
            [
                "--connection-mode",
                "analytics",
                "--connection-string",
                "https://cb.abc123.cloud.couchbase.com",
                "--username",
                "user",
                "--password",
                "pass",
            ]
        )
        assert result.exit_code != 0
        assert "self-managed" in result.output

    def test_analytics_mode_allows_self_managed_connection_string(self):
        """Startup should get past the Capella guard for a self-managed EA
        connection string (it may still fail later trying to actually reach
        the cluster, which this test doesn't exercise)."""
        fake_instance = MagicMock()
        with patch("mcp_server.FastMCP", return_value=fake_instance):
            result = self._invoke(
                [
                    "--connection-mode",
                    "analytics",
                    "--connection-string",
                    "https://ea.internal.example.com:18095",
                    "--username",
                    "user",
                    "--password",
                    "pass",
                ]
            )
        assert result.exit_code == 0, result.output

    def test_operational_mode_allows_capella_connection_string(self):
        """The Capella guard only applies to analytics mode — operational
        mode against Capella is the long-supported, unaffected path."""
        fake_instance = MagicMock()
        with patch("mcp_server.FastMCP", return_value=fake_instance):
            result = self._invoke(
                [
                    "--connection-string",
                    "couchbases://cb.abc123.cloud.couchbase.com",
                    "--username",
                    "user",
                    "--password",
                    "pass",
                ]
            )
        assert result.exit_code == 0, result.output


class TestConnectionModeDefault:
    def test_default_connection_mode_is_operational(self):
        assert DEFAULT_CONNECTION_MODE == CONNECTION_MODE_OPERATIONAL
