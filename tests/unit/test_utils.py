"""
Unit tests for utility modules.

Tests for:
- utils/index_utils.py - Index utility functions
- utils/constants.py - Constants validation
- utils/config.py - Configuration functions
- utils/connection.py - Connection functions
- utils/context.py - Context management functions
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cb_mcp.tools.index import (
    fetch_indexes_via_query_service,
    list_indexes,
)
from cb_mcp.utils.config import get_settings
from cb_mcp.utils.connection import connect_to_bucket, connect_to_couchbase_cluster
from cb_mcp.utils.constants import (
    ALLOWED_TRANSPORTS,
    DEFAULT_READ_ONLY_MODE,
    DEFAULT_TRANSPORT,
    MCP_SERVER_NAME,
    NETWORK_TRANSPORTS,
)
from cb_mcp.utils.context import (
    AppContext,
    get_cluster_connection,
)
from cb_mcp.utils.index_utils import (
    _build_query_params,
    _determine_ssl_verification,
    _extract_hosts_from_connection_string,
    _get_capella_root_ca_path,
    clean_index_definition,
    fetch_indexes_from_rest_api,
    parse_major_version,
    process_index_data_from_query,
    process_index_data_from_rest_api,
    resolve_cluster_major_version,
    validate_connection_settings,
    validate_filter_params,
)
from providers.static import StaticClusterProvider


class TestIndexUtilsFunctions:
    """Unit tests for index_utils.py pure functions."""

    def test_validate_filter_params_valid_all(self) -> None:
        """Validate all filter params provided correctly."""
        # Should not raise
        validate_filter_params(
            bucket_name="bucket",
            scope_name="scope",
            collection_name="collection",
            index_name="index",
        )

    def test_validate_filter_params_valid_bucket_only(self) -> None:
        """Validate bucket-only filter is valid."""
        validate_filter_params(
            bucket_name="bucket",
            scope_name=None,
            collection_name=None,
        )

    def test_validate_filter_params_valid_bucket_scope(self) -> None:
        """Validate bucket+scope filter is valid."""
        validate_filter_params(
            bucket_name="bucket",
            scope_name="scope",
            collection_name=None,
        )

    def test_validate_filter_params_scope_without_bucket(self) -> None:
        """Scope without bucket should raise ValueError."""
        with pytest.raises(ValueError, match="bucket_name is required"):
            validate_filter_params(
                bucket_name=None,
                scope_name="scope",
                collection_name=None,
            )

    def test_validate_filter_params_collection_without_scope(self) -> None:
        """Collection without scope should raise ValueError."""
        with pytest.raises(ValueError, match="bucket_name and scope_name are required"):
            validate_filter_params(
                bucket_name="bucket",
                scope_name=None,
                collection_name="collection",
            )

    def test_validate_filter_params_index_without_collection(self) -> None:
        """Index without collection should raise ValueError."""
        with pytest.raises(ValueError, match="collection_name are required"):
            validate_filter_params(
                bucket_name="bucket",
                scope_name="scope",
                collection_name=None,
                index_name="index",
            )

    def test_validate_connection_settings_valid(self) -> None:
        """Valid connection settings should not raise."""
        settings = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
            "password": "password",
        }
        # Should not raise
        validate_connection_settings(settings)

    def test_validate_connection_settings_missing_password(self) -> None:
        """Missing password should raise ValueError."""
        settings = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
        }
        with pytest.raises(ValueError, match="password"):
            validate_connection_settings(settings)

    def test_validate_connection_settings_empty(self) -> None:
        """Empty settings should raise ValueError."""
        with pytest.raises(ValueError, match="connection_string"):
            validate_connection_settings({})

    def test_clean_index_definition_with_quotes(self) -> None:
        """Clean index definition with surrounding quotes."""
        definition = '"CREATE INDEX idx ON bucket(field)"'
        result = clean_index_definition(definition)
        assert result == "CREATE INDEX idx ON bucket(field)"

    def test_clean_index_definition_with_escaped_quotes(self) -> None:
        """Clean index definition with escaped quotes."""
        definition = 'CREATE INDEX idx ON bucket(\\"field\\")'
        result = clean_index_definition(definition)
        assert result == 'CREATE INDEX idx ON bucket("field")'

    def test_clean_index_definition_empty(self) -> None:
        """Clean empty definition returns empty string."""
        assert clean_index_definition("") == ""
        assert clean_index_definition(None) == ""

    def test_process_index_data_basic(self) -> None:
        """Process basic index data."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
            "bucket": "travel-sample",
            "scope": "_default",
            "collection": "_default",
            "lastScanTime": "NA",
        }
        result = process_index_data_from_rest_api(idx)

        assert result is not None
        assert result["name"] == "idx_test"
        assert result["bucket"] == "travel-sample"
        assert result["status"] == "Ready"
        assert result["isPrimary"] is False
        assert "lastScanTime" in result

    def test_process_index_data_with_last_scan_time(self) -> None:
        """Process index data includes lastScanTime."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
            "bucket": "bucket",
            "scope": "scope",
            "collection": "collection",
            "lastScanTime": "Thu Feb 26 13:12:55 IST 2026",
            "extra_field": "some_value",
        }
        result = process_index_data_from_rest_api(idx)

        assert result is not None
        assert result["lastScanTime"] == "Thu Feb 26 13:12:55 IST 2026"
        assert "extra_field" not in result

    def test_process_index_data_without_raw_stats(self) -> None:
        """Process index data without raw stats by default."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
            "bucket": "bucket",
            "lastScanTime": "NA",
        }
        result = process_index_data_from_rest_api(idx)

        assert result is not None
        assert "raw_index_stats" not in result

    def test_rest_missing_name_falls_back_to_raw(self) -> None:
        """Missing 'name' field should return raw fallback with warning message."""
        idx = {"status": "Ready", "bucket": "bucket"}
        result = process_index_data_from_rest_api(idx)
        assert result == {
            "warning": result["warning"],
            "raw_index_stats": idx,
        }
        assert "name" in result["warning"]
        # Raw stats must be the unmodified original input.
        assert result["raw_index_stats"] is idx

    def test_rest_missing_definition_falls_back_to_raw(self) -> None:
        """Missing 'definition' field should return raw fallback with warning message."""
        idx = {"name": "idx_test", "status": "Ready", "bucket": "bucket"}
        result = process_index_data_from_rest_api(idx)
        assert "warning" in result
        assert "definition" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_rest_missing_bucket_falls_back_to_raw(self) -> None:
        """Missing 'bucket' field should return raw fallback. REST always
        emits bucket today, so its absence indicates a problem in fetching
        the index information."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
        }
        result = process_index_data_from_rest_api(idx)
        assert "warning" in result
        assert "bucket" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_process_index_data_primary_index(self) -> None:
        """Process primary index data."""
        idx = {
            "name": "#primary",
            "definition": "CREATE PRIMARY INDEX `#primary` ON `bucket`",
            "status": "Ready",
            "isPrimary": True,
            "bucket": "bucket",
            "lastScanTime": "NA",
        }
        result = process_index_data_from_rest_api(idx)

        assert result is not None
        assert result["isPrimary"] is True

    def test_extract_hosts_single_host(self) -> None:
        """Extract single host from connection string."""
        conn_str = "couchbase://localhost"
        hosts = _extract_hosts_from_connection_string(conn_str)
        assert hosts == ["localhost"]

    def test_extract_hosts_multiple_hosts(self) -> None:
        """Extract multiple hosts from connection string."""
        conn_str = "couchbase://host1,host2,host3"
        hosts = _extract_hosts_from_connection_string(conn_str)
        assert hosts == ["host1", "host2", "host3"]

    def test_extract_hosts_with_port(self) -> None:
        """Extract hosts with port numbers."""
        conn_str = "couchbase://localhost:8091"
        hosts = _extract_hosts_from_connection_string(conn_str)
        assert hosts == ["localhost"]

    def test_extract_hosts_tls_connection(self) -> None:
        """Extract hosts from TLS connection string."""
        conn_str = "couchbases://secure-host.example.com"
        hosts = _extract_hosts_from_connection_string(conn_str)
        assert hosts == ["secure-host.example.com"]

    def test_extract_hosts_capella(self) -> None:
        """Extract hosts from Capella connection string."""
        conn_str = "couchbases://cb.abc123.cloud.couchbase.com"
        hosts = _extract_hosts_from_connection_string(conn_str)
        assert hosts == ["cb.abc123.cloud.couchbase.com"]

    def test_build_query_params_all(self) -> None:
        """Build query params with all fields."""
        params = _build_query_params(
            bucket_name="bucket",
            scope_name="scope",
            collection_name="collection",
            index_name="index",
        )
        assert params == {
            "bucket": "bucket",
            "scope": "scope",
            "collection": "collection",
            "index": "index",
        }

    def test_build_query_params_partial(self) -> None:
        """Build query params with some fields."""
        params = _build_query_params(
            bucket_name="bucket",
            scope_name=None,
            collection_name=None,
        )
        assert params == {"bucket": "bucket"}

    def test_build_query_params_empty(self) -> None:
        """Build query params with no fields."""
        params = _build_query_params(
            bucket_name=None,
            scope_name=None,
            collection_name=None,
        )
        assert params == {}

    def test_determine_ssl_non_tls(self) -> None:
        """Non-TLS connection should disable SSL verification."""
        result = _determine_ssl_verification("couchbase://localhost", None)
        assert result is False

    def test_determine_ssl_tls_no_cert(self) -> None:
        """TLS connection without cert uses system CA bundle."""
        result = _determine_ssl_verification("couchbases://localhost", None)
        assert result is True

    def test_determine_ssl_tls_with_cert(self) -> None:
        """TLS connection with cert uses provided cert."""
        result = _determine_ssl_verification(
            "couchbases://localhost", "/path/to/ca.pem"
        )
        assert result == "/path/to/ca.pem"

    def test_parse_major_version_basic(self) -> None:
        """Parse a typical full version string."""
        assert parse_major_version("8.0.0-1928-enterprise") == 8
        assert parse_major_version("7.6.11") == 7

    def test_parse_major_version_only_major(self) -> None:
        """Parse a string that is just a major version."""
        assert parse_major_version("8") == 8

    def test_parse_major_version_v_prefix(self) -> None:
        """A 'v' prefix should be tolerated."""
        assert parse_major_version("v8.0.0") == 8

    def test_parse_major_version_empty_or_none(self) -> None:
        """Empty/None inputs should raise ValueError."""
        with pytest.raises(ValueError):
            parse_major_version("")
        with pytest.raises(ValueError):
            parse_major_version(None)

    def test_parse_major_version_malformed(self) -> None:
        """Malformed input should raise ValueError."""
        with pytest.raises(ValueError):
            parse_major_version("unknown")
        with pytest.raises(ValueError):
            parse_major_version("abc.def")

    def test_process_index_data_from_query_basic(self) -> None:
        """Map a typical post-LET system:indexes row to the standard schema.

        The processor reads bucket/scope/collection (LET-injected by
        fetch_indexes_via_query_service) and ignores the raw bucket_id /
        scope_id / keyspace_id fields, so the fixture only needs the
        injected shape.
        """
        idx = {
            "name": "def_inventory_airport_city",
            "bucket": "travel-sample",
            "scope": "inventory",
            "collection": "airport",
            "state": "online",
            "metadata": {
                "definition": (
                    "CREATE INDEX `def_inventory_airport_city` ON "
                    "`travel-sample`.`inventory`.`airport`(`city`)"
                ),
                "last_scan_time": "2026-02-26T13:12:56.581+05:30",
            },
        }

        result = process_index_data_from_query(idx)

        assert result is not None
        assert result["name"] == "def_inventory_airport_city"
        assert result["bucket"] == "travel-sample"
        assert result["scope"] == "inventory"
        assert result["collection"] == "airport"
        assert result["status"] == "online"
        assert "city" in result["definition"]
        assert result["isPrimary"] is False
        assert result["lastScanTime"] == "2026-02-26T13:12:56.581+05:30"

    def test_process_index_data_from_query_primary(self) -> None:
        """Primary index rows should set isPrimary=True."""
        idx = {
            "name": "def_inventory_airport_primary",
            "bucket": "travel-sample",
            "scope": "inventory",
            "collection": "airport",
            "is_primary": True,
            "state": "online",
            "metadata": {
                "definition": "CREATE PRIMARY INDEX ...",
                "last_scan_time": None,
            },
        }

        result = process_index_data_from_query(idx)

        assert result is not None
        assert result["isPrimary"] is True

    def test_process_index_data_from_query_last_scan_time(self) -> None:
        """lastScanTime should be included from metadata."""
        idx = {
            "name": "idx",
            "bucket": "b",
            "scope": "s",
            "collection": "c",
            "state": "online",
            "metadata": {
                "definition": "CREATE INDEX idx ON b.s.c(x)",
                "last_scan_time": "2026-02-26T13:12:56.581+05:30",
            },
        }
        result = process_index_data_from_query(idx)
        assert result is not None
        assert result["lastScanTime"] == "2026-02-26T13:12:56.581+05:30"

    def test_process_index_data_from_query_without_raw_stats(self) -> None:
        """Default (processed) shape should not carry raw-row keys."""
        idx = {
            "name": "idx",
            "bucket_id": "b",
            "scope_id": "s",
            "keyspace_id": "c",
            "bucket": "b",
            "scope": "s",
            "collection": "c",
            "state": "online",
            "metadata": {
                "definition": "CREATE INDEX idx ON b.s.c(x)",
                "last_scan_time": None,
            },
        }
        result = process_index_data_from_query(idx)
        assert result is not None
        # Raw-shape keys should not leak into the processed output.
        assert "raw_index_stats" not in result
        assert "bucket_id" not in result
        assert "scope_id" not in result
        assert "keyspace_id" not in result
        assert "state" not in result  # processed shape uses 'status'

    def test_query_missing_name_falls_back_to_raw(self) -> None:
        """Rows without a name should return raw fallback with warning message."""
        idx = {"bucket_id": "b"}
        result = process_index_data_from_query(idx)
        assert "warning" in result
        assert "name" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_query_missing_metadata_falls_back_to_raw(self) -> None:
        """Missing metadata.definition should return raw fallback, not empty string."""
        idx = {
            "name": "idx",
            "bucket_id": "b",
            "scope_id": "s",
            "keyspace_id": "c",
            "state": "online",
        }
        result = process_index_data_from_query(idx)
        assert "warning" in result
        assert "metadata.definition" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_query_missing_let_bucket_falls_back_to_raw(self) -> None:
        """Query path: bucket is injected by the SQL LET clause. Its absence
        means the row didn't come from our SQL or the LET semantics have
        changed — must fail loud."""
        idx = {
            "name": "idx",
            "state": "online",
            "scope": "s",
            "collection": "c",
            "metadata": {"definition": "CREATE INDEX idx ON b.s.c(x)"},
        }
        result = process_index_data_from_query(idx)
        assert "warning" in result
        assert "bucket" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_query_missing_let_scope_falls_back_to_raw(self) -> None:
        """Query path: scope is injected by the SQL LET clause — same fail-
        loud contract as bucket."""
        idx = {
            "name": "idx",
            "state": "online",
            "bucket": "b",
            "collection": "c",
            "metadata": {"definition": "CREATE INDEX idx ON b.s.c(x)"},
        }
        result = process_index_data_from_query(idx)
        assert "warning" in result
        assert "scope" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_query_missing_let_collection_falls_back_to_raw(self) -> None:
        """Query path: collection is injected by the SQL LET clause — same
        fail-loud contract as bucket."""
        idx = {
            "name": "idx",
            "state": "online",
            "bucket": "b",
            "scope": "s",
            "metadata": {"definition": "CREATE INDEX idx ON b.s.c(x)"},
        }
        result = process_index_data_from_query(idx)
        assert "warning" in result
        assert "collection" in result["warning"]
        assert result["raw_index_stats"] is idx

    # ------------------------------------------------------------------
    # Failure-mode tests: missing status, missing lastScanTime, etc.
    # ------------------------------------------------------------------

    def test_rest_missing_status_falls_back_to_raw(self) -> None:
        """REST path: missing 'status' must NOT default to empty string —
        the row should fall back to raw with a warning message."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "bucket": "bucket",
            "scope": "scope",
            "collection": "collection",
        }
        result = process_index_data_from_rest_api(idx)
        assert result.get("status") is None
        assert "warning" in result
        assert "status" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_query_missing_state_falls_back_to_raw(self) -> None:
        """Query path: missing 'state' must NOT default to empty string —
        the row should fall back to raw with a warning message."""
        idx = {
            "name": "idx",
            "bucket_id": "b",
            "scope_id": "s",
            "keyspace_id": "c",
            "metadata": {"definition": "CREATE INDEX idx ON b.s.c(x)"},
        }
        result = process_index_data_from_query(idx)
        assert result.get("status") is None
        assert "warning" in result
        assert "state" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_rest_missing_last_scan_time_key_falls_back_to_raw(self) -> None:
        """REST path: REST always emits the 'lastScanTime' key today (with
        literal 'NA' for never-scanned). Its absence indicates a schema
        change upstream and must fall back to raw.
        """
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
            "bucket": "bucket",
            "scope": "scope",
            "collection": "collection",
            # no lastScanTime — simulate a schema change
        }
        result = process_index_data_from_rest_api(idx)
        assert "warning" in result
        assert "lastScanTime" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_rest_literal_NA_last_scan_time_passes_through(self) -> None:
        """REST path: never-scanned indexes carry the literal 'NA' string —
        this is the normal case and must NOT trigger a fallback."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
            "bucket": "bucket",
            "lastScanTime": "NA",
        }
        result = process_index_data_from_rest_api(idx)
        assert "warning" not in result
        assert result["lastScanTime"] == "NA"

    def test_rest_null_last_scan_time_omitted(self) -> None:
        """REST path: explicit null lastScanTime (defensive — REST doesn't
        emit null today but if it ever does we omit the field rather than
        surfacing a meaningless null to the caller)."""
        idx = {
            "name": "idx_test",
            "definition": "CREATE INDEX idx_test ON bucket(field)",
            "status": "Ready",
            "bucket": "bucket",
            "lastScanTime": None,
        }
        result = process_index_data_from_rest_api(idx)
        assert "warning" not in result
        assert "lastScanTime" not in result

    def test_query_missing_last_scan_time_key_falls_back_to_raw(self) -> None:
        """Query path: system:indexes always emits 'metadata.last_scan_time'
        today (with value null for never-scanned). Its absence indicates a
        schema change upstream and must fall back to raw.
        """
        idx = {
            "name": "idx",
            "bucket": "b",
            "scope": "s",
            "collection": "c",
            "state": "online",
            # metadata is present but the last_scan_time key is missing —
            # this is the schema-drift case we want to detect.
            "metadata": {"definition": "CREATE INDEX idx ON b.s.c(x)"},
        }
        result = process_index_data_from_query(idx)
        assert "warning" in result
        assert "last_scan_time" in result["warning"]
        assert result["raw_index_stats"] is idx

    def test_query_null_last_scan_time_passes_through(self) -> None:
        """Query path: null last_scan_time (never-scanned) is honored verbatim
        — we don't substitute 'NA' or any other sentinel."""
        idx = {
            "name": "idx",
            "bucket": "b",
            "scope": "s",
            "collection": "c",
            "state": "online",
            "metadata": {
                "definition": "CREATE INDEX idx ON b.s.c(x)",
                "last_scan_time": None,
            },
        }
        result = process_index_data_from_query(idx)
        assert "warning" not in result
        assert result["lastScanTime"] is None

    def test_query_timestamp_last_scan_time_passes_through(self) -> None:
        """Query path: timestamp last_scan_time is passed through verbatim."""
        ts = "2026-02-26T13:12:56.581+05:30"
        idx = {
            "name": "idx",
            "bucket": "b",
            "scope": "s",
            "collection": "c",
            "state": "online",
            "metadata": {
                "definition": "CREATE INDEX idx ON b.s.c(x)",
                "last_scan_time": ts,
            },
        }
        result = process_index_data_from_query(idx)
        assert "warning" not in result
        assert result["lastScanTime"] == ts

    def test_query_legacy_keyspace_id_only_works(self) -> None:
        """Query path: legacy bucket-level indexes are normalised in SQL via
        the LET clause in fetch_indexes_via_query_service, so by the time the
        processor sees the row, bucket/scope/collection are already populated.
        """
        # Simulated post-LET shape for a legacy bucket-level index:
        # the SQL coerces scope="_default", collection="_default" when
        # bucket_id is absent.
        idx = {
            "name": "legacy_idx",
            "keyspace_id": "my-bucket",
            "bucket": "my-bucket",
            "scope": "_default",
            "collection": "_default",
            "state": "online",
            "metadata": {
                "definition": "CREATE INDEX legacy_idx ON `my-bucket`(x)",
                "last_scan_time": None,
            },
        }
        result = process_index_data_from_query(idx)
        assert "warning" not in result
        assert result["bucket"] == "my-bucket"
        assert result["scope"] == "_default"
        assert result["collection"] == "_default"

    def test_rest_status_passes_through_as_is(self) -> None:
        """REST API status strings should pass through unchanged."""
        for status in (
            "Ready",
            "Building",
            "Created",
            "Error",
            "Scheduled for Creation",
            "Building (Upgrading)",
            "SomeNewStatus",
        ):
            idx = {
                "name": "idx_test",
                "definition": "CREATE INDEX idx_test ON bucket(field)",
                "status": status,
                "bucket": "bucket",
                "lastScanTime": "NA",
            }
            result = process_index_data_from_rest_api(idx)
            assert result["status"] == status


