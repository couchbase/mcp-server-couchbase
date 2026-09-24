"""Unit tests for get_index_stats and its Index Service REST helpers.

Covers:
- build_stats_path granularity selection and dotted-segment quoting.
- parse_index_stats_key for both key shapes plus unrecognised input.
- get_index_stats response reshaping (indexer split out, node reported).
- get_index_stats filter-hierarchy validation and error propagation.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest

from cb_mcp.tools.operational.index import get_index_stats
from cb_mcp.utils.operational.index_utils import (
    _parse_index_nodes,
    build_stats_path,
    format_stats_keyspace_part,
    parse_index_stats_key,
    resolve_management_endpoints,
)

SETTINGS = {
    "connection_string": "couchbase://localhost",
    "username": "Administrator",
    "password": "password",
    "ca_cert_path": None,
}

# Shape of a real /api/v1/stats response: the node-level "indexer" block sits at
# the same level as the per-index blocks, which use two different key shapes.
RAW_STATS = {
    "indexer": {"indexer_state": "Active", "memory_quota": 1024},
    "travel-sample:def_city": {"frag_percent": 12, "num_requests": 0},
    "travel-sample:inventory:airport:idx_airport_city": {
        "frag_percent": 83,
        "num_requests": 1,
    },
}


class TestBuildStatsPath:
    """Path construction picks the endpoint matching the supplied filters."""

    @pytest.mark.parametrize(
        "args,expected",
        [
            ((None, None, None, None), ""),
            (("travel-sample", None, None, None), "/travel-sample"),
            # A scope without a collection is a valid partial keyspace and must
            # narrow the result, not fall back to the whole bucket.
            (
                ("travel-sample", "inventory", None, None),
                "/travel-sample.inventory",
            ),
            (
                ("travel-sample", "inventory", "airport", None),
                "/travel-sample.inventory.airport",
            ),
            (
                ("travel-sample", "inventory", "airport", "idx_airport_city"),
                "/travel-sample.inventory.airport/idx_airport_city",
            ),
            (("travel-sample", None, None, "def_city"), "/travel-sample/def_city"),
        ],
    )
    def test_granularity(self, args, expected):
        assert build_stats_path(*args) == expected

    def test_dotted_segment_is_quoted(self):
        """Only segments containing a dot get backticks."""
        assert (
            build_stats_path("my.bucket", "inventory", "airport", None)
            == "/`my.bucket`.inventory.airport"
        )

    def test_plain_segment_is_not_quoted(self):
        """The API 404s on backticked scope/collection names, so quote sparingly."""
        assert format_stats_keyspace_part("travel-sample") == "travel-sample"
        assert format_stats_keyspace_part("my.bucket") == "`my.bucket`"

    def test_backticks_are_left_literal(self):
        """The server rejects the percent-encoded %60 form, so ` must survive."""
        assert "%60" not in format_stats_keyspace_part("my.bucket")


class TestKeyspacePathTraversal:
    """Segments are URL-encoded so a crafted name cannot leave /api/v1/stats/.

    Unencoded, a bucket_name of ``x/../../../stats`` normalises to
    ``/api/stats`` — a different, live endpoint.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "x/../../../stats",
            "x/../../../../getIndexStatus",
            "../../etc/passwd",
        ],
    )
    def test_traversal_cannot_escape_the_stats_path(self, hostile):
        path = build_stats_path(hostile, None, None, None)
        # ".." is harmless once the separators around it are encoded, so the
        # invariant is that no unencoded "/" survives to be normalised away.
        assert "/" not in path.lstrip("/"), f"path separator survived in {path!r}"
        assert "%2F" in path

    def test_traversal_blocked_in_every_segment(self):
        path = build_stats_path(
            "travel-sample", "inventory", "airport", "x/../../../stats"
        )
        assert path.startswith("/travel-sample.inventory.airport/")
        assert "%2F" in path

    @pytest.mark.parametrize(
        "hostile,encoded",
        [
            ("a\r\nX-Evil: 1", "%0D%0A"),
            ("a\x00", "%00"),
            ("a?b", "%3F"),
            ("a#b", "%23"),
        ],
    )
    def test_control_and_delimiter_chars_are_encoded(self, hostile, encoded):
        assert encoded in format_stats_keyspace_part(hostile)


class TestParseIndexStatsKey:
    """Keys are colon-joined with either two or four segments."""

    def test_default_keyspace_key(self):
        assert parse_index_stats_key("travel-sample:def_city") == {
            "bucket": "travel-sample",
            "scope": "_default",
            "collection": "_default",
            "name": "def_city",
        }

    def test_named_keyspace_key(self):
        assert parse_index_stats_key(
            "travel-sample:inventory:airport:idx_airport_city"
        ) == {
            "bucket": "travel-sample",
            "scope": "inventory",
            "collection": "airport",
            "name": "idx_airport_city",
        }

    def test_unrecognised_shape_is_preserved(self):
        """An unexpected split is surfaced raw rather than mislabelled."""
        assert parse_index_stats_key("weird:a:b") == {"name": "weird:a:b"}


class TestGetIndexStats:
    def _call(self, raw=RAW_STATS, **kwargs):
        with (
            patch("cb_mcp.tools.operational.index.get_settings", return_value=SETTINGS),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch("cb_mcp.tools.operational.index.get_cluster_connection"),
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api",
                return_value=({"node1:8091": raw}, [], []),
            ) as fetch,
        ):
            return get_index_stats(SimpleNamespace(), **kwargs), fetch

    def test_splits_indexer_from_indexes(self):
        result, _ = self._call()
        node = result["nodes"]["node1:8091"]
        assert node["indexer"] == {"indexer_state": "Active", "memory_quota": 1024}
        # The indexer block must not leak into the per-index list.
        assert len(node["indexes"]) == 2
        assert "indexer" not in {i["name"] for i in node["indexes"]}

    def test_indexes_carry_parsed_keyspace_and_stats(self):
        result, _ = self._call()
        node = result["nodes"]["node1:8091"]
        by_name = {i["name"]: i for i in node["indexes"]}
        airport = by_name["idx_airport_city"]
        assert airport["bucket"] == "travel-sample"
        assert airport["scope"] == "inventory"
        assert airport["collection"] == "airport"
        assert airport["stats"]["frag_percent"] == 83

    def test_indexer_is_none_when_filtered(self):
        """Keyspace-scoped endpoints omit the indexer block."""
        raw = {"travel-sample:def_city": {"frag_percent": 12}}
        result, _ = self._call(raw=raw, bucket_name="travel-sample")
        node = result["nodes"]["node1:8091"]
        assert node["indexer"] is None
        assert len(node["indexes"]) == 1

    def test_keeps_nodes_separate(self):
        """A partitioned index appears per node; its stats are not merged."""
        with (
            patch("cb_mcp.tools.operational.index.get_settings", return_value=SETTINGS),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch("cb_mcp.tools.operational.index.get_cluster_connection"),
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api",
                return_value=(
                    {
                        "a:8091": {"test:idx_p": {"items_count": 4}},
                        "b:8091": {"test:idx_p": {"items_count": 4}},
                    },
                    [],
                    [],
                ),
            ),
        ):
            result = get_index_stats(SimpleNamespace())
        assert sorted(result["nodes"]) == ["a:8091", "b:8091"]
        for node in result["nodes"].values():
            assert node["indexes"][0]["stats"]["items_count"] == 4

    def test_reports_unreachable_nodes(self):
        """A partial answer must say which nodes were missed."""
        failures = [{"node": "b:8091", "error": "timeout"}]
        with (
            patch("cb_mcp.tools.operational.index.get_settings", return_value=SETTINGS),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch("cb_mcp.tools.operational.index.get_cluster_connection"),
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api",
                return_value=({"a:8091": RAW_STATS}, [], failures),
            ),
        ):
            result = get_index_stats(SimpleNamespace())
        assert result["nodes_failed"] == failures
        assert list(result["nodes"]) == ["a:8091"]

    def test_skip_empty_is_passed_through(self):
        _, fetch = self._call(skip_empty=True)
        assert fetch.call_args.kwargs["skip_empty"] is True

    def test_skip_empty_defaults_to_false(self):
        """Left off, zero-valued stats like num_requests: 0 stay visible."""
        _, fetch = self._call()
        assert fetch.call_args.kwargs["skip_empty"] is False

    def test_rejects_non_hierarchical_filters(self):
        with pytest.raises(ValueError, match="bucket_name is required"):
            self._call(scope_name="inventory")

    def test_propagates_fetch_failure(self):
        with (
            patch("cb_mcp.tools.operational.index.get_settings", return_value=SETTINGS),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch("cb_mcp.tools.operational.index.get_cluster_connection"),
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api",
                side_effect=RuntimeError("all hosts failed"),
            ),
            pytest.raises(RuntimeError, match="all hosts failed"),
        ):
            get_index_stats(SimpleNamespace())


class TestDiscoverIndexNodes:
    """Only nodes running the index service are queried for statistics."""

    PAYLOAD: ClassVar[dict] = {
        "nodesExt": [
            {"hostname": "10.0.0.1", "services": {"mgmt": 8091, "kv": 11210}},
            {
                "hostname": "10.0.0.2",
                "services": {"mgmt": 8091, "indexHttp": 9102, "indexHttps": 19102},
            },
        ]
    }

    def test_skips_nodes_without_the_index_service(self):
        """A kv-only node has nothing on the index port, so querying it would
        only manufacture failures that look like outages."""
        nodes = _parse_index_nodes(self.PAYLOAD, "http", "indexHttp", "10.0.0.1")
        assert [n["node"] for n in nodes] == ["10.0.0.2:8091"]

    def test_node_is_keyed_by_management_port(self):
        """getIndexStatus, get_cluster_metrics and the UI all name nodes this way."""
        nodes = _parse_index_nodes(self.PAYLOAD, "http", "indexHttp", "10.0.0.1")
        assert nodes[0]["node"] == "10.0.0.2:8091"

    def test_stats_url_uses_the_reported_index_port(self):
        nodes = _parse_index_nodes(self.PAYLOAD, "http", "indexHttp", "10.0.0.1")
        assert nodes[0]["stats_urls"] == ["http://10.0.0.2:9102/api/v1/stats"]

    def test_tls_selects_the_https_index_port(self):
        nodes = _parse_index_nodes(self.PAYLOAD, "https", "indexHttps", "10.0.0.1")
        assert nodes[0]["stats_urls"] == ["https://10.0.0.2:19102/api/v1/stats"]

    def test_missing_hostname_falls_back_to_loopback(self):
        """A single-node cluster reports no hostname for itself."""
        payload = {"nodesExt": [{"services": {"mgmt": 8091, "indexHttp": 9102}}]}
        nodes = _parse_index_nodes(payload, "http", "indexHttp", "127.0.0.1")
        assert nodes[0]["stats_urls"] == ["http://127.0.0.1:9102/api/v1/stats"]

    def test_no_index_nodes_yields_empty_list(self):
        payload = {"nodesExt": [{"hostname": "10.0.0.1", "services": {"kv": 11210}}]}
        assert _parse_index_nodes(payload, "http", "indexHttp", "127.0.0.1") == []


class TestAlternateAddresses:
    """nodeServices reports internal hostnames; the client picks the network.

    A port-mapped, NAT'd or Capella-style cluster is only reachable at its
    advertised external address, so the internal hostname alone is not enough.
    """

    PAYLOAD: ClassVar[dict] = {
        "nodesExt": [
            {
                "hostname": "172.20.0.3",
                "services": {"mgmt": 8091, "indexHttp": 9102},
                "alternateAddresses": {
                    "external": {
                        "hostname": "127.0.0.1",
                        "ports": {"mgmt": 19291, "indexHttp": 19202},
                    }
                },
            }
        ]
    }

    def test_external_preferred_when_reached_externally(self):
        """Reaching nodeServices at an advertised external address is evidence
        that external addresses work from here."""
        nodes = _parse_index_nodes(self.PAYLOAD, "http", "indexHttp", "127.0.0.1")
        assert nodes[0]["node"] == "127.0.0.1:19291"
        assert nodes[0]["stats_urls"][0] == "http://127.0.0.1:19202/api/v1/stats"

    def test_internal_preferred_when_reached_internally(self):
        nodes = _parse_index_nodes(self.PAYLOAD, "http", "indexHttp", "172.20.0.3")
        assert nodes[0]["node"] == "172.20.0.3:8091"
        assert nodes[0]["stats_urls"][0] == "http://172.20.0.3:9102/api/v1/stats"

    def test_other_address_is_kept_as_a_fallback(self):
        """A node may advertise only some services externally, so the
        non-preferred address is still worth trying."""
        nodes = _parse_index_nodes(self.PAYLOAD, "http", "indexHttp", "127.0.0.1")
        assert nodes[0]["stats_urls"] == [
            "http://127.0.0.1:19202/api/v1/stats",
            "http://172.20.0.3:9102/api/v1/stats",
        ]

    def test_node_with_external_mgmt_but_no_external_index_port(self):
        """Partial alternate addresses fall back to the internal index port."""
        payload = {
            "nodesExt": [
                {
                    "hostname": "172.20.0.3",
                    "services": {"mgmt": 8091, "indexHttp": 9102},
                    "alternateAddresses": {
                        "external": {"hostname": "127.0.0.1", "ports": {"mgmt": 19291}}
                    },
                }
            ]
        }
        nodes = _parse_index_nodes(payload, "http", "indexHttp", "127.0.0.1")
        assert nodes[0]["stats_urls"] == ["http://172.20.0.3:9102/api/v1/stats"]


class TestResolveManagementEndpoints:
    """The management port comes from the SDK, not from a constant.

    The connection string carries a KV port (the SDK bootstraps over KV), so a
    port-mapped cluster serves management somewhere the default cannot reach.
    """

    def test_uses_sdk_reported_endpoints(self):
        """ping, not diagnostics: a freshly opened cluster has no management
        socket yet, so diagnostics would report none and fall back to 8091."""
        cluster = SimpleNamespace(
            ping=lambda *a, **k: SimpleNamespace(
                as_json=lambda: json.dumps(
                    {
                        "services": {
                            "management": [
                                {"remote": "127.0.0.1:19191"},
                                {"remote": "127.0.0.1:19291"},
                            ]
                        }
                    }
                )
            )
        )
        assert resolve_management_endpoints(cluster, "couchbase://127.0.0.1:19210") == [
            "127.0.0.1:19191",
            "127.0.0.1:19291",
        ]

    def test_falls_back_to_default_port_without_diagnostics(self):
        cluster = SimpleNamespace(
            ping=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no"))
        )
        assert resolve_management_endpoints(cluster, "couchbase://host1,host2") == [
            "host1:8091",
            "host2:8091",
        ]

    def test_tls_fallback_uses_the_tls_management_port(self):
        cluster = SimpleNamespace(
            ping=lambda *a, **k: SimpleNamespace(as_json=lambda: json.dumps({}))
        )
        assert resolve_management_endpoints(cluster, "couchbases://host1") == [
            "host1:18091"
        ]


class TestNotHostedReporting:
    """A 404 from a reachable node is not the same as an unreachable node.

    The stats API returns 404 both for an index this node does not hold and for
    a name that does not exist anywhere, so the two are told apart by whether
    any other node answered.
    """

    def _call(self, per_node, not_hosted, failures):
        with (
            patch("cb_mcp.tools.operational.index.get_settings", return_value=SETTINGS),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch("cb_mcp.tools.operational.index.get_cluster_connection"),
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api",
                return_value=(per_node, not_hosted, failures),
            ),
        ):
            return get_index_stats(SimpleNamespace())

    def test_not_hosted_is_separate_from_failed(self):
        """An index lives only on the nodes it was placed on, so a node without
        it is a normal result, not an outage."""
        result = self._call({"a:8091": RAW_STATS}, ["b:8091"], [])
        assert result["nodes_without_index"] == ["b:8091"]
        assert result["nodes_failed"] == []
        assert list(result["nodes"]) == ["a:8091"]

    def test_both_categories_are_reported(self):
        failures = [{"node": "c:8091", "error": "timeout"}]
        result = self._call({"a:8091": RAW_STATS}, ["b:8091"], failures)
        assert result["nodes_without_index"] == ["b:8091"]
        assert result["nodes_failed"] == failures
