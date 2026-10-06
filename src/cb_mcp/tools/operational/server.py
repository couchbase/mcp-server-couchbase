"""
Tools for Couchbase cluster operations.

This module contains tools for testing the connection, and getting the buckets in the cluster, the scopes and collections in the bucket.

``get_server_configuration_status`` used to live here too. It moved to
``cb_mcp.tools.status``: its body names no SDK, and leaving it here meant a
second server could not report its own configuration. Everything remaining
in this module needs a Couchbase ``Cluster``.
"""

import json
import logging
from collections import Counter
from datetime import datetime
from typing import Any

import httpx
from couchbase.diagnostics import ServiceType
from couchbase.options import PingOptions
from fastmcp import Context

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.config import get_settings
from ...utils.operational.cluster_health import (
    bare_host,
    build_cluster_health_snapshot,
)
from ...utils.operational.connection import connect_to_bucket
from ...utils.operational.connection_string import (
    determine_ssl_verification,
    extract_hosts_from_connection_string,
    is_capella_connection,
    validate_connection_settings,
)
from ...utils.operational.constants import (
    MANAGEMENT_REST_PORT_PLAIN,
    MANAGEMENT_REST_PORT_TLS,
)
from ...utils.operational.context import get_cluster_connection
from ...utils.operational.index_utils import resolve_management_endpoints
from .query import run_cluster_query

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.server")

# /events returns ~430 bytes per event and its own default of 250 is ~119 KB — far
# more than a tool result should spend. 50 keeps a call alongside a metrics and a
# health result in one window; the cap stops a broad request from swamping it.
SYSTEM_EVENTS_DEFAULT_LIMIT = 50
SYSTEM_EVENTS_MAX_LIMIT = 500

EVENTS_ORDERING = "ascending_oldest_first"


def test_cluster_connection(
    ctx: Context, bucket_name: str | None = None
) -> dict[str, Any]:
    """Test the connection to Couchbase cluster and optionally to a bucket.
    This tool verifies the connection to the Couchbase cluster and bucket by establishing the connection if it is not already established.
    If bucket name is not provided, it will not try to connect to the bucket specified in the MCP server settings.
    Returns connection status and basic cluster information.
    """
    try:
        cluster = get_cluster_connection(ctx)
        bucket = None
        if bucket_name:
            bucket = connect_to_bucket(cluster, bucket_name)

        return {
            "status": "success",
            "cluster_connected": True,
            "bucket_connected": bucket is not None,
            "bucket_name": bucket_name,
            "message": "Successfully connected to Couchbase cluster",
        }
    except Exception as e:
        logger.error(f"Connection test failed: {e}", exc_info=True)
        return {
            "status": "error",
            "cluster_connected": False,
            "bucket_connected": False,
            "bucket_name": bucket_name,
            "error": str(e),
            "message": "Failed to connect to Couchbase cluster",
        }


def get_scopes_and_collections_in_bucket(
    ctx: Context, bucket_name: str
) -> dict[str, list[str]]:
    """Get the names of all scopes and collections in the bucket.
    Returns a dictionary with scope names as keys and lists of collection names as values.
    """
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Listing scopes and collections in bucket '{bucket_name}'")
        scopes_collections = {}
        collection_manager = bucket.collections()
        scopes = collection_manager.get_all_scopes()
        for scope in scopes:
            collection_names = [c.name for c in scope.collections]
            scopes_collections[scope.name] = collection_names
        logger.info(
            f"Found {len(scopes_collections)} scope(s) in bucket '{bucket_name}'"
        )
        return scopes_collections
    except Exception as e:
        logger.error(
            f"Error getting scopes and collections in bucket '{bucket_name}': {e}",
            exc_info=True,
        )
        raise


def get_buckets_in_cluster(ctx: Context) -> list[str]:
    """Get the names of all the accessible buckets in the cluster."""
    cluster = get_cluster_connection(ctx)
    logger.debug("Listing all buckets in cluster")
    bucket_manager = cluster.buckets()
    buckets_with_settings = bucket_manager.get_all_buckets()

    buckets = []
    for bucket in buckets_with_settings:
        buckets.append(bucket.name)

    logger.info(f"Found {len(buckets)} bucket(s) in cluster")
    return buckets