class TestConstants:
    """Unit tests for constants.py."""

    def test_mcp_server_name(self) -> None:
        """Verify MCP server name constant."""
        assert MCP_SERVER_NAME == "couchbase"

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


class TestConnectionModule:
    """Unit tests for connection.py module."""

    def test_connect_to_couchbase_cluster_with_password(self) -> None:
        """Verify password authentication path is used correctly."""
        mock_cluster = MagicMock()

        with (
            patch("cb_mcp.utils.connection.PasswordAuthenticator") as mock_auth,
            patch("cb_mcp.utils.connection.ClusterOptions") as mock_options,
            patch(
                "cb_mcp.utils.connection.Cluster", return_value=mock_cluster
            ) as mock_cluster_class,
        ):
            mock_options_instance = MagicMock()
            mock_options.return_value = mock_options_instance

            result = connect_to_couchbase_cluster(
                connection_string="couchbase://localhost",
                username="admin",
                password="password",
            )

            mock_auth.assert_called_once_with("admin", "password", cert_path=None)
            mock_cluster_class.assert_called_once()
            mock_cluster.wait_until_ready.assert_called_once()
            assert result == mock_cluster

    def test_connect_to_couchbase_cluster_with_certificate(self) -> None:
        """Verify certificate authentication path is used when certs provided."""
        mock_cluster = MagicMock()

        with (
            patch("cb_mcp.utils.connection.CertificateAuthenticator") as mock_cert_auth,
            patch("cb_mcp.utils.connection.ClusterOptions") as mock_options,
            patch("cb_mcp.utils.connection.Cluster", return_value=mock_cluster),
            patch("cb_mcp.utils.connection.os.path.exists", return_value=True),
        ):
            mock_options_instance = MagicMock()
            mock_options.return_value = mock_options_instance

            result = connect_to_couchbase_cluster(
                connection_string="couchbases://localhost",
                username="admin",
                password="password",
                ca_cert_path="/path/to/ca.pem",
                client_cert_path="/path/to/client.pem",
                client_key_path="/path/to/client.key",
            )

            mock_cert_auth.assert_called_once_with(
                cert_path="/path/to/client.pem",
                key_path="/path/to/client.key",
                trust_store_path="/path/to/ca.pem",
            )
            assert result == mock_cluster

    def test_connect_to_couchbase_cluster_missing_cert_file(self) -> None:
        """Verify FileNotFoundError raised when cert files don't exist."""
        with (
            patch("cb_mcp.utils.connection.os.path.exists", return_value=False),
            pytest.raises(
                FileNotFoundError, match="Client certificate files not found"
            ),
        ):
            connect_to_couchbase_cluster(
                connection_string="couchbases://localhost",
                username="admin",
                password="password",
                client_cert_path="/path/to/missing.pem",
                client_key_path="/path/to/missing.key",
            )

    def test_connect_to_couchbase_cluster_connection_failure(self) -> None:
        """Verify exceptions are re-raised on connection failure."""
        with (
            patch("cb_mcp.utils.connection.PasswordAuthenticator"),
            patch("cb_mcp.utils.connection.ClusterOptions"),
            patch(
                "cb_mcp.utils.connection.Cluster",
                side_effect=Exception("Connection refused"),
            ),
            pytest.raises(Exception, match="Connection refused"),
        ):
            connect_to_couchbase_cluster(
                connection_string="couchbase://invalid-host",
                username="admin",
                password="password",
            )

    def test_connect_to_bucket_success(self) -> None:
        """Verify connect_to_bucket returns bucket object."""
        mock_cluster = MagicMock()
        mock_bucket = MagicMock()
        mock_cluster.bucket.return_value = mock_bucket

        result = connect_to_bucket(mock_cluster, "my-bucket")

        mock_cluster.bucket.assert_called_once_with("my-bucket")
        assert result == mock_bucket

    def test_connect_to_bucket_failure(self) -> None:
        """Verify connect_to_bucket raises exception on failure."""
        mock_cluster = MagicMock()
        mock_cluster.bucket.side_effect = Exception("Bucket not found")

        with pytest.raises(Exception, match="Bucket not found"):
            connect_to_bucket(mock_cluster, "nonexistent-bucket")


