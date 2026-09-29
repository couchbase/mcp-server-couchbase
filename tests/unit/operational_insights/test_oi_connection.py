"""Unit tests for cb_mcp.utils.operational_insights.connection.

Mirrors tests/unit/test_utils.py::TestConnectionModule (the operational
server's equivalent), covering the same shape of cases for the Operational
Insights connect function: password auth, certificate (mTLS) auth, and the
validation errors each path can raise.
"""

from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.utils.operational_insights.connection import (
    connect_to_operational_insights_cluster,
)


class TestOiConnectionModule:
    def test_connect_with_password(self) -> None:
        mock_cluster = MagicMock()

        with (
            patch(
                "cb_mcp.utils.operational_insights.connection.Credential"
            ) as mock_credential,
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=mock_cluster,
            ) as mock_create,
        ):
            mock_credential.from_username_and_password.return_value = "cred"

            result = connect_to_operational_insights_cluster(
                "http://localhost:8095", "Administrator", "hunter2"
            )

            mock_credential.from_username_and_password.assert_called_once_with(
                "Administrator", "hunter2"
            )
            mock_credential.from_certificate.assert_not_called()
            mock_create.assert_called_once_with("http://localhost:8095", "cred", None)
            assert result is mock_cluster

    def test_connect_with_client_certificate(self) -> None:
        mock_cluster = MagicMock()

        with (
            patch(
                "cb_mcp.utils.operational_insights.connection.Credential"
            ) as mock_credential,
            patch(
                "cb_mcp.utils.operational_insights.connection.Cluster.create_instance",
                return_value=mock_cluster,
            ) as mock_create,
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
            mock_create.assert_called_once_with("https://host:18095", "cert-cred", None)
            assert result is mock_cluster

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
            patch(
                "cb_mcp.utils.operational_insights.connection.ClusterOptions"
            ) as mock_cluster_options,
        ):
            mock_security_options.return_value = "sec-opts"
            mock_cluster_options.return_value = "cluster-opts"

            connect_to_operational_insights_cluster(
                "http://localhost:8095",
                "Administrator",
                "hunter2",
                ca_cert_path="/path/ca.pem",
            )

            mock_security_options.assert_called_once_with(
                trust_only_pem_file="/path/ca.pem"
            )
            mock_cluster_options.assert_called_once_with(security_options="sec-opts")
            mock_create.assert_called_once_with(
                "http://localhost:8095", mock_create.call_args[0][1], "cluster-opts"
            )

    def test_missing_connection_string_raises(self) -> None:
        with pytest.raises(ValueError, match="connection_string"):
            connect_to_operational_insights_cluster(None, "Administrator", "hunter2")

    def test_missing_username_and_password_raises(self) -> None:
        with pytest.raises(ValueError, match="username, password"):
            connect_to_operational_insights_cluster("http://localhost:8095", None, None)