def get_scopes_in_bucket(ctx: Context, bucket_name: str) -> list[str]:
    """Get the names of all scopes in the given bucket."""
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Listing scopes in bucket '{bucket_name}'")
        scopes = bucket.collections().get_all_scopes()
        scope_names = [scope.name for scope in scopes]
        logger.info(f"Found {len(scope_names)} scope(s) in bucket '{bucket_name}'")
        return scope_names
    except Exception as e:
        logger.error(
            f"Error getting scopes in bucket '{bucket_name}': {e}", exc_info=True
        )
        raise


def get_collections_in_scope(
    ctx: Context, bucket_name: str, scope_name: str
) -> list[str]:
    """Get the names of all collections in the given scope and bucket."""

    # Get the collections in the scope using system:all_keyspaces collection
    logger.debug(f"Listing collections in {bucket_name}.{scope_name}")
    query = "SELECT DISTINCT(name) as collection_name FROM system:all_keyspaces where `bucket`=$bucket_name and `scope`=$scope_name"
    results = run_cluster_query(
        ctx, query, bucket_name=bucket_name, scope_name=scope_name
    )
    collection_names = [result["collection_name"] for result in results]
    logger.info(
        f"Found {len(collection_names)} collection(s) in {bucket_name}.{scope_name}"
    )
    return collection_names


def get_cluster_health_and_services(
    ctx: Context,
    bucket_name: str | None = None,
    service_types: list[str] | None = None,
) -> dict[str, Any]:
    """Check whether the cluster is reachable right now, and where it's broken.

    This actively pings (see caveat below) the cluster's services and reports, per service:
    - Whether it responded and how long it took (latency)
    - Which node/endpoint answered, and any error if it didn't

    Scope: cluster-level vs bucket-level ping
    - If bucket_name is omitted, this pings at the cluster level. This covers more services in
      one call, but whether the key-value (KV) service is included depends on the Couchbase
      Server version — it may be silently skipped.
    - If bucket_name is provided, this pings from the perspective of that bucket instead. This
      guarantees the KV service is covered for that bucket, but the result is scoped to that
      one bucket only — ping again per bucket_name to cover a multi-bucket cluster.

    service_types optionally restricts which services get pinged. Valid values: "key_value",
    "query", "search", "analytics", "view", "management", "eventing". Omit to ping every
    service. An unrecognized value returns an error response instead of raising.

    Caution — this is somewhat invasive: unlike a passive connection-state check, ping performs
    a live network round-trip to every targeted service. Prefer a narrow service_types filter,
    and avoid calling this in tight loops or high-frequency polling.

    Returns:
    - Cluster health status with service-level connection details and latency measurements
    """
    try:
        cluster = get_cluster_connection(ctx)

        ping_opts = (
            PingOptions(service_types=[ServiceType(s) for s in service_types])
            if service_types
            else None
        )
        ping_args = (ping_opts,) if ping_opts is not None else ()

        if bucket_name:
            # Ping services from the perspective of the bucket
            logger.debug(f"Pinging cluster services via bucket '{bucket_name}'")
            bucket = connect_to_bucket(cluster, bucket_name)
            ping_result = bucket.ping(*ping_args)
            result = ping_result.as_json()
        else:
            # Ping services from the perspective of the cluster
            logger.debug("Pinging cluster services")
            ping_result = cluster.ping(*ping_args)
            result = ping_result.as_json()

        logger.info("Retrieved cluster health and services information")
        return {
            "status": "success",
            "data": json.loads(result),
        }
    except Exception as e:
        logger.error(f"Error getting cluster health: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster health and services information",
        }


def get_cluster_diagnostics_report(ctx: Context) -> dict[str, Any]:
    """Check whether the client's connections were already broken, and for how long.

    Unlike get_cluster_health_and_services (which actively pings each service right now),
    this reports the SDK's own cached connection state without performing any network I/O.
    It's cheap enough to call frequently, but it's only as fresh as the last time the SDK
    actually talked to each node — it won't proactively detect a service that just went down
    if nothing has touched it since. Use get_cluster_health_and_services instead when you need
    a live, right-now reachability check; there's also no way to filter this report to specific
    services the way that tool's ping can, since no I/O means nothing to filter.

    For each known endpoint, reports which service it belongs to, its remote/local addresses,
    connection state, and last_activity — how long it's been since that connection last saw
    traffic. Also reports an overall online/degraded/offline cluster state.

    This call makes no request to the server at all, so it needs no specific RBAC role beyond
    whatever the initial cluster connection already required — unlike an active ping, it isn't
    gated on KV/Query/Search or Cluster Admin privileges.

    Returns:
    - Diagnostics report with per-endpoint connection state and overall cluster state
    """
    try:
        cluster = get_cluster_connection(ctx)
        logger.debug("Retrieving cluster diagnostics")
        diagnostics_result = cluster.diagnostics()
        result = diagnostics_result.as_json()

        logger.info("Retrieved cluster diagnostics information")
        return {
            "status": "success",
            "data": json.loads(result),
        }
    except Exception as e:
        logger.error(f"Error getting cluster diagnostics: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster diagnostics information",
        }