class TestContextModule:
    """Unit tests for context.py module."""

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

    def test_get_cluster_connection_delegates_to_provider(self) -> None:
        """get_cluster_connection calls into the provider attached to AppContext."""
        mock_cluster = MagicMock()
        mock_provider = MagicMock()
        mock_provider.get_cluster = MagicMock(return_value=mock_cluster)

        mock_ctx = MagicMock()
        mock_ctx.request_context.lifespan_context.cluster_provider = mock_provider

        result = get_cluster_connection(mock_ctx)

        assert result is mock_cluster
        mock_provider.get_cluster.assert_called_once_with(mock_ctx)

    def test_get_cluster_connection_raises_without_provider(self) -> None:
        """get_cluster_connection fails fast if the lifespan forgot to wire a provider."""
        mock_ctx = MagicMock()
        mock_ctx.request_context.lifespan_context.cluster_provider = None

        with pytest.raises(RuntimeError, match="Cluster provider not initialized"):
            get_cluster_connection(mock_ctx)

    def test_static_cluster_provider_connects_lazily(self) -> None:
        """StaticClusterProvider defers connection until first get_cluster call."""
        mock_cluster = MagicMock()
        mock_settings = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
            "password": "password",
        }

        with patch(
            "providers.static.connect_to_couchbase_cluster",
            return_value=mock_cluster,
        ) as mock_connect:
            provider = StaticClusterProvider(settings=mock_settings)
            # Constructor alone must not open a connection.
            mock_connect.assert_not_called()

            result = provider.get_cluster(MagicMock())
            assert result is mock_cluster
            mock_connect.assert_called_once()

    def test_static_cluster_provider_caches_cluster(self) -> None:
        """Repeated get_cluster calls reuse the first cluster."""
        mock_cluster = MagicMock()
        mock_settings = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
            "password": "password",
        }

        with patch(
            "providers.static.connect_to_couchbase_cluster",
            return_value=mock_cluster,
        ) as mock_connect:
            provider = StaticClusterProvider(settings=mock_settings)
            first = provider.get_cluster(MagicMock())
            second = provider.get_cluster(MagicMock())

        assert first is second is mock_cluster
        mock_connect.assert_called_once()

    def test_static_cluster_provider_propagates_connection_failure(self) -> None:
        """A failed connect raises and does not poison the cache."""
        mock_settings = {
            "connection_string": "couchbase://invalid",
            "username": "admin",
            "password": "wrong",
        }

        with patch(
            "providers.static.connect_to_couchbase_cluster",
            side_effect=Exception("Auth failed"),
        ):
            provider = StaticClusterProvider(settings=mock_settings)
            with pytest.raises(Exception, match="Auth failed"):
                provider.get_cluster(MagicMock())

        # Cache stayed empty so a subsequent attempt can retry.
        assert provider._cluster is None

    def test_static_cluster_provider_coalesces_concurrent_first_calls(self) -> None:
        """The threading.Lock in StaticClusterProvider must coalesce concurrent
        first-call attempts so we don't open multiple cluster connections when
        several tool handlers race to be the first caller.
        """
        mock_cluster = MagicMock()
        mock_settings = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
            "password": "password",
        }

        # Events let us hold the first connect attempt inside the lock so
        # the other threads actually contend on it.
        connect_started = threading.Event()
        connect_allowed = threading.Event()

        def slow_connect(*args, **kwargs):
            connect_started.set()
            # Block until the test releases us — guarantees that other
            # threads queue up behind the lock during this window.
            connect_allowed.wait(timeout=2.0)
            return mock_cluster

        with patch(
            "providers.static.connect_to_couchbase_cluster",
            side_effect=slow_connect,
        ) as mock_connect:
            provider = StaticClusterProvider(settings=mock_settings)

            results: list = []
            results_lock = threading.Lock()

            def worker():
                cluster = provider.get_cluster(MagicMock())
                with results_lock:
                    results.append(cluster)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()

            # Wait for the first thread to enter slow_connect, then release.
            assert connect_started.wait(timeout=2.0), (
                "no thread reached the connect callback"
            )
            connect_allowed.set()

            for t in threads:
                t.join(timeout=5.0)

        # Every thread saw the same cluster reference.
        assert len(results) == 5
        assert all(r is mock_cluster for r in results)
        # The crucial assertion: the lock coalesced the racers into one
        # actual connection attempt — without it this would be 5.
        mock_connect.assert_called_once()

    def test_static_cluster_provider_close_releases_cluster(self) -> None:
        """close() calls cluster.close() and clears the cache."""
        mock_cluster = MagicMock()
        mock_settings = {
            "connection_string": "couchbase://localhost",
            "username": "admin",
            "password": "password",
        }

        with patch(
            "providers.static.connect_to_couchbase_cluster",
            return_value=mock_cluster,
        ):
            provider = StaticClusterProvider(settings=mock_settings)
            provider.get_cluster(MagicMock())
            provider.close()

        mock_cluster.close.assert_called_once()
        assert provider._cluster is None


