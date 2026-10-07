"""Unit tests for utils/operational/connection_string.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.core.spec import Deployment
from cb_mcp.utils.operational.connection_string import (
    _get_capella_root_ca_path,
    determine_ssl_verification,
    extract_hosts_from_connection_string,
    is_capella_connection,
    resolve_deployment,
)


class TestResolveDeployment:
    """What the connection string alone can say about where the cluster runs."""

    def test_capella_host(self) -> None:
        assert (
            resolve_deployment("couchbases://cb.abc123.cloud.couchbase.com")
            is Deployment.CAPELLA
        )

    def test_private_endpoint_is_still_capella(self) -> None:
        """A Capella private endpoint keeps the cloud.couchbase.com domain.

        Confirmed with the Capella team: every private endpoint carries it, so
        a private-link cluster must be recognised exactly like a public one —
        otherwise it would be offered the Management REST tools it cannot run.
        """
        for connection_string in (
            "couchbases://private-endpoint.abc123.cloud.couchbase.com",
            "couchbases://cb.abc123.private.cloud.couchbase.com",
        ):
            assert resolve_deployment(connection_string) is Deployment.CAPELLA

    def test_self_managed_host(self) -> None:
        assert resolve_deployment("couchbase://localhost") is Deployment.ON_PREM

    def test_lookalike_domain_is_not_capella(self) -> None:
        """The suffix must be a whole label, not a substring of the host."""
        assert (
            resolve_deployment("couchbases://notcloud.couchbase.com.evil.test")
            is Deployment.ON_PREM
        )

    def test_ipv6_host_is_self_managed(self) -> None:
        assert resolve_deployment("couchbases://[::1]:11207") is Deployment.ON_PREM

    def test_mixed_hosts_are_not_capella(self) -> None:
        """``is_capella_connection`` requires *every* host; so does this.

        A list with one non-Capella host cannot be served by Capella's control
        plane, so the Capella-only answer would be wrong.
        """
        assert (
            resolve_deployment("couchbases://a.cloud.couchbase.com,b.corp.local")
            is Deployment.ON_PREM
        )

    def test_none_when_no_hosts(self) -> None:
        """Nothing parseable means "cannot tell", never a default.

        Returning ON_PREM here would withhold any Capella-only tool on the
        strength of a malformed string.
        """
        assert resolve_deployment("") is None
        assert resolve_deployment(None) is None
        assert resolve_deployment("couchbase://") is None

    @pytest.mark.parametrize(
        "connection_string",
        ["couchbase://[bad", "couchbases://]x[", "couchbase://[::1", "[["],
    )
    def test_unparseable_string_does_not_raise(self, connection_string: str) -> None:
        """``urlparse`` raises on an unmatched "[" — this must absorb it.

        This resolver runs during startup, so a raise here is the difference
        between a server that boots and one that does not. A typo in a
        connection string has to come back as "cannot tell" and be reported by
        the connection attempt, not as a traceback out of tool gating.
        """
        assert resolve_deployment(connection_string) is None


class TestExtractHostsFromConnectionString:
    def test_single_host(self) -> None:
        assert extract_hosts_from_connection_string("couchbase://host1") == ["host1"]

    def test_multiple_hosts(self) -> None:
        assert extract_hosts_from_connection_string("couchbases://host1,host2") == [
            "host1",
            "host2",
        ]

    def test_strips_port(self) -> None:
        assert extract_hosts_from_connection_string("couchbase://host1:8091") == [
            "host1"
        ]

    def test_ipv6_host(self) -> None:
        assert extract_hosts_from_connection_string("couchbase://[::1]:8091") == ["::1"]

    def test_multiple_ipv6_hosts(self) -> None:
        assert extract_hosts_from_connection_string(
            "couchbases://[2001:db8::1]:18091,[::1]:18091"
        ) == ["2001:db8::1", "::1"]

    def test_userinfo_ignored(self) -> None:
        """A user:pass@ prefix must not be mistaken for the host."""
        assert extract_hosts_from_connection_string(
            "couchbase://user:pass@host1:8091"
        ) == ["host1"]

    def test_empty_connection_string_returns_no_hosts(self) -> None:
        assert extract_hosts_from_connection_string("") == []

    def test_malformed_connection_string_returns_no_hosts(self) -> None:
        assert extract_hosts_from_connection_string("not-a-url") == []


class TestIsCapellaConnection:
    def test_capella_host_detected(self) -> None:
        assert (
            is_capella_connection("couchbases://cb.abc123.cloud.couchbase.com") is True
        )

    def test_self_managed_host_not_detected(self) -> None:
        assert is_capella_connection("couchbase://localhost") is False
        assert is_capella_connection("couchbases://my-cluster.internal") is False

    def test_mixed_hosts_not_all_capella_not_detected(self) -> None:
        """Every host must be a Capella host for the connection to count as Capella."""
        assert (
            is_capella_connection(
                "couchbases://node1.abc123.cloud.couchbase.com,node2.internal"
            )
            is False
        )

    def test_multiple_capella_hosts_detected(self) -> None:
        assert (
            is_capella_connection(
                "couchbases://node1.abc123.cloud.couchbase.com,"
                "node2.abc123.cloud.couchbase.com"
            )
            is True
        )


class TestDetermineSSLVerification:
    def test_non_tls(self) -> None:
        """Non-TLS connection should disable SSL verification."""
        assert determine_ssl_verification("couchbase://localhost", None) is False

    def test_tls_no_cert(self) -> None:
        """TLS connection without cert uses system CA bundle."""
        assert determine_ssl_verification("couchbases://localhost", None) is True

    def test_tls_with_cert(self) -> None:
        """TLS connection with cert uses provided cert."""
        result = determine_ssl_verification("couchbases://localhost", "/path/to/ca.pem")
        assert result == "/path/to/ca.pem"


class TestDetermineSSLVerificationCapella:
    """determine_ssl_verification's Capella branch."""

    def test_capella_returns_bundled_ca_when_present(self) -> None:
        """For *.cloud.couchbase.com hosts, the Capella CA bundle should
        be returned when the file is present on disk."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com"

        with (
            patch(
                "cb_mcp.utils.operational.connection_string._get_capella_root_ca_path",
                return_value="/fake/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=True,
            ),
        ):
            result = determine_ssl_verification(capella_conn, None)

        assert result == "/fake/capella_root_ca.pem"

    def test_capella_detected_with_port_and_query_params(self) -> None:
        """Ports and query parameters must not break Capella detection."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com:11207?network=auto"

        with (
            patch(
                "cb_mcp.utils.operational.connection_string._get_capella_root_ca_path",
                return_value="/fake/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=True,
            ),
        ):
            result = determine_ssl_verification(capella_conn, None)

        assert result == "/fake/capella_root_ca.pem"

    def test_capella_detected_with_query_params_only(self) -> None:
        """Query parameters without a port must not break Capella detection."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com?network=external"

        with (
            patch(
                "cb_mcp.utils.operational.connection_string._get_capella_root_ca_path",
                return_value="/fake/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=True,
            ),
        ):
            result = determine_ssl_verification(capella_conn, None)

        assert result == "/fake/capella_root_ca.pem"

    def test_capella_falls_back_to_system_bundle_when_missing(self) -> None:
        """If the bundled Capella CA cannot be located on disk, fall back
        to the system CA bundle (verify=True) so connections still work."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com"

        with (
            patch(
                "cb_mcp.utils.operational.connection_string._get_capella_root_ca_path",
                return_value="/missing/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=False,
            ),
        ):
            result = determine_ssl_verification(capella_conn, None)

        assert result is True

    def test_capella_ignores_user_ca_path(self) -> None:
        """A Capella host should pick the bundled Capella CA over a
        user-supplied CA path — Capella certs are pinned."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com"

        with (
            patch(
                "cb_mcp.utils.operational.connection_string._get_capella_root_ca_path",
                return_value="/fake/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=True,
            ),
        ):
            result = determine_ssl_verification(capella_conn, "/user/supplied/ca.pem")

        assert result == "/fake/capella_root_ca.pem"


class TestGetCapellaRootCAPath:
    """_get_capella_root_ca_path resource resolution."""

    def test_uses_importlib_resources_when_available(self) -> None:
        """The installed-package path uses importlib.resources.files()."""
        fake_path = MagicMock()
        fake_path.__str__ = lambda self: (
            "/site-packages/cb_mcp/certs/capella_root_ca.pem"
        )

        with patch("cb_mcp.utils.operational.connection_string.files") as mock_files:
            mock_files.return_value.joinpath.return_value = fake_path
            result = _get_capella_root_ca_path()

        assert result == "/site-packages/cb_mcp/certs/capella_root_ca.pem"
        mock_files.assert_called_once_with("cb_mcp.utils.operational.certs")

    def test_falls_back_to_dev_path_when_importlib_fails(self) -> None:
        """When importlib.resources raises, the fallback returns a path
        derived from this module's location and logs a fallback message
        when the file exists."""
        with (
            patch(
                "cb_mcp.utils.operational.connection_string.files",
                side_effect=FileNotFoundError("no resource"),
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=True,
            ),
        ):
            result = _get_capella_root_ca_path()

        # Path must end with the expected filename and the certs/ dir.
        assert result.endswith(os.path.join("certs", "capella_root_ca.pem"))

    def test_returns_fallback_path_even_when_file_missing(self) -> None:
        """If both the resource lookup AND the fallback file are missing,
        the fallback path is still returned (with a warning logged)."""
        with (
            patch(
                "cb_mcp.utils.operational.connection_string.files",
                side_effect=ImportError("no module"),
            ),
            patch(
                "cb_mcp.utils.operational.connection_string.os.path.exists",
                return_value=False,
            ),
        ):
            result = _get_capella_root_ca_path()

        assert result.endswith(os.path.join("certs", "capella_root_ca.pem"))
