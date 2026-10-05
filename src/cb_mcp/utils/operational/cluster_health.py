"""Merge the Management REST API's cluster-topology endpoints into one snapshot.

``get_cluster_health_snapshot`` fetches ``/pools/default`` (per-node status,
membership and services), ``/pools/default/nodeServices`` (service ports and
external addresses) and ``/pools/default/terseClusterInfo`` (the orchestrator,
which is in neither of the others); this module joins them.

Kept separate from the tool so the join is testable without HTTP — it is where
the subtle bugs live, since the payloads spell the same node different ways.
"""

import logging
from typing import Any

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from .index_utils import _bracket_ipv6, _external_addresses

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.cluster_health")

#: Node fields copied through from ``/pools/default``; everything else is
#: dropped, see ``build_cluster_health_snapshot``.
_NODE_FIELDS = (
    "hostname",
    "otpNode",
    "status",
    "clusterMembership",
    "recoveryType",
    "services",
    "serverGroup",
    "version",
)


#: Key for the sole node of a single-node cluster, which ``nodeServices`` may
#: report without a hostname. See ``_service_endpoints``.
_SOLE_NODE_KEY = "*"


def bare_host(host: str) -> str:
    """Strip the ``:port`` from a ``host:port``, leaving an IPv6 literal bare.

    ``/pools/default`` reports ``"10.0.1.12:8091"`` where ``nodeServices``
    reports ``"10.0.1.12"``, so one side has to be normalised before the two
    can be joined. ``"[::1]:8091"`` and ``"::1"`` both come back as ``"::1"``;
    a plain ``split(":")`` would truncate the IPv6 literal at its first group.
    """
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    # Several colons and no brackets is a bare IPv6 address, not host:port.
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_orchestrator(otp_node: str, orchestrator: str) -> bool:
    """Whether *otp_node* is the cluster's orchestrator.

    ``terseClusterInfo`` names the orchestrator by ``otpNode`` — the cluster
    manager's internal node id, ``"ns_1@10.0.1.11"`` — so the comparison is
    otpNode-to-otpNode. Matching against ``hostname`` would compare
    ``"10.0.1.11:8091"`` against ``"ns_1@10.0.1.11"`` and return False for
    *every* node, leaving the orchestrator unflagged and marked safe to restart.
    """
    return bool(orchestrator) and otp_node == orchestrator


def _service_endpoints(
    node_services: dict[str, Any], reached_host: str | None
) -> dict[str, dict[str, Any]]:
    """Index ``nodeServices`` by bare hostname, resolving each node's address.

    ``nodeServices`` reports internal hostnames and carries any externally
    reachable mapping alongside them without saying which works from here.
    Having reached the REST API at *reached_host* is the evidence: if that
    address is one the cluster advertises externally, external addresses work
    from here. ``_parse_index_nodes`` in ``index_utils`` makes the same call for
    the same reason — an unreachable internal address would turn a network
    boundary into what looks like a node outage.
    """
    entries = node_services.get("nodesExt") or []

    advertised_external = {
        _external_addresses(entry).get("hostname") for entry in entries
    }
    prefer_external = bool(reached_host) and reached_host in advertised_external

    resolved: dict[str, dict[str, Any]] = {}
    for entry in entries:
        external = _external_addresses(entry)
        external_host = external.get("hostname")
        # A node that only knows its own address reports no hostname; the host
        # that answered is the right stand-in, since that is how we reached it.
        internal_host = entry.get("hostname") or reached_host or ""

        use_external = bool(prefer_external and external_host)
        # External addresses come with their own ports: on a NAT'd or
        # port-mapped deployment the internal ports are not reachable at the
        # external address, so publishing one with the other would hand the
        # caller an endpoint that cannot be dialled.
        services = entry.get("services") or {}
        if use_external:
            services = external.get("ports") or services
        record = {
            "services": services,
            "alternate_addresses": entry.get("alternateAddresses"),
            "reachable_address": _bracket_ipv6(
                external_host if use_external else internal_host
            ),
            "reachable_from_here": "external" if use_external else "internal",
        }
        resolved[bare_host(internal_host)] = record
        # A nameless entry was keyed by whichever address answered, which need
        # not be how /pools/default spells the node (a single-node cluster
        # reached at "localhost" is "172.18.0.2:8091" there). With one node on
        # each side there is nothing to confuse it with, so publish it under
        # the empty key too and let the caller fall back to it.
        if not entry.get("hostname") and len(entries) == 1:
            resolved[_SOLE_NODE_KEY] = record
    return resolved