class TestFetchIndexesViaQueryService:
    """Unit tests for fetch_indexes_via_query_service."""

    _LET_CLAUSE = (
        "LET bid = IFMISSING(s.bucket_id, s.keyspace_id), "
        "sid = IFMISSING(s.scope_id, '_default'), "
        "kid = NVL2(s.bucket_id, s.keyspace_id, '_default')"
    )
    _BASE_WHERE = "s.namespace_id = 'default' AND s.`using` = 'gsi'"

    def test_no_filters(self) -> None:
        """With no filters, query should carry the namespace + GSI guards
        and the LET-based bucket/scope/collection normalization."""
        mock_ctx = MagicMock()
        expected_query = (
            "SELECT s.*, bid AS `bucket`, sid AS `scope`, kid AS `collection` "
            f"FROM system:indexes AS s {self._LET_CLAUSE} "
            f"WHERE {self._BASE_WHERE}"
        )

        with patch(
            "cb_mcp.tools.index.run_cluster_query",
            new_callable=MagicMock,
            return_value=[{"name": "idx1"}, {"name": "idx2"}],
        ) as mock_query:
            result = fetch_indexes_via_query_service(mock_ctx, None, None, None, None)

        mock_query.assert_called_once_with(
            mock_ctx, expected_query, named_parameters={}
        )
        assert len(result) == 2

    def test_raw_mode_selects_raw_source_rows(self) -> None:
        """return_raw_index_stats=True must SELECT RAW s — no injected
        bucket/scope/collection on the result rows."""
        mock_ctx = MagicMock()
        expected_query = (
            f"SELECT RAW s FROM system:indexes AS s {self._LET_CLAUSE} "
            f"WHERE {self._BASE_WHERE}"
        )

        with patch(
            "cb_mcp.tools.index.run_cluster_query",
            new_callable=MagicMock,
            return_value=[{"name": "idx1"}],
        ) as mock_query:
            fetch_indexes_via_query_service(
                mock_ctx, None, None, None, None, return_raw_index_stats=True
            )

        mock_query.assert_called_once_with(
            mock_ctx, expected_query, named_parameters={}
        )

    def test_all_filters(self) -> None:
        """All filters should apply against the normalized LET aliases so
        legacy indexes match by bucket symmetrically with modern ones."""
        mock_ctx = MagicMock()

        with patch(
            "cb_mcp.tools.index.run_cluster_query",
            new_callable=MagicMock,
            return_value=[{"name": "idx1"}],
        ) as mock_query:
            result = fetch_indexes_via_query_service(
                mock_ctx, "bucket", "scope", "collection", "idx1"
            )

        sent_query = mock_query.call_args[0][1]
        params = mock_query.call_args[1]["named_parameters"]
        assert params == {
            "bucket_id": "bucket",
            "scope_id": "scope",
            "keyspace_id": "collection",
            "index_name": "idx1",
        }
        # Verify filters are applied against LET aliases (not raw fields)
        # so legacy and modern indexes both match.
        assert "bid = $bucket_id" in sent_query
        assert "sid = $scope_id" in sent_query
        assert "kid = $keyspace_id" in sent_query
        assert "s.name = $index_name" in sent_query
        assert len(result) == 1

    def test_non_dict_rows_filtered(self) -> None:
        """Non-dict rows returned by the query should be dropped."""
        mock_ctx = MagicMock()

        with patch(
            "cb_mcp.tools.index.run_cluster_query",
            new_callable=MagicMock,
            return_value=[{"name": "idx1"}, "stray_string", 42, None],
        ):
            result = fetch_indexes_via_query_service(mock_ctx, None, None, None, None)

        assert result == [{"name": "idx1"}]