def get_cluster_metrics(
    ctx: Context,
    metrics: list[dict[str, Any]],
    timeout: int = 30,
) -> dict[str, Any]:
    """Get one or more cluster statistics over a historic time window in a single call.

    Use this to quantify a suspected problem (e.g. after get_cluster_health_and_services or
    get_cluster_diagnostics_report) — is a metric spiking, climbing, or stable over time?

    Self-managed Couchbase Server 7.6+ only — rejects Capella connections without a REST call.
    Calls POST /pools/default/stats/range.

    `metrics` is passed through as the request body: a list of specs, each with a required
    "metric" (list of {"label", "value"} pairs, e.g. [{"label": "name", "value":
    "kv_ep_diskqueue_fill"}]), an optional "nodes" ("host:port" targets; omit it to cover every
    node, which is usually what you want), and optional "applyFunctions",
    "nodesAggregation", "start"/"end" (negative seconds relative to now; default -60/now),
    "step" (seconds, default 10), "alignTimestamps".

    To find metric names, call discover_tool_input_values(tool_name="get_cluster_metrics") — it
    lists or searches the full Couchbase metrics reference bundled with this server, offline.
    Don't guess a metric name: an unknown name comes back as a per-spec error with no data.

    Not bounded: any number of specs, window, step, or node count is passed straight through to
    the REST call, so a broad request (long window, fine step, many nodes/specs) can return a
    large response.

    Returns {"status": "success", "data": [...]} (one entry per spec, each with "data" and any
    per-spec "errors") or {"status": "error", "error": "..."}.
    """
    try:
        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError("get_cluster_metrics is not supported on Capella clusters")

        for i, spec in enumerate(metrics):
            if not isinstance(spec, dict):
                raise ValueError(f"metrics[{i}] must be an object, got {spec!r}")
            step = spec.get("step", 10)
            end = spec.get("end", 0)
            start = spec.get("start", -60)
            if not all(
                isinstance(v, int) and not isinstance(v, bool)
                for v in (step, end, start)
            ):
                raise ValueError(
                    f"metrics[{i}] has non-integer 'step'/'start'/'end' "
                    f"(step={step!r}, start={start!r}, end={end!r}); all three must be integers."
                )

        is_tls = connection_string.lower().startswith("couchbases://")
        protocol, port = (
            ("https", MANAGEMENT_REST_PORT_TLS)
            if is_tls
            else ("http", MANAGEMENT_REST_PORT_PLAIN)
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        hosts = [
            f"[{host}]" if ":" in host else host
            for host in extract_hosts_from_connection_string(connection_string)
        ]
        if not hosts:
            raise ValueError(
                f"No hosts found in connection_string: {connection_string!r}"
            )

        last_error: Exception | None = None
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for host in hosts:
                try:
                    response = client.post(
                        f"{protocol}://{host}:{port}/pools/default/stats/range",
                        json=metrics,
                        auth=(settings["username"], settings["password"]),
                    )
                    response.raise_for_status()
                    data = response.json()
                    logger.info(
                        f"Retrieved cluster metrics for {len(metrics)} spec(s) from {host}"
                    )
                    return {"status": "success", "data": data}
                except Exception as e:
                    last_error = e
        raise RuntimeError(f"Failed to reach any host in {hosts}: {last_error}")
    except ValueError as e:
        # Up-front, documented rejections (bad input, Capella, no hosts) — not a
        # system fault, so no traceback noise in the logs.
        logger.warning(f"Rejected get_cluster_metrics request: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster metrics",
        }
    except Exception as e:
        logger.error(f"Error getting cluster metrics: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster metrics",
        }


def get_cluster_tasks(ctx: Context, timeout: int = 30) -> list[dict[str, Any]]:
    """Get the cluster tasks running right now — rebalance, compaction, XDCR, index build.

    Answers "what is this cluster doing?" — where stuck rebalances, hung index builds and
    lagging XDCR surface. One call is a single sample, so pair it with get_cluster_metrics
    to tell "slow but progressing" from "flatlined".

    Calls GET /pools/default/tasks. Self-managed Couchbase Server 7.6+ only (Capella is
    rejected without a REST call); needs the Read-Only Admin (ro_admin) role.

    Returns the endpoint's array unchanged, or [] if no tasks are reported. Only "type" and
    "status" are common to every entry; the rest are per type — rebalance: progress,
    perNode, detailedProgress, stageInfo, subtype; bucket_compaction: bucket, progress,
    changesDone, totalChanges; xdcr: changesLeft, docsChecked, docsWritten, source, target;
    global_indexes/indexer: bucket, index, progress, id; loadingSampleBucket: task_id,
    bucket, bucket_uuid.

    Reading the result:
    - An idle cluster still reports a rebalance entry with status "notRunning", so check
      each task's "status" rather than counting entries.
    - "statusIsStale": true means the cluster cannot vouch for that status — read it as
      "unknown", not "stuck". Flatlined progress and a status that stopped updating look
      identical here but mean different things.
    - "progress" means different things per type, so do not compare it across tasks.
    - Poll no faster than "recommendedRefreshPeriod" (seconds), the server's own hint.
    - "cancelURI"/"lastReportURI" are informational UI links; this tool only ever GETs and
      never invokes them.
    """
    try:
        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError("get_cluster_tasks is not supported on Capella clusters")

        is_tls = connection_string.lower().startswith("couchbases://")
        protocol, port = (
            ("https", MANAGEMENT_REST_PORT_TLS)
            if is_tls
            else ("http", MANAGEMENT_REST_PORT_PLAIN)
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        hosts = [
            f"[{host}]" if ":" in host else host
            for host in extract_hosts_from_connection_string(connection_string)
        ]
        if not hosts:
            raise ValueError(
                f"No hosts found in connection_string: {connection_string!r}"
            )

        # Failover, not fan-out: tasks are tracked by the orchestrator and every node
        # relays its view, so any one node returns the whole cluster's answer. The first
        # host that responds is therefore complete — the rest are only tried if it is
        # unreachable. (Contrast fetch_index_stats_from_rest_api, which must visit every
        # index node because each one knows only its own indexes.)
        last_error: Exception | None = None
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for host in hosts:
                try:
                    response = client.get(
                        f"{protocol}://{host}:{port}/pools/default/tasks",
                        auth=(settings["username"], settings["password"]),
                    )
                    response.raise_for_status()
                    tasks = response.json()
                    running = sum(
                        1
                        for task in tasks
                        if isinstance(task, dict) and task.get("status") == "running"
                    )
                    logger.info(
                        f"Retrieved {len(tasks)} cluster task(s) ({running} running) from {host}"
                    )
                    return tasks
                except Exception as e:
                    logger.warning(f"Failed to fetch cluster tasks from {host}: {e}")
                    last_error = e
        raise RuntimeError(f"Failed to reach any host in {hosts}: {last_error}")
    except Exception as e:
        logger.error(f"Error getting cluster tasks: {e}", exc_info=True)
        raise


def get_cluster_health_snapshot(ctx: Context, timeout: int = 30) -> dict[str, Any]:
    """Get per-node service topology, membership, orchestrator and health in one call.

    Use this to turn a symptom into a specific node and service: which node is in
    "warning", what it runs, whether it is still in the cluster, and whether it is the
    orchestrator — i.e. whether acting on it is disruptive. Also answers "did a topology
    change leave us in a good state?" after a node replacement or failover.

    Self-managed Couchbase Server 7.6+ only (Capella is rejected without a REST call);
    needs the Read-Only Admin (ro_admin) role.

    Reading the result:
    - cluster.unhealthy_nodes / inactive_nodes are the fast path: a node is in
      unhealthy_nodes when status is not "healthy", and in inactive_nodes when it is no
      longer an "active" member (e.g. "inactiveFailed" — already failed over, which is a
      different situation from an active node merely reporting "warning").
    - safe_to_act_on is false only for the orchestrator, where a restart or failover is
      disruptive in a way it is not elsewhere. When cluster.orchestrator_known is false
      no node could be flagged, so read safe_to_act_on as "unknown", not "yes" — the
      cluster reports no orchestrator while one is being elected.
    - reachable_address is the address to probe next, chosen between the node's internal
      and externally advertised addresses based on which one this server reached the
      cluster on. Probing the other form may fail for network reasons that look like a
      node outage; alternateAddresses keeps it as a fallback.
    - cluster.counters holds lifetime rebalance tallies: rebalance_start above
      rebalance_success means a rebalance began and did not finish — the signature of an
      interrupted topology change. Pair with get_cluster_tasks for what is running now.
    - recoveryType other than "none" means a recovery is in progress: not healthy yet,
      rather than broken.

    Returns {"cluster": {...}, "nodes": [...]}; raises on failure.
    """
    try:
        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError(
                "get_cluster_health_snapshot is not supported on Capella clusters"
            )

        protocol = (
            "https" if connection_string.lower().startswith("couchbases://") else "http"
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        # Ask the SDK where management actually listens rather than appending the
        # default port to the connection string's hosts: the port it carries is a KV
        # one, and a port-mapped or NAT'd cluster serves management elsewhere. Same
        # resolution get_index_stats uses.
        endpoints = resolve_management_endpoints(
            get_cluster_connection(ctx), connection_string
        )
        if not endpoints:
            raise ValueError(
                f"No management endpoints found for connection_string: "
                f"{connection_string!r}"
            )

        # Failover, not fan-out: every node relays the whole cluster's topology, so the
        # first host that answers gives the complete picture and the rest are only tried
        # if it is unreachable.
        last_error: Exception | None = None
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for host in endpoints:
                try:
                    payloads = []
                    for path in (
                        "/pools/default",
                        "/pools/default/nodeServices",
                        "/pools/default/terseClusterInfo",
                    ):
                        response = client.get(
                            f"{protocol}://{host}{path}",
                            auth=(settings["username"], settings["password"]),
                        )
                        response.raise_for_status()
                        payloads.append(response.json())

                    pools_default, node_services, terse_cluster_info = payloads
                    snapshot = build_cluster_health_snapshot(
                        pools_default,
                        node_services,
                        terse_cluster_info,
                        # Which address answered decides whether this server can use the
                        # cluster's externally advertised addresses; strip the brackets
                        # an IPv6 literal carries in a URL, since nodeServices reports
                        # hostnames bare.
                        reached_host=bare_host(host),
                    )
                    cluster = snapshot["cluster"]
                    logger.info(
                        f"Retrieved cluster health snapshot from {host}: "
                        f"{cluster['nodes_total']} node(s), "
                        f"{len(cluster['unhealthy_nodes'])} not healthy"
                    )
                    return snapshot
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch cluster health snapshot from {host}: {e}"
                    )
                    last_error = e
        raise RuntimeError(f"Failed to reach any host in {endpoints}: {last_error}")
    except Exception as e:
        logger.error(f"Error getting cluster health snapshot: {e}", exc_info=True)
        raise


def _validate_system_events_limit(limit: int) -> None:
    """Reject a limit that would return an unusable amount of data.

    ``-1`` is the REST API's "no limit", which on a full ring buffer is several
    megabytes; it is rejected rather than forwarded. ``bool`` is excluded
    explicitly because it is an ``int`` subclass.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit must be an integer, got {limit!r}")
    if limit < 1 or limit > SYSTEM_EVENTS_MAX_LIMIT:
        raise ValueError(
            f"limit must be between 1 and {SYSTEM_EVENTS_MAX_LIMIT}, got {limit}. "
            f"The REST API's -1 ('no limit') is not accepted: the event log holds up "
            f"to 20,000 entries. Move the window with since_time instead of "
            f"raising the limit."
        )


def _validate_since_time(since_time: str) -> None:
    """Require an ISO-8601 timestamp at an explicit zero UTC offset.

    ``datetime.fromisoformat`` also accepts a bare date, a naive timestamp and a
    non-UTC offset, all of which the endpoint answers with a 400 — so they are
    caught here instead, where the message can name the expected form rather
    than relaying a bare status code.
    """
    try:
        parsed = datetime.fromisoformat(since_time.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"since_time must be an ISO-8601 UTC timestamp such as "
            f"'2026-10-05T09:12:04Z', got {since_time!r}"
        ) from e
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(
            f"since_time must be in UTC, ending in 'Z' (or '+00:00') — the "
            f"endpoint rejects a bare date, a naive timestamp or a non-UTC "
            f"offset. Got {since_time!r}"
        )


def _system_events_rejection(response: httpx.Response) -> str:
    """Describe a 4xx from /events using the server's own message.

    The endpoint reports a rejected parameter as ``{"errors": {"sinceTime":
    "..."}}`` — the authoritative reason for the running version, and the one
    thing an agent needs to correct itself. ``raise_for_status`` discards it, so
    it is read out here. Other 4xx bodies are not JSON (401 is empty, 404 is
    plain text), so those fall back to whatever text there is.
    """
    detail = ""
    try:
        errors = response.json().get("errors")
        if isinstance(errors, dict):
            detail = "; ".join(f"{field}: {msg}" for field, msg in errors.items())
    except (ValueError, AttributeError) as e:
        # Only 400 answers in JSON; 401 is empty and 404 is plain text. Falling
        # back to the raw body is the point, so this is logged and moved past
        # rather than raised — the caller already has a failure to report.
        logger.debug(f"/events error body was not the expected JSON: {e}")
    if not detail:
        detail = response.text.strip() or response.reason_phrase
    return f"/events rejected the request ({response.status_code}): {detail}"


def _shape_system_events(
    payload: Any,
    *,
    limit: int,
    since_time: str | None,
) -> dict[str, Any]:
    """Summarise an event list without reordering it.

    The list is returned exactly as the server sent it. /events emits events
    oldest-first and has already picked the right window, so re-sorting or
    re-slicing here would be wrong in a way that is easy to miss: slicing an
    ascending array keeps the OLDEST events and drops the newest, which is
    backwards for every use of this tool. To get fewer events, lower ``limit``
    and let the server choose them.
    """
    shaped: dict[str, Any] = {}
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        shaped["warning"] = (
            f"Expected an 'events' array, got {type(events).__name__}; "
            f"reporting it as empty."
        )
        events = []

    dicts = [event for event in events if isinstance(event, dict)]
    timestamps = [event.get("timestamp") for event in dicts if event.get("timestamp")]
    # len(events) == limit means the server filled the quota, so there are
    # probably more events outside this window.
    truncated = len(events) == limit

    # The endpoint's only cursor is sinceTime, and it is inclusive, so a batch
    # whose first and last events share a timestamp cannot be paged past: the
    # next call returns the same batch and the same cursor indefinitely. Offer
    # the cursor only when it is guaranteed to advance.
    next_since_time = (
        timestamps[-1]
        if (truncated and since_time and timestamps and timestamps[0] != timestamps[-1])
        else None
    )

    shaped["summary"] = {
        "returned": len(events),
        "limit": limit,
        # Stated in the payload as well as the docstring: the ordering is the
        # thing most likely to be misread.
        "ordering": EVENTS_ORDERING,
        "time_range": {
            "earliest": timestamps[0] if timestamps else None,
            "latest": timestamps[-1] if timestamps else None,
        },
        # Counts describe the returned events only, not the whole event log.
        "by_severity": dict(Counter(event.get("severity") for event in dicts)),
        "by_component": dict(Counter(event.get("component") for event in dicts)),
        "since_time": since_time,
        "possibly_truncated": truncated,
        # Supplied ready-made so paging never depends on indexing into the array
        # from the wrong end. Only meaningful when already paging forward: with
        # no since_time the batch is the newest there is.
        "next_since_time": next_since_time,
    }
    if truncated and since_time and timestamps and next_since_time is None:
        # Every event in a full batch shares one timestamp, so sinceTime — the
        # only cursor the endpoint offers — cannot move past them: the next call
        # would return this same batch forever. Say so rather than hand back a
        # cursor that does not advance.
        shaped["summary"]["paging_blocked"] = (
            f"All {len(events)} events share timestamp {timestamps[-1]}, which is "
            f"more than this limit can return. sinceTime cannot advance past them; "
            f"raise limit to see the rest of that timestamp."
        )
    shaped["events"] = events
    return shaped


def get_cluster_system_events(
    ctx: Context,
    since_time: str | None = None,
    limit: int = SYSTEM_EVENTS_DEFAULT_LIMIT,
    timeout: int = 30,
) -> dict[str, Any]:
    """Get the cluster's system event log — what changed on the cluster, and when.

    This is the RCA timeline: once a symptom is confirmed, it finds the config
    change, failover, rebalance or service restart that preceded it. Window it to
    the symptom's onset with since_time, then cross-check that timestamp against
    get_cluster_metrics.

    Self-managed Couchbase Server 7.6+ only (Capella is rejected without a REST
    call); needs the Full Admin or Cluster Admin role. Calls GET /events.

    Ordering — easy to misread:
    - Events are ALWAYS oldest-first, but which ones the server picks depends on
      since_time. Without it, the server takes the `limit` MOST RECENT events, so
      the last element is the newest thing on the cluster. With it, it takes the
      `limit` EARLIEST events at or after that time, so the last element is NOT
      the newest — more may follow it.
    - Do not re-sort or re-slice to get "the latest N": slicing keeps the oldest.
      Lower `limit` and let the server choose.
    - To page forward, pass summary.next_since_time as the next since_time. It is
      inclusive, so the boundary event repeats — dedupe on uuid. When it is null
      on a truncated result, check summary.paging_blocked: every event in the
      batch shares one timestamp, so sinceTime cannot move past them and a
      higher limit is the only way to see the rest.

    A cluster's log is overwhelmingly "info", so expect routine entries in the window; move
    the window rather than raising `limit`.

    Reading the result:
    - summary.by_severity / by_component say what the window holds at a glance, so
      a lone "error" among routine entries is visible without reading every event.
    - summary.possibly_truncated means the server filled the limit, so more events
      probably exist outside the window. Absence of a later event is not evidence.
    - The log is a ring buffer (10,000 entries by default), so an empty result for
      an old since_time can mean the events aged out, not that nothing happened.

    Returns {"summary": {...}, "events": [...]}; raises on failure.
    """
    try:
        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError(
                "get_cluster_system_events is not supported on Capella clusters"
            )

        _validate_system_events_limit(limit)
        if since_time is not None:
            _validate_since_time(since_time)

        params: dict[str, Any] = {"limit": limit}
        if since_time is not None:
            # The endpoint spells it camelCase.
            params["sinceTime"] = since_time

        protocol = (
            "https" if connection_string.lower().startswith("couchbases://") else "http"
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        # Ask the SDK where management actually listens rather than appending the
        # default port to the connection string's hosts: the port it carries is a KV
        # one, and a port-mapped or NAT'd cluster serves management elsewhere. Same
        # resolution get_index_stats uses.
        endpoints = resolve_management_endpoints(
            get_cluster_connection(ctx), connection_string
        )
        if not endpoints:
            raise ValueError(
                f"No management endpoints found for connection_string: "
                f"{connection_string!r}"
            )

        # Failover, not fan-out
        last_error: Exception | None = None
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for host in endpoints:
                try:
                    response = client.get(
                        f"{protocol}://{host}/events",
                        params=params,
                        auth=(settings["username"], settings["password"]),
                    )
                    # A 4xx is this request being refused, not the host being
                    # unreachable: every other node would refuse it identically,
                    # so report the server's reason instead of failing over and
                    # burying it under "failed to reach any host".
                    if 400 <= response.status_code < 500:
                        raise ValueError(_system_events_rejection(response))
                    response.raise_for_status()
                    shaped = _shape_system_events(
                        response.json(),
                        limit=limit,
                        since_time=since_time,
                    )
                    summary = shaped["summary"]
                    logger.info(
                        f"Retrieved {summary['returned']} system event(s) from {host}"
                        f"{' (truncated)' if summary['possibly_truncated'] else ''}"
                    )
                    return shaped
                except json.JSONDecodeError as e:
                    # A garbled body is this host misbehaving, not the request
                    # being wrong — and JSONDecodeError subclasses ValueError, so
                    # it must be caught ahead of the rejection branch below or a
                    # bad response from one node would abort the whole call.
                    logger.warning(
                        f"Failed to decode cluster system events from {host}: {e}"
                    )
                    last_error = e
                except ValueError:
                    # The request itself was refused — failing over would only
                    # collect the same refusal from every other node.
                    raise
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch cluster system events from {host}: {e}"
                    )
                    last_error = e
        raise RuntimeError(f"Failed to reach any host in {endpoints}: {last_error}")
    except Exception as e:
        logger.error(f"Error getting cluster system events: {e}", exc_info=True)
        raise
