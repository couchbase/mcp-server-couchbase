"""Unit tests for get_index_stats and its Index Service REST helpers.

Covers:
- build_stats_path granularity selection and dotted-segment quoting.
- parse_index_stats_key for both key shapes plus unrecognised input.
- get_index_stats response reshaping (indexer split out, node reported).
- get_index_stats filter-hierarchy validation and error propagation.
- get_index_stats rejects Capella connections up front, before any cluster
  connection or REST call.
- resolve_management_endpoints / resolve_query_endpoints: the SDK-reported
  path (dedup, unusable entries) and the connection-string fallback (scheme
  to port, IPv6 bracketing, host-less strings).
- fetch_index_stats_from_rest_api's fan-out across several index nodes: one
  node per request, results kept apart, and a slow or absent node reported
  without discarding the rest.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.operational.index import get_index_stats
from cb_mcp.utils.operational.index_utils import (
    _parse_index_nodes,
    build_stats_path,
    fetch_index_stats_from_rest_api,
    format_stats_keyspace_part,
    parse_index_stats_key,
    resolve_management_endpoints,
    resolve_query_endpoints,
)

SETTINGS = {
    "connection_string": "couchbase://localhost",
    "username": "Administrator",
    "password": "password",
    "ca_cert_path": None,
}

CAPELLA_SETTINGS = {
    **SETTINGS,
    "connection_string": "couchbases://cb.abc.cloud.couchbase.com",
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


class TestFanOutAcrossIndexNodes:
    """fetch_index_stats_from_rest_api queries every index node, not one.

    ``/api/v1/stats`` is served by an individual indexer and reports only the
    indexes that node holds, so a single call never describes the cluster --
    an index on another node is simply absent, with no error to notice. The
    fan-out is what makes the answer cluster-wide, and it is the part a
    single-node setup cannot exercise.

    ``TestGetIndexStats`` above covers the reshaping that happens *after*
    this, with the whole fetch stubbed out; these tests drive the fetch
    itself, stubbing only node discovery and the HTTP client, so no cluster
    is needed.
    """

    @staticmethod
    def _node(name: str, *urls: str) -> dict:
        """One discovered index node: its key, and the addresses to try in order."""
        return {"node": name, "stats_urls": list(urls)}

    @staticmethod
    def _response(status: int = 200, payload: dict | None = None) -> MagicMock:
        """A mock httpx.Response that mimics .status_code/.raise_for_status/.json."""
        response = MagicMock()
        response.status_code = status
        response.json.return_value = {} if payload is None else payload
        if status >= 400:
            response.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
        else:
            response.raise_for_status.return_value = None
        return response

    def _fetch(self, nodes, get_side_effect, **kwargs):
        """Run the fan-out over *nodes* with httpx.Client.get stubbed.

        Returns ``(result, urls_requested)`` so a test can assert on what came
        back *and* on which addresses were actually called -- the ordering of
        a node's candidate addresses is only observable through the latter.
        """
        requested: list[str] = []

        def get(url, **_kwargs):
            requested.append(url)
            outcome = get_side_effect(url)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client = MagicMock()
        client.get = MagicMock(side_effect=get)
        client_cm = MagicMock()
        client_cm.__enter__.return_value = client
        client_cm.__exit__.return_value = False

        with (
            patch(
                "cb_mcp.utils.operational.index_utils.discover_index_nodes",
                return_value=nodes,
            ),
            patch(
                "cb_mcp.utils.operational.index_utils.httpx.Client",
                return_value=client_cm,
            ),
        ):
            result = fetch_index_stats_from_rest_api(
                SimpleNamespace(),
                "couchbase://a",
                "Administrator",
                "password",
                **kwargs,
            )
        return result, requested

    def test_every_node_is_queried_and_kept_apart(self):
        """Three nodes, three requests, three separate entries.

        A partitioned index reports only the partitions stored locally, so
        merging the bodies would double-count; each node's raw body is kept
        under its own key.
        """
        nodes = [
            self._node("a:8091", "http://a:9102/api/v1/stats"),
            self._node("b:8091", "http://b:9102/api/v1/stats"),
            self._node("c:8091", "http://c:9102/api/v1/stats"),
        ]
        bodies = {
            "http://a:9102/api/v1/stats": {"test:idx": {"items_count": 1}},
            "http://b:9102/api/v1/stats": {"test:idx": {"items_count": 2}},
            "http://c:9102/api/v1/stats": {"test:idx": {"items_count": 3}},
        }
        (per_node, not_hosted, failures), requested = self._fetch(
            nodes, lambda url: self._response(payload=bodies[url])
        )

        assert len(requested) == 3
        assert sorted(per_node) == ["a:8091", "b:8091", "c:8091"]
        assert [per_node[n]["test:idx"]["items_count"] for n in sorted(per_node)] == [
            1,
            2,
            3,
        ]
        assert not_hosted == []
        assert failures == []

    def test_one_unreachable_node_does_not_discard_the_others(self):
        """A partial answer is still an answer, and it names what was missed.

        Raising here would turn one flaky indexer into no stats at all, which
        is the opposite of what querying every node is for.
        """
        nodes = [
            self._node("a:8091", "http://a:9102/api/v1/stats"),
            self._node("b:8091", "http://b:9102/api/v1/stats"),
            self._node("c:8091", "http://c:9102/api/v1/stats"),
        ]

        def get(url):
            if "b:" in url:
                return RuntimeError("connection refused")
            return self._response(payload={"test:idx": {"items_count": 7}})

        (per_node, not_hosted, failures), _ = self._fetch(nodes, get)

        assert sorted(per_node) == ["a:8091", "c:8091"]
        assert not_hosted == []
        assert [f["node"] for f in failures] == ["b:8091"]
        assert "connection refused" in failures[0]["error"]

    def test_a_404_marks_the_node_not_hosted_rather_than_failed(self):
        """The two partial-answer categories are not the same thing.

        A 404 is the indexer answering that it does not hold this keyspace --
        a definitive reply. Folding it in with transport failures would report
        a healthy node as unreachable.
        """
        nodes = [
            self._node("a:8091", "http://a:9102/api/v1/stats"),
            self._node("b:8091", "http://b:9102/api/v1/stats"),
        ]

        def get(url):
            if "b:" in url:
                return self._response(status=404)
            return self._response(payload={"test:idx": {"items_count": 7}})

        (per_node, not_hosted, failures), _ = self._fetch(nodes, get)

        assert sorted(per_node) == ["a:8091"]
        assert not_hosted == ["b:8091"]
        assert failures == []

    def test_a_404_stops_that_node_without_trying_its_other_address(self):
        """A definitive "not here" ends the node; the alternate address cannot help.

        Both addresses reach the same indexer, so retrying could only replace
        a clear 404 with a connection error against a second address.
        """
        nodes = [
            self._node(
                "a:8091",
                "http://external:9102/api/v1/stats",
                "http://internal:9102/api/v1/stats",
            ),
            self._node("b:8091", "http://b:9102/api/v1/stats"),
        ]

        def get(url):
            if "external" in url:
                return self._response(status=404)
            return self._response(payload={"test:idx": {"items_count": 7}})

        (_per_node, not_hosted, failures), requested = self._fetch(nodes, get)

        assert "http://internal:9102/api/v1/stats" not in requested
        assert not_hosted == ["a:8091"]
        assert failures == []

    def test_a_node_falls_back_to_its_second_address(self):
        """A transport failure, unlike a 404, is worth retrying elsewhere.

        A node can advertise an external and an internal address; only one may
        be routable from here, so the first failing must not condemn the node.
        """
        nodes = [
            self._node(
                "a:8091",
                "http://external:9102/api/v1/stats",
                "http://internal:9102/api/v1/stats",
            )
        ]

        def get(url):
            if "external" in url:
                return RuntimeError("no route to host")
            return self._response(payload={"test:idx": {"items_count": 7}})

        (per_node, _not_hosted, failures), requested = self._fetch(nodes, get)

        assert requested == [
            "http://external:9102/api/v1/stats",
            "http://internal:9102/api/v1/stats",
        ]
        assert per_node["a:8091"]["test:idx"]["items_count"] == 7
        assert failures == []

    def test_every_node_failing_raises_rather_than_returning_empty(self):
        """No stats at all is an error, not an empty success.

        An empty mapping would reshape into a successful-looking response
        describing no indexes, which reads as "this cluster has none".
        """
        nodes = [
            self._node("a:8091", "http://a:9102/api/v1/stats"),
            self._node("b:8091", "http://b:9102/api/v1/stats"),
        ]
        with pytest.raises(RuntimeError, match="every index node"):
            self._fetch(nodes, lambda _url: RuntimeError("connection refused"))

    def test_every_node_denying_the_name_raises_its_own_error(self):
        """All-404 means the name is wrong, which is not "the cluster is down".

        Distinguished so the message can point at the bucket/scope/collection
        or index name instead of sending the caller to check connectivity.
        """
        nodes = [
            self._node("a:8091", "http://a:9102/api/v1/stats"),
            self._node("b:8091", "http://b:9102/api/v1/stats"),
        ]
        with pytest.raises(RuntimeError, match="does not exist"):
            self._fetch(
                nodes,
                lambda _url: self._response(status=404),
                bucket_name="nosuch",
            )

    def test_no_index_nodes_raises_before_any_request(self):
        """Discovery finding nothing is its own error, reported up front."""
        with pytest.raises(RuntimeError, match="No index-service nodes"):
            self._fetch([], lambda _url: self._response())


class TestEndpointResolversShareOneShape:
    """The management and query resolvers are the same function twice over.

    Both ping one service, dedup the remotes it reports, and otherwise fall
    back to the connection string's hosts on a scheme-chosen default port.
    They are parametrized together so a fix applied to one and forgotten in
    the other fails here -- the duplication is in the source, so it has to be
    in the test or only one copy stays honest.

    ``TestResolveManagementEndpoints`` above already covers the happy path and
    the TLS fallback for management; this class covers what neither resolver
    had: the fallback's own edges, and every query-resolver case.
    """

    #: (resolver, service key in the ping report, plain port, TLS port)
    RESOLVERS: ClassVar = [
        pytest.param(
            resolve_management_endpoints, "management", 8091, 18091, id="mgmt"
        ),
        pytest.param(resolve_query_endpoints, "query", 8093, 18093, id="query"),
    ]

    @staticmethod
    def _cluster(report):
        """A stand-in cluster whose ping returns *report* as JSON.

        Pass ``None`` to make ping raise instead, which is the other way the
        SDK can leave the resolver with nothing to go on.
        """
        if report is None:

            def ping(*_args, **_kwargs):
                raise RuntimeError("management service unreachable")
        else:

            def ping(*_args, **_kwargs):
                return SimpleNamespace(as_json=lambda: json.dumps(report))

        return SimpleNamespace(ping=ping)

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_plain_scheme_falls_back_to_the_plain_port(
        self, resolver, service, plain, tls
    ):
        """The counterpart to the TLS fallback, pinned as its own subject.

        The existing plain-port assertion rides along on the ping-raises test,
        so a change to the scheme check would be reported as an exception-path
        failure rather than as the port regression it is.
        """
        cluster = self._cluster({})
        assert resolver(cluster, "couchbase://host1") == [f"host1:{plain}"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_scheme_match_is_case_insensitive(self, resolver, service, plain, tls):
        """The resolver lowercases before testing the scheme, so uppercase is TLS.

        A connection string is not normalised before it reaches here, and URL
        schemes are case-insensitive, so ``COUCHBASES://`` has to select the
        TLS port like its lowercase spelling does.
        """
        cluster = self._cluster({})
        assert resolver(cluster, "COUCHBASES://host1") == [f"host1:{tls}"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_ipv6_host_is_rebracketed_in_the_fallback(
        self, resolver, service, plain, tls
    ):
        """An IPv6 literal survives the strip-then-restore round trip.

        ``extract_hosts_from_connection_string`` returns a hostname bare, so
        ``[::1]`` arrives here as ``::1``; the result is going into a URL, so
        ``_bracket_ipv6`` has to put the brackets back or the port reads as
        another address group. Both halves are tested elsewhere -- this pins
        their composition, which is where an unbracketed host would appear.
        """
        cluster = self._cluster({})
        assert resolver(cluster, "couchbase://[::1]:11210") == [f"[::1]:{plain}"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_ipv6_fallback_handles_several_hosts(self, resolver, service, plain, tls):
        """Each host is bracketed on its own, including alongside a named one."""
        cluster = self._cluster({})
        assert resolver(
            cluster, "couchbases://[2001:db8::1]:11207,node2.example.test:11207"
        ) == [f"[2001:db8::1]:{tls}", f"node2.example.test:{tls}"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_hostless_connection_string_yields_no_endpoints(
        self, resolver, service, plain, tls
    ):
        """No hosts to fall back to means no endpoints -- not a bare port.

        Formatting the default port with an empty host would produce
        ``":8091"``, which reads as a usable endpoint and fails later as a
        connection error pointing at the cluster rather than at the empty
        connection string. Callers iterate the result, so [] surfaces as
        "nothing to query" instead.
        """
        cluster = self._cluster({})
        assert resolver(cluster, "couchbase://") == []

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_ping_failure_falls_back_rather_than_raising(
        self, resolver, service, plain, tls
    ):
        """A ping that raises is caught and logged, not propagated.

        The fallback exists precisely for the case where the SDK cannot say
        where the service listens, so an unreachable service must degrade to
        the default port rather than fail the caller's tool call.
        """
        cluster = self._cluster(None)
        assert resolver(cluster, "couchbase://host1") == [f"host1:{plain}"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_duplicate_remotes_are_reported_once(self, resolver, service, plain, tls):
        """Ping can name the same remote twice; the caller fans out per node.

        A duplicate would mean querying one node twice and reporting it as
        two, which matters most for the query resolver -- /admin/vitals and
        /admin/active_requests answer per node, so every entry returned here
        becomes a separate request and a separate row.
        """
        cluster = self._cluster(
            {
                "services": {
                    service: [
                        {"remote": "10.0.1.11:9102"},
                        {"remote": "10.0.1.11:9102"},
                        {"remote": "10.0.1.12:9102"},
                    ]
                }
            }
        )
        assert resolver(cluster, "couchbase://10.0.1.11") == [
            "10.0.1.11:9102",
            "10.0.1.12:9102",
        ]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_entries_without_a_remote_are_skipped(self, resolver, service, plain, tls):
        """An endpoint the SDK reports with no (or an empty) remote is unusable.

        Keeping it would put None or "" into a URL; skipping it leaves the
        endpoints the SDK could actually name, which is still better than
        discarding the whole report and falling back.
        """
        cluster = self._cluster(
            {
                "services": {
                    service: [
                        {"remote": None},
                        {"remote": ""},
                        {"state": "ok"},
                        {"remote": "10.0.1.12:9102"},
                    ]
                }
            }
        )
        assert resolver(cluster, "couchbase://10.0.1.11") == ["10.0.1.12:9102"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_report_without_usable_entries_falls_back(
        self, resolver, service, plain, tls
    ):
        """A report whose every entry is unusable is as good as no report.

        The fallback is guarded on the collected endpoints rather than on
        whether the ping succeeded, so a service block that yields nothing
        still reaches the connection string.
        """
        cluster = self._cluster({"services": {service: [{"remote": None}]}})
        assert resolver(cluster, "couchbase://host1") == [f"host1:{plain}"]

    @pytest.mark.parametrize(("resolver", "service", "plain", "tls"), RESOLVERS)
    def test_other_services_in_the_report_are_ignored(
        self, resolver, service, plain, tls
    ):
        """Only this resolver's own service block is read.

        The ping is already limited to one service type, so a report carrying
        another is not expected -- but reading the wrong block would silently
        return the wrong ports, so the key lookup is pinned here.
        """
        other = "query" if service == "management" else "management"
        cluster = self._cluster(
            {
                "services": {
                    service: [{"remote": "10.0.1.11:9999"}],
                    other: [{"remote": "10.0.1.11:1111"}],
                }
            }
        )
        assert resolver(cluster, "couchbase://10.0.1.11") == ["10.0.1.11:9999"]


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


class TestGetIndexStatsCapellaGuard:
    """The Capella rejection must happen before anything is attempted.

    Registration normally keeps this tool off a Capella server entirely, so
    this guard only runs where gating did not apply — an embedding host that
    resolved no deployment, or a Capella cluster behind a custom hostname. It
    is the only check in those cases, which is why it is tested directly
    rather than through the registration path.
    """

    @staticmethod
    def _call_with_capella():
        with (
            patch(
                "cb_mcp.tools.operational.index.get_settings",
                return_value=CAPELLA_SETTINGS,
            ),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch(
                "cb_mcp.tools.operational.index.get_cluster_connection"
            ) as get_cluster,
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api"
            ) as fetch,
        ):
            with pytest.raises(ValueError) as excinfo:
                get_index_stats(SimpleNamespace())
            return excinfo.value, get_cluster, fetch

    def test_raises_for_a_capella_connection(self):
        error, _, _ = self._call_with_capella()
        assert "Capella" in str(error)

    def test_does_not_open_a_cluster_connection(self):
        """Rejecting up front is the point: no connection, no REST call.

        A guard that fired after ``get_cluster_connection`` would still raise,
        but only after paying for a connection the tool cannot use.
        """
        _, get_cluster, fetch = self._call_with_capella()
        get_cluster.assert_not_called()
        fetch.assert_not_called()

    def test_self_managed_connection_is_not_rejected(self):
        """The guard must key on the host, not merely on being called."""
        with (
            patch("cb_mcp.tools.operational.index.get_settings", return_value=SETTINGS),
            patch("cb_mcp.tools.operational.index.validate_connection_settings"),
            patch("cb_mcp.tools.operational.index.get_cluster_connection"),
            patch(
                "cb_mcp.tools.operational.index.fetch_index_stats_from_rest_api",
                return_value=({"node1:8091": RAW_STATS}, [], []),
            ),
        ):
            result = get_index_stats(SimpleNamespace())
        assert "node1:8091" in result["nodes"]