class TestResolveClusterMajorVersion:
    """Unit tests for resolve_cluster_major_version."""

    def test_dict_nodes(self) -> None:
        """Version detection with nodes represented as dicts."""
        mock_cluster = MagicMock()
        info = MagicMock()
        info.nodes = [
            {"version": "8.0.0-1928-enterprise"},
            {"version": "8.0.1-2000-enterprise"},
        ]
        mock_cluster.cluster_info.return_value = info

        result = resolve_cluster_major_version(mock_cluster)

        assert result == 8

    def test_object_nodes(self) -> None:
        """Version detection with nodes represented as objects with attributes."""
        mock_cluster = MagicMock()
        info = MagicMock()
        node = MagicMock()
        node.version = "7.6.0"
        info.nodes = [node]
        mock_cluster.cluster_info.return_value = info

        result = resolve_cluster_major_version(mock_cluster)

        assert result == 7

    def test_mixed_versions_returns_min(self) -> None:
        """Mixed-version cluster returns the minimum major version."""
        mock_cluster = MagicMock()
        info = MagicMock()
        info.nodes = [
            {"version": "8.0.0-enterprise"},
            {"version": "7.6.11-enterprise"},
        ]
        mock_cluster.cluster_info.return_value = info

        result = resolve_cluster_major_version(mock_cluster)

        assert result == 7

    def test_cluster_info_exception_propagates(self) -> None:
        """If cluster_info() throws, the exception should propagate."""
        mock_cluster = MagicMock()
        mock_cluster.cluster_info.side_effect = Exception("connection refused")

        with pytest.raises(Exception, match="connection refused"):
            resolve_cluster_major_version(mock_cluster)

    def test_empty_nodes_raises(self) -> None:
        """If cluster reports no nodes, raise RuntimeError."""
        mock_cluster = MagicMock()
        info = MagicMock()
        info.nodes = []
        mock_cluster.cluster_info.return_value = info

        with pytest.raises(RuntimeError, match="no nodes"):
            resolve_cluster_major_version(mock_cluster)