def build_cluster_health_snapshot(
    pools_default: dict[str, Any],
    node_services: dict[str, Any],
    terse_cluster_info: dict[str, Any],
    reached_host: str | None = None,
) -> dict[str, Any]:
    """Join the three topology payloads into one per-node health snapshot.

    *reached_host* is the host the REST calls succeeded against, used to resolve
    each node's reachable address (see ``_service_endpoints``).

    Only the fields answering "which node, which service, is it safe to act on"
    are carried through. The omissions are deliberate: ``systemStats`` and
    ``interestingStats`` are single samples whose every field is already
    available to ``get_cluster_metrics`` over a time window, and ``controllers``
    holds pre-signed failover/eject URLs that a read-only advisory tool has no
    business handing to a caller. ``storageTotals``, the quota fields, the
    bucket lists and the ``*URI`` links are another tool's subject or UI
    plumbing.

    Returns a dict with ``cluster`` (rollup) and ``nodes`` (per-node) keys.
    """
    orchestrator = terse_cluster_info.get("orchestrator") or ""
    endpoints = _service_endpoints(node_services, reached_host)

    nodes: list[dict[str, Any]] = []
    for entry in pools_default.get("nodes") or []:
        node = {field: entry.get(field) for field in _NODE_FIELDS}
        is_orchestrator = _is_orchestrator(node["otpNode"] or "", orchestrator)
        node["is_orchestrator"] = is_orchestrator
        # A restart or failover on the orchestrator is disruptive in a way the same action elsewhere is not; read alongside orchestrator_known.
        node["safe_to_act_on"] = not is_orchestrator
        # Couchbase sends uptime as a string of seconds; passed through as sent.
        node["uptime_seconds"] = entry.get("uptime")

        resolved = endpoints.get(bare_host(node["hostname"] or "")) or endpoints.get(
            _SOLE_NODE_KEY, {}
        )
        node["service_ports"] = resolved.get("services", {})
        node["reachable_address"] = resolved.get("reachable_address")
        node["reachable_from_here"] = resolved.get("reachable_from_here")
        if resolved.get("alternate_addresses"):
            node["alternateAddresses"] = resolved["alternate_addresses"]
        nodes.append(node)

    statuses = sorted({node["status"] for node in nodes if node["status"]})
    cluster = {
        "name": pools_default.get("clusterName"),
        "orchestrator": orchestrator,
        # Derived from an actual match, not merely from terseClusterInfo
        # carrying a name: the three payloads are read in sequence, so a
        # topology change can name an orchestrator that /pools/default did not
        # report. False means no node was flagged, so safe_to_act_on reads as
        # "unknown" rather than "yes".
        "orchestrator_known": any(node["is_orchestrator"] for node in nodes),
        "cluster_compat_version": terse_cluster_info.get("clusterCompatVersion"),
        "balanced": pools_default.get("balanced"),
        "rebalanceStatus": pools_default.get("rebalanceStatus"),
        # Lifetime tallies: rebalance_start above rebalance_success means one began and did not finish.
        "counters": pools_default.get("counters") or {},
        "alerts": pools_default.get("alerts") or [],
        "nodes_total": len(nodes),
        "nodes_by_status": {
            status: sum(1 for node in nodes if node["status"] == status)
            for status in statuses
        },
        "unhealthy_nodes": [
            node["hostname"] for node in nodes if node["status"] != "healthy"
        ],
        "inactive_nodes": [
            node["hostname"] for node in nodes if node["clusterMembership"] != "active"
        ],
    }

    logger.debug(
        f"Built cluster health snapshot: {len(nodes)} node(s), "
        f"{len(cluster['unhealthy_nodes'])} not healthy, "
        f"orchestrator={orchestrator or 'unknown'}"
    )
    return {"cluster": cluster, "nodes": nodes}
