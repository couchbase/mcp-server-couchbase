"""Unit tests for cb_mcp.utils.operational_insights.connection.

Mirrors tests/unit/test_utils.py::TestConnectionModule (the operational
server's equivalent), covering the same shape of cases for the Operational
Insights connect function: password auth, certificate (mTLS) auth, and the
validation errors each path can raise.
"""

import logging
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.servers.operational_insights.constants import (
    DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT,
)
from cb_mcp.utils.operational_insights.connection import (
    connect_to_operational_insights_cluster,
)


def _assert_handle_request_timeout(
    options, seconds: int = DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT
) -> None:
    """The cluster must be created with the configured handle_request_timeout.

    The SDK default (10s) is a deadline on the whole of fetch_results(), row
    stream included, so leaving it in place makes any async result that takes
    longer than that to download unfetchable.
    """
    assert options["timeout_options"]["handle_request_timeout"] == timedelta(
        seconds=seconds
    )


class TestOperationalInsightsConnectionModule:
    def test_connect_with_password(self, caplog: pytest.LogCaptureFixture) -> None:
        mock_cluster = MagicMock()

        with (
            patch(
                "cb_mcp.utils.operational_insights.connection.Credential"
            ) as mock_credential,
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=mock_cluster,
            ) as mock_create,
            caplog.at_level(logging.DEBUG),
        ):
            mock_credential.from_username_and_password.return_value = "cred"

            result = connect_to_operational_insights_cluster(
                "http://localhost:8095", "Administrator", "hunter2"
            )

            mock_credential.from_username_and_password.assert_called_once_with(
                "Administrator", "hunter2"
            )
            mock_credential.from_certificate.assert_not_called()
            mock_create.assert_called_once()
            endpoint, credential, options = mock_create.call_args.args
            assert (endpoint, credential) == ("http://localhost:8095", "cred")
            _assert_handle_request_timeout(options)
            assert "security_options" not in options
            assert result is mock_cluster
            assert "Using username/password authentication" in caplog.text

    def test_connect_with_client_certificate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_cluster = MagicMock()

        with (
            patch(
                "cb_mcp.utils.operational_insights.connection.Credential"
            ) as mock_credential,
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=mock_cluster,
            ) as mock_create,
            caplog.at_level(logging.DEBUG),
        ):
            mock_credential.from_certificate.return_value = "cert-cred"

            result = connect_to_operational_insights_cluster(
                "https://host:18095",
                username=None,
                password=None,
                client_cert_path="/path/client.pem",
                client_key_path="/path/client.key",
                client_cert_password="secret",
            )

            mock_credential.from_certificate.assert_called_once_with(
                "/path/client.pem", "/path/client.key", password="secret"
            )
            mock_credential.from_username_and_password.assert_not_called()
            mock_create.assert_called_once()
            endpoint, credential, options = mock_create.call_args.args
            assert (endpoint, credential) == ("https://host:18095", "cert-cred")
            _assert_handle_request_timeout(options)
            assert result is mock_cluster
            assert "Using client certificate authentication" in caplog.text

    def test_connect_with_pkcs12_bundle_needs_no_key_path(self) -> None:
        with (
            patch(
                "cb_mcp.utils.operational_insights.connection.Credential"
            ) as mock_credential,
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=MagicMock(),
            ),
        ):
            connect_to_operational_insights_cluster(
                "https://host:18095",
                username=None,
                password=None,
                client_cert_path="/path/client.p12",
                client_cert_password="secret",
            )

            mock_credential.from_certificate.assert_called_once_with(
                "/path/client.p12", None, password="secret"
            )

    def test_client_certificate_requires_https(self) -> None:
        with pytest.raises(ValueError, match="https://"):
            connect_to_operational_insights_cluster(
                "http://localhost:8095",
                username=None,
                password=None,
                client_cert_path="/path/client.pem",
            )

    def test_ca_cert_path_builds_security_options(self) -> None:
        with (
            patch("cb_mcp.utils.operational_insights.connection.Credential"),
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=MagicMock(),
            ) as mock_create,
            patch(
                "cb_mcp.utils.operational_insights.connection.SecurityOptions"
            ) as mock_security_options,
        ):
            mock_security_options.return_value = "sec-opts"

            connect_to_operational_insights_cluster(
                "http://localhost:8095",
                "Administrator",
                "hunter2",
                ca_cert_path="/path/ca.pem",
            )

            mock_security_options.assert_called_once_with(
                trust_only_pem_file="/path/ca.pem"
            )
            endpoint, _credential, options = mock_create.call_args.args
            assert endpoint == "http://localhost:8095"
            # The CA path must not displace the timeout: both ride on one
            # ClusterOptions.
            assert options["security_options"] == "sec-opts"
            _assert_handle_request_timeout(options)

    def test_handle_request_timeout_is_configurable(self) -> None:
        with (
            patch("cb_mcp.utils.operational_insights.connection.Credential"),
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=MagicMock(),
            ) as mock_create,
        ):
            connect_to_operational_insights_cluster(
                "http://localhost:8095",
                "Administrator",
                "hunter2",
                handle_request_timeout_seconds=120,
            )

            _endpoint, _credential, options = mock_create.call_args.args
            _assert_handle_request_timeout(options, seconds=120)

    def test_missing_connection_string_raises(self) -> None:
        with pytest.raises(ValueError, match="connection_string"):
            connect_to_operational_insights_cluster(None, "Administrator", "hunter2")

    def test_missing_username_and_password_raises(self) -> None:
        with pytest.raises(ValueError, match="username, password"):
            connect_to_operational_insights_cluster("http://localhost:8095", None, None)