class TestListIndexesVersionRouting:
    """Integration-level tests verifying list_indexes routes to the correct path."""

    def test_version_8_uses_query_service(self) -> None:
        """Cluster version >= 8 should use system:indexes, not REST API."""
        mock_ctx = MagicMock()
        mock_cluster = MagicMock()
        info = MagicMock()
        info.nodes = [{"version": "8.0.0-enterprise"}]
        mock_cluster.cluster_info.return_value = info

        with (
            patch(
                "cb_mcp.tools.index.get_settings",
                return_value={
                    "connection_string": "couchbase://localhost",
                    "username": "u",
                    "password": "p",
                },
            ),
            patch(
                "cb_mcp.tools.index.get_cluster_connection",
                new_callable=MagicMock,
                return_value=mock_cluster,
            ),
            patch(
                "cb_mcp.tools.index.run_cluster_query",
                new_callable=MagicMock,
                return_value=[
                    {
                        "name": "idx1",
                        "bucket_id": "b",
                        "scope_id": "s",
                        "keyspace_id": "c",
                        # bucket/scope/collection are injected by the LET
                        # clause in the production SQL; the mock simulates
                        # what the query service actually returns.
                        "bucket": "b",
                        "scope": "s",
                        "collection": "c",
                        "state": "online",
                        "metadata": {
                            "definition": "CREATE INDEX idx1 ON b.s.c(x)",
                            "last_scan_time": None,
                        },
                    }
                ],
            ) as mock_query,
            patch(
                "cb_mcp.tools.index.fetch_indexes_from_rest_api", new_callable=MagicMock
            ) as mock_rest,
        ):
            result = list_indexes(mock_ctx)

        mock_query.assert_called_once()
        mock_rest.assert_not_called()
        assert len(result) == 1
        assert result[0]["name"] == "idx1"

    def test_version_7_uses_rest_api(self) -> None:
        """Cluster version < 8 should fall back to the REST API."""
        mock_ctx = MagicMock()
        mock_cluster = MagicMock()
        info = MagicMock()
        info.nodes = [{"version": "7.6.11-enterprise"}]
        mock_cluster.cluster_info.return_value = info

        with (
            patch(
                "cb_mcp.tools.index.get_settings",
                return_value={
                    "connection_string": "couchbase://localhost",
                    "username": "u",
                    "password": "p",
                },
            ),
            patch(
                "cb_mcp.tools.index.get_cluster_connection",
                new_callable=MagicMock,
                return_value=mock_cluster,
            ),
            patch(
                "cb_mcp.tools.index.run_cluster_query", new_callable=MagicMock
            ) as mock_query,
            patch(
                "cb_mcp.tools.index.fetch_indexes_from_rest_api",
                new_callable=MagicMock,
                return_value=[
                    {
                        "name": "idx1",
                        "definition": "CREATE INDEX idx1 ON b.s.c(x)",
                        "status": "Ready",
                        "bucket": "b",
                        "scope": "s",
                        "collection": "c",
                        "isPrimary": False,
                        "lastScanTime": "NA",
                    }
                ],
            ) as mock_rest,
        ):
            result = list_indexes(mock_ctx)

        mock_query.assert_not_called()
        mock_rest.assert_called_once()
        assert len(result) == 1
        assert result[0]["name"] == "idx1"


