"""Unit tests for the cluster health snapshot merge.

``build_cluster_health_snapshot`` joins three REST payloads, and the join is
where the subtle bugs live: the orchestrator is named by ``otpNode`` while the
two node lists key on hostnames written differently. Being a pure function it
needs no HTTP, so the sick-cluster cases below (a node in "warning", one failed
over, an orchestrator mid-election) are reachable here in a way they are not
against a live cluster.

The payloads are trimmed versions of real 7.6 responses — only the fields the
merge reads, plus a couple it must ignore.
"""

from __future__ import annotations

from typing import Any, ClassVar

from cb_mcp.utils.operational.cluster_health import build_cluster_health_snapshot

_ORCHESTRATOR = "ns_1@10.0.1.11"


def _node(
    host: str,
    *,
    services: list[str] | None = None,
    status: str = "healthy",
    membership: str = "active",
    recovery: str = "none",
) -> dict[str, Any]:
    """A /pools/default nodes[] entry, with the noise the merge should drop."""
    return {
        "hostname": f"{host}:8091",
        "otpNode": f"ns_1@{host}",
        "status": status,
        "clusterMembership": membership,
        "recoveryType": recovery,
        "services": services if services is not None else ["kv"],
        "serverGroup": "Group 1",
        "version": "7.6.0-2176-enterprise",
        "uptime": "114242",
        # Dropped by the merge — present so the tests prove they stay out.
        "systemStats": {"cpu_utilization_rate": 5.87, "allocstall": 0},
        "interestingStats": {"ops": 0, "index_disk_size": 5004482},
        "nodeUUID": "abc123",
    }


