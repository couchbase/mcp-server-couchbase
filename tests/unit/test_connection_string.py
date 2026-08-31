"""Unit tests for utils/connection_string.py."""

from __future__ import annotations

from cb_mcp.utils.connection_string import (
    extract_hosts_from_connection_string,
    is_capella_connection,
)


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