class TestExtractHostsFallback:
    """_extract_hosts_from_connection_string fallback when urlparse can't
    populate netloc (e.g., scheme-less or oddly formatted inputs)."""

    def test_bare_host_no_scheme(self) -> None:
        """A bare host string with no scheme has no netloc; the fallback
        path should still return the host."""
        # urlparse treats "host.example.com" as a path, not a netloc.
        hosts = _extract_hosts_from_connection_string("host.example.com")
        assert hosts == ["host.example.com"]

    def test_bare_host_with_port_no_scheme(self) -> None:
        """Bare host:port (no scheme) should still strip the port."""
        hosts = _extract_hosts_from_connection_string("host.example.com:8091")
        assert hosts == ["host.example.com"]

    def test_bare_multiple_hosts_no_scheme(self) -> None:
        """Comma-separated bare hosts should be split apart."""
        hosts = _extract_hosts_from_connection_string("h1,h2,h3")
        assert hosts == ["h1", "h2", "h3"]


class TestDetermineSSLCapella:
    """_determine_ssl_verification Capella branch."""

    def test_capella_returns_bundled_ca_when_present(self) -> None:
        """For *.cloud.couchbase.com hosts, the Capella CA bundle should
        be returned when the file is present on disk."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com"

        with (
            patch(
                "cb_mcp.utils.index_utils._get_capella_root_ca_path",
                return_value="/fake/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.index_utils.os.path.exists",
                return_value=True,
            ),
        ):
            result = _determine_ssl_verification(capella_conn, None)

        assert result == "/fake/capella_root_ca.pem"

    def test_capella_falls_back_to_system_bundle_when_missing(self) -> None:
        """If the bundled Capella CA cannot be located on disk, fall back
        to the system CA bundle (verify=True) so connections still work."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com"

        with (
            patch(
                "cb_mcp.utils.index_utils._get_capella_root_ca_path",
                return_value="/missing/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.index_utils.os.path.exists",
                return_value=False,
            ),
        ):
            result = _determine_ssl_verification(capella_conn, None)

        assert result is True

    def test_capella_ignores_user_ca_path(self) -> None:
        """A Capella host should pick the bundled Capella CA over a
        user-supplied CA path — Capella certs are pinned."""
        capella_conn = "couchbases://cb.abc123.cloud.couchbase.com"

        with (
            patch(
                "cb_mcp.utils.index_utils._get_capella_root_ca_path",
                return_value="/fake/capella_root_ca.pem",
            ),
            patch(
                "cb_mcp.utils.index_utils.os.path.exists",
                return_value=True,
            ),
        ):
            result = _determine_ssl_verification(capella_conn, "/user/supplied/ca.pem")

        assert result == "/fake/capella_root_ca.pem"