def _pools_default(nodes: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload = {
        "clusterName": "prod",
        "balanced": True,
        "rebalanceStatus": "none",
        "counters": {"rebalance_success": 1, "rebalance_start": 1},
        "alerts": [],
        "nodes": nodes,
        # Dropped by the merge.
        "controllers": {"failOver": {"uri": "/controller/failOver?uuid=x"}},
        "storageTotals": {"ram": {"total": 1}},
    }
    payload.update(overrides)
    return payload


def _node_services(
    hosts: list[str], *, external: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A nodeServices payload: bare hostnames, per-node service ports."""
    return {
        "rev": 123,
        "clusterCapabilities": {"n1ql": ["costBasedOptimizer"]},
        "nodesExt": [
            {
                "hostname": host,
                "services": {"mgmt": 8091, "kv": 11210, "indexHttp": 9102},
                **({"alternateAddresses": external} if external else {}),
            }
            for host in hosts
        ],
    }


_TERSE = {
    "orchestrator": _ORCHESTRATOR,
    "clusterCompatVersion": "7.6",
    "clusterUUID": "7aa0db58",
    "isBalanced": True,
}


class TestBareHost:
    """bare_host: strip :port without truncating an IPv6 literal."""


class TestOrchestratorIdentification:
    """The otpNode comparison, and what happens when nobody is named."""

    def test_hostname_comparison_would_match_nothing(self) -> None:
        """Regression guard for the quiet failure this join invites.

        Comparing orchestrator against ``hostname`` ("10.0.1.11:8091" vs
        "ns_1@10.0.1.11") is False for *every* node, so the orchestrator would
        go unflagged and be reported safe to restart. On an all-healthy cluster
        nothing else in the output changes, so only this assertion catches it.
        """
        nodes = [_node("10.0.1.11"), _node("10.0.1.12")]
        assert all(node["hostname"] != _ORCHESTRATOR for node in nodes)

        snapshot = build_cluster_health_snapshot(
            _pools_default(nodes), _node_services(["10.0.1.11", "10.0.1.12"]), _TERSE
        )

        assert [n["is_orchestrator"] for n in snapshot["nodes"]] == [True, False]

    def test_safe_to_act_on_is_false_only_for_orchestrator(self) -> None:
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11"), _node("10.0.1.12")]),
            _node_services(["10.0.1.11", "10.0.1.12"]),
            _TERSE,
        )

        assert [n["safe_to_act_on"] for n in snapshot["nodes"]] == [False, True]

    def test_orchestrator_naming_an_unknown_node_is_not_known(self) -> None:
        """terseClusterInfo can name a node /pools/default did not report.

        The three payloads are read in sequence, so a topology change between
        them leaves a name that matches nothing. Reporting that as "known"
        while every node is flagged safe is the contradiction the field exists
        to prevent.
        """
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.12")]),
            _node_services(["10.0.1.12"]),
            {"orchestrator": "ns_1@10.0.1.99"},
        )

        assert snapshot["cluster"]["orchestrator_known"] is False
        assert all(node["is_orchestrator"] is False for node in snapshot["nodes"])

    def test_missing_orchestrator_flags_no_node(self) -> None:
        """Mid-election the cluster reports no orchestrator.

        Both sides default to "", so an unguarded ``otp == orch`` would match
        on emptiness and flag a node that is not the orchestrator.
        """
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11"), {"hostname": "10.0.1.12:8091"}]),
            _node_services(["10.0.1.11", "10.0.1.12"]),
            {"clusterCompatVersion": "7.6"},
        )

        assert snapshot["cluster"]["orchestrator_known"] is False
        assert snapshot["cluster"]["orchestrator"] == ""
        assert all(node["is_orchestrator"] is False for node in snapshot["nodes"])


class TestHealthRollup:
    """The cluster-level summary an agent reads before scanning nodes[]."""

    def test_counts_and_lists_unhealthy_nodes(self) -> None:
        snapshot = build_cluster_health_snapshot(
            _pools_default(
                [
                    _node("10.0.1.11"),
                    _node("10.0.1.12", services=["index"], status="warning"),
                    _node("10.0.1.13", status="unhealthy"),
                ]
            ),
            _node_services(["10.0.1.11", "10.0.1.12", "10.0.1.13"]),
            _TERSE,
        )

        cluster = snapshot["cluster"]
        assert cluster["nodes_total"] == 3
        assert cluster["nodes_by_status"] == {
            "healthy": 1,
            "unhealthy": 1,
            "warning": 1,
        }
        assert cluster["unhealthy_nodes"] == ["10.0.1.12:8091", "10.0.1.13:8091"]

    def test_failed_over_node_is_inactive_not_unhealthy(self) -> None:
        """An already-failed-over node can still report status "healthy".

        Membership and status answer different questions, so a node that has
        left the cluster must not be conflated with one merely reporting a
        warning.
        """
        snapshot = build_cluster_health_snapshot(
            _pools_default(
                [
                    _node("10.0.1.11"),
                    _node("10.0.1.12", membership="inactiveFailed"),
                ]
            ),
            _node_services(["10.0.1.11", "10.0.1.12"]),
            _TERSE,
        )

        cluster = snapshot["cluster"]
        assert cluster["inactive_nodes"] == ["10.0.1.12:8091"]
        assert cluster["unhealthy_nodes"] == []

    def test_carries_cluster_level_signals(self) -> None:
        """balanced / rebalanceStatus / counters / alerts drive use case 2."""
        alert = {"msg": "Node 10.0.1.12 is down", "serverTime": "2026-01-01T00:00:00Z"}
        snapshot = build_cluster_health_snapshot(
            _pools_default(
                [_node("10.0.1.11")],
                balanced=False,
                rebalanceStatus="running",
                alerts=[alert],
                counters={"rebalance_success": 1, "rebalance_start": 2},
            ),
            _node_services(["10.0.1.11"]),
            _TERSE,
        )

        cluster = snapshot["cluster"]
        assert cluster["balanced"] is False
        assert cluster["rebalanceStatus"] == "running"
        assert cluster["alerts"] == [alert]
        # Raw tallies are passed through; rebalance_start above
        # rebalance_success is the caller's "began and did not finish" signal.
        assert cluster["counters"] == {"rebalance_success": 1, "rebalance_start": 2}

    def test_defaults_missing_cluster_fields(self) -> None:
        """A payload missing alerts/counters must not raise."""
        snapshot = build_cluster_health_snapshot(
            {"nodes": [_node("10.0.1.11")]}, _node_services(["10.0.1.11"]), _TERSE
        )

        assert snapshot["cluster"]["alerts"] == []
        assert snapshot["cluster"]["counters"] == {}


class TestServicePortJoin:
    """Joining nodeServices onto /pools/default despite differing host spellings."""

    def test_joins_ipv6_nodes(self) -> None:
        pools = _pools_default([_node("[::1]")])
        pools["nodes"][0]["hostname"] = "[::1]:8091"
        pools["nodes"][0]["otpNode"] = "ns_1@::1"

        snapshot = build_cluster_health_snapshot(
            pools, _node_services(["::1"]), {"orchestrator": "ns_1@::1"}
        )

        node = snapshot["nodes"][0]
        assert node["service_ports"]["indexHttp"] == 9102
        assert node["is_orchestrator"] is True

    def test_node_absent_from_node_services_gets_empty_ports(self) -> None:
        """A node missing from nodeServices must not raise or drop the node."""
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11"), _node("10.0.1.99")]),
            _node_services(["10.0.1.11"]),
            _TERSE,
        )

        assert snapshot["nodes"][1]["service_ports"] == {}
        assert snapshot["nodes"][1]["reachable_address"] is None


class TestReachableAddress:
    """Choosing between internal and externally advertised addresses."""

    _EXTERNAL: ClassVar[dict[str, Any]] = {
        "external": {"hostname": "203.0.113.5", "ports": {"mgmt": 18091}}
    }

    def test_prefers_external_when_reached_on_an_advertised_address(self) -> None:
        """Reaching the cluster at an advertised external address is the
        evidence that external addresses work from here."""
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11")]),
            _node_services(["10.0.1.11"], external=self._EXTERNAL),
            _TERSE,
            reached_host="203.0.113.5",
        )

        node = snapshot["nodes"][0]
        assert node["reachable_address"] == "203.0.113.5"
        assert node["reachable_from_here"] == "external"
        # The raw block stays as a fallback for when the inference is wrong.
        assert node["alternateAddresses"] == self._EXTERNAL

    def test_publishes_external_ports_with_the_external_address(self) -> None:
        """Ports must match the address they are published with.

        On a NAT'd or port-mapped deployment the internal ports are not
        reachable at the external address, so pairing one with the other hands
        the caller an endpoint it cannot dial.
        """
        external = {
            "external": {
                "hostname": "203.0.113.5",
                "ports": {"mgmt": 18091, "indexHttp": 19102},
            }
        }
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11", services=["index"])]),
            _node_services(["10.0.1.11"], external=external),
            _TERSE,
            reached_host="203.0.113.5",
        )

        node = snapshot["nodes"][0]
        assert node["reachable_from_here"] == "external"
        assert node["service_ports"] == {"mgmt": 18091, "indexHttp": 19102}

    def test_uses_internal_when_reached_on_an_internal_address(self) -> None:
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11")]),
            _node_services(["10.0.1.11"], external=self._EXTERNAL),
            _TERSE,
            reached_host="10.0.1.11",
        )

        node = snapshot["nodes"][0]
        assert node["reachable_address"] == "10.0.1.11"
        assert node["reachable_from_here"] == "internal"

    def test_joins_sole_node_that_reports_no_hostname(self) -> None:
        """A single-node cluster knows only its own address and sends none.

        The address that answered need not be how /pools/default spells the
        node — a cluster reached at "localhost" reports "172.18.0.2:8091"
        there — so keying the nameless entry by the reached host alone loses
        the join, and with it every port the caller needs to probe.
        """
        node_services = {"nodesExt": [{"services": {"mgmt": 8091, "kv": 11210}}]}
        pools = _pools_default([_node("172.18.0.2")])

        for reached in ("localhost", "127.0.0.1", "172.18.0.2"):
            snapshot = build_cluster_health_snapshot(
                pools, node_services, _TERSE, reached_host=reached
            )

            node = snapshot["nodes"][0]
            assert node["service_ports"]["kv"] == 11210, (
                f"reached at {reached!r}: ports lost"
            )
            assert node["reachable_address"] == reached

    def test_brackets_ipv6_reachable_address(self) -> None:
        """The address is handed on for a URL, so an IPv6 literal needs brackets."""
        pools = _pools_default([_node("[::1]")])
        pools["nodes"][0]["hostname"] = "[::1]:8091"

        snapshot = build_cluster_health_snapshot(
            pools, _node_services(["::1"]), _TERSE, reached_host="::1"
        )

        assert snapshot["nodes"][0]["reachable_address"] == "[::1]"


class TestOmittedFields:
    """Fields deliberately kept out of the snapshot."""

    def test_drops_per_node_sample_metrics(self) -> None:
        """systemStats/interestingStats are single samples; get_cluster_metrics
        serves the same quantities over a window."""
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11")]), _node_services(["10.0.1.11"]), _TERSE
        )

        node = snapshot["nodes"][0]
        assert "systemStats" not in node
        assert "interestingStats" not in node
        assert "nodeUUID" not in node

    def test_drops_mutation_control_urls(self) -> None:
        """controllers holds pre-signed failOver/ejectNode URLs; this tool is
        read-only and advisory, so they never reach the caller."""
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11")]), _node_services(["10.0.1.11"]), _TERSE
        )

        assert "controllers" not in snapshot["cluster"]
        assert "failOver" not in str(snapshot)

    def test_keeps_uptime_as_sent(self) -> None:
        """Couchbase reports uptime as a string of seconds; not coerced."""
        snapshot = build_cluster_health_snapshot(
            _pools_default([_node("10.0.1.11")]), _node_services(["10.0.1.11"]), _TERSE
        )

        assert snapshot["nodes"][0]["uptime_seconds"] == "114242"


class TestEmptyPayloads:
    """Degenerate inputs must produce a well-formed snapshot, not an exception."""

    def test_node_missing_every_optional_field(self) -> None:
        snapshot = build_cluster_health_snapshot(
            {"nodes": [{}]}, _node_services([]), _TERSE
        )

        node = snapshot["nodes"][0]
        assert node["hostname"] is None
        assert node["status"] is None
        assert node["is_orchestrator"] is False
        assert node["service_ports"] == {}