class TestGetCapellaRootCAPath:
    """_get_capella_root_ca_path resource resolution."""

    def test_uses_importlib_resources_when_available(self) -> None:
        """The installed-package path uses importlib.resources.files()."""
        fake_path = MagicMock()
        fake_path.__str__ = (
            lambda self: "/site-packages/cb_mcp/certs/capella_root_ca.pem"
        )

        with patch("cb_mcp.utils.index_utils.files") as mock_files:
            mock_files.return_value.joinpath.return_value = fake_path
            result = _get_capella_root_ca_path()

        assert result == "/site-packages/cb_mcp/certs/capella_root_ca.pem"
        mock_files.assert_called_once_with("cb_mcp.certs")

    def test_falls_back_to_dev_path_when_importlib_fails(self) -> None:
        """When importlib.resources raises, the fallback returns a path
        derived from this module's location and logs a fallback message
        when the file exists."""
        with (
            patch(
                "cb_mcp.utils.index_utils.files",
                side_effect=FileNotFoundError("no resource"),
            ),
            patch(
                "cb_mcp.utils.index_utils.os.path.exists",
                return_value=True,
            ),
        ):
            result = _get_capella_root_ca_path()

        # Path must end with the expected filename and the certs/ dir.
        assert result.endswith("certs/capella_root_ca.pem")

    def test_returns_fallback_path_even_when_file_missing(self) -> None:
        """If both the resource lookup AND the fallback file are missing,
        the fallback path is still returned (with a warning logged)."""
        with (
            patch(
                "cb_mcp.utils.index_utils.files",
                side_effect=ImportError("no module"),
            ),
            patch(
                "cb_mcp.utils.index_utils.os.path.exists",
                return_value=False,
            ),
        ):
            result = _get_capella_root_ca_path()

        assert result.endswith("certs/capella_root_ca.pem")


class TestFetchIndexesFromRestApi:
    """Unit tests for fetch_indexes_from_rest_api (mocked httpx)."""

    @staticmethod
    def _ok_response(payload: dict | None = None) -> MagicMock:
        """Build a mock httpx.Response that mimics .raise_for_status / .json."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = payload or {"status": []}
        return response

    def _patch_client(self, get_side_effect):
        """Patch httpx.Client so .get() returns the supplied side effect."""
        mock_client_cm = MagicMock()
        mock_client = MagicMock()
        mock_client.get = MagicMock(side_effect=get_side_effect)
        mock_client_cm.__enter__.return_value = mock_client
        mock_client_cm.__exit__.return_value = False
        return patch(
            "cb_mcp.utils.index_utils.httpx.Client", return_value=mock_client_cm
        ), mock_client

    def test_single_host_success(self) -> None:
        """A single-host success path should return indexes from response.status."""
        indexes = [{"indexName": "idx1", "bucket": "b", "definition": "CREATE..."}]
        client_patch, mock_client = self._patch_client(
            [self._ok_response({"status": indexes})]
        )

        with client_patch:
            result = fetch_indexes_from_rest_api(
                "couchbase://host1",
                "u",
                "p",
            )

        assert result == indexes
        mock_client.get.assert_called_once()
        # URL must point at HTTP (non-TLS) on the cleartext index-status port.
        called_url = mock_client.get.call_args[0][0]
        assert called_url == "http://host1:9102/getIndexStatus"

    def test_tls_uses_https_and_secure_port(self) -> None:
        """TLS connection strings should select https + the secure port (19102)."""
        client_patch, mock_client = self._patch_client(
            [self._ok_response({"status": []})]
        )

        with client_patch:
            fetch_indexes_from_rest_api(
                "couchbases://host1",
                "u",
                "p",
            )

        called_url = mock_client.get.call_args[0][0]
        assert called_url == "https://host1:19102/getIndexStatus"

    def test_filter_params_forwarded(self) -> None:
        """Bucket/scope/collection/index filters must be sent as query params."""
        client_patch, mock_client = self._patch_client(
            [self._ok_response({"status": []})]
        )

        with client_patch:
            fetch_indexes_from_rest_api(
                "couchbase://host1",
                "u",
                "p",
                bucket_name="b",
                scope_name="s",
                collection_name="c",
                index_name="idx",
            )

        sent_params = mock_client.get.call_args[1]["params"]
        assert sent_params == {
            "bucket": "b",
            "scope": "s",
            "collection": "c",
            "index": "idx",
        }

    def test_basic_auth_forwarded(self) -> None:
        """Username/password must be forwarded as HTTP basic auth."""
        client_patch, mock_client = self._patch_client(
            [self._ok_response({"status": []})]
        )

        with client_patch:
            fetch_indexes_from_rest_api(
                "couchbase://host1",
                "admin",
                "secret",
            )

        assert mock_client.get.call_args[1]["auth"] == ("admin", "secret")

    def test_multi_host_failover(self) -> None:
        """If the first host fails, the second one should be tried."""
        first_error = httpx.ConnectError("connection refused")
        success_response = self._ok_response({"status": [{"indexName": "idx1"}]})

        client_patch, mock_client = self._patch_client([first_error, success_response])

        with client_patch:
            result = fetch_indexes_from_rest_api(
                "couchbase://host1,host2",
                "u",
                "p",
            )

        assert len(result) == 1
        # Both hosts attempted in order.
        assert mock_client.get.call_count == 2
        urls = [call.args[0] for call in mock_client.get.call_args_list]
        assert "host1" in urls[0]
        assert "host2" in urls[1]

    def test_all_hosts_fail_raises_runtime_error(self) -> None:
        """When every host raises, the helper must raise RuntimeError with
        the list of attempted hosts in the message."""
        error = httpx.ConnectError("connection refused")
        client_patch, _ = self._patch_client([error, error])

        with client_patch, pytest.raises(RuntimeError, match="host1.*host2"):
            fetch_indexes_from_rest_api(
                "couchbase://host1,host2",
                "u",
                "p",
            )

    def test_unexpected_exception_continues_to_next_host(self) -> None:
        """Non-HTTPError exceptions on one host should not abort failover —
        the next host should still be tried."""
        unexpected = ValueError("weird parser bug")
        success = self._ok_response({"status": []})

        client_patch, mock_client = self._patch_client([unexpected, success])

        with client_patch:
            result = fetch_indexes_from_rest_api(
                "couchbase://host1,host2",
                "u",
                "p",
            )

        assert result == []
        assert mock_client.get.call_count == 2

    def test_http_error_status_continues_to_next_host(self) -> None:
        """raise_for_status() failures (e.g., 500) should be treated as a
        host failure and not stop the failover loop."""
        bad_response = MagicMock()
        bad_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        success = self._ok_response({"status": []})

        client_patch, mock_client = self._patch_client([bad_response, success])

        with client_patch:
            result = fetch_indexes_from_rest_api(
                "couchbase://host1,host2",
                "u",
                "p",
            )

        assert result == []
        assert mock_client.get.call_count == 2
