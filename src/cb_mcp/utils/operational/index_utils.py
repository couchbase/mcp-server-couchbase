"""
Utility functions for index operations.

This module contains helper functions for working with Couchbase indexes.
"""

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx
from couchbase.diagnostics import ServiceType
from couchbase.options import PingOptions

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.constants import (
    INDEX_REST_PORT_PLAIN,
    INDEX_REST_PORT_TLS,
    MANAGEMENT_REST_PORT_PLAIN,
    MANAGEMENT_REST_PORT_TLS,
)
from .connection_string import (
    determine_ssl_verification,
    extract_hosts_from_connection_string,
)

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.index_utils")


def validate_filter_params(
    bucket_name: str | None,
    scope_name: str | None,
    collection_name: str | None,
    index_name: str | None = None,
) -> None:
    """Validate that filter parameters are provided in the correct hierarchy."""
    if scope_name and not bucket_name:
        raise ValueError("bucket_name is required when filtering by scope_name")
    if collection_name and (not bucket_name or not scope_name):
        raise ValueError(
            "bucket_name and scope_name are required when filtering by collection_name"
        )
    if index_name and (not bucket_name or not scope_name or not collection_name):
        raise ValueError(
            "bucket_name, scope_name, and collection_name are required when filtering by index_name"
        )


def clean_index_definition(definition: Any) -> str:
    """Clean up index definition string by removing quotes and escape characters."""
    if isinstance(definition, str) and definition:
        return definition.strip('"').replace('\\"', '"')
    return ""


def _raw_fallback(idx: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build a fallback response when an index row cannot be fully processed.

    Returns the raw index data as-is under ``raw_index_stats`` and a
    ``warning`` field explaining what went wrong.
    """
    logger.warning(
        "Failed to process index data (%s). There's a problem in fetching the "
        "index information. Please report this issue. Returning raw index data "
        "as-is.",
        reason,
    )
    return {
        "warning": (
            f"Failed to process index data: {reason}. Returning raw row "
            "under 'raw_index_stats' — please report this issue."
        ),
        "raw_index_stats": idx,
    }


def _validate_rest_row(idx: dict[str, Any]) -> str | None:
    """Return a warning reason if *idx* from the REST API is missing required fields."""
    if not (idx.get("indexName") or idx.get("name")):
        return "missing 'indexName'/'name' field"
    definition = idx.get("definition")
    if not definition or not isinstance(definition, str):
        return "missing or invalid 'definition' field"
    if not idx.get("status"):
        return "missing 'status' field"
    if not idx.get("bucket"):
        return "missing 'bucket' field"
    if "lastScanTime" not in idx:
        return "missing 'lastScanTime' field"
    return None


def _validate_query_row(idx: dict[str, Any]) -> str | None:
    """Return a warning reason if *idx* from system:indexes is missing required fields."""
    if not idx.get("name"):
        return "missing 'name' field"
    metadata = idx.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("definition"):
        return "missing or invalid 'metadata.definition' field"
    if not idx.get("state"):
        return "missing 'state' field"
    for field in ("bucket", "scope", "collection"):
        if not idx.get(field):
            return f"missing {field!r} field (LET clause may not have run)"
    if "last_scan_time" not in metadata:
        return "missing 'metadata.last_scan_time' field"
    return None


def process_index_data_from_rest_api(
    idx: dict[str, Any],
) -> dict[str, Any]:
    """Process raw index data from the REST API into formatted index info.

    Args:
        idx: Raw index data from the /getIndexStatus API

    Returns:
        Formatted index info dictionary. If a required field is missing or
        invalid, returns a fallback dict containing ``warning`` and the
        unprocessed raw row under ``raw_index_stats``.
    """
    warning = _validate_rest_row(idx)
    if warning:
        return _raw_fallback(idx, warning)

    name = idx.get("indexName") or idx.get("name")
    raw_definition = idx["definition"]

    index_info: dict[str, Any] = {
        "name": name,
        "definition": clean_index_definition(raw_definition),
        "status": idx["status"],
        "isPrimary": bool(idx.get("isPrimary", False)),
        "bucket": idx["bucket"],
    }

    if "scope" in idx:
        index_info["scope"] = idx["scope"]
    if "collection" in idx:
        index_info["collection"] = idx["collection"]

    if idx["lastScanTime"]:
        index_info["lastScanTime"] = idx["lastScanTime"]

    return index_info


def process_index_data_from_query(
    idx: dict[str, Any],
) -> dict[str, Any]:
    """Process a row from ``system:indexes`` into formatted index info.

    Bucket / scope / collection are normalized in SQL++ by
    ``fetch_indexes_via_query_service`` via a LET clause, so legacy
    bucket-level indexes (only ``keyspace_id`` present) and modern scoped
    indexes (``bucket_id`` + ``scope_id`` + ``keyspace_id``) both arrive
    here with the same enriched shape — no branching needed here.

    Args:
        idx: A single index row from ``system:indexes`` with ``bucket`` /
            ``scope`` / ``collection`` already injected by the LET clause
            in the fetch query.

    Returns:
        Formatted index info dictionary. If a required field is missing or
        invalid, returns a fallback dict containing ``warning`` and the
        unprocessed raw row under ``raw_index_stats``.
    """
    warning = _validate_query_row(idx)
    if warning:
        return _raw_fallback(idx, warning)

    metadata = idx["metadata"]

    return {
        "name": idx["name"],
        "definition": metadata["definition"],
        "status": idx["state"],
        "bucket": idx["bucket"],
        "scope": idx["scope"],
        "collection": idx["collection"],
        "isPrimary": bool(idx.get("is_primary", False)),
        "lastScanTime": metadata["last_scan_time"],
    }


def parse_major_version(version_str: str | None) -> int:
    """Extract the integer major version from a Couchbase version string.

    Examples:
        - "8.0.0-1928-enterprise" -> 8
        - "7.6.0"                 -> 7

    Args:
        version_str: Node ``version`` string returned by the cluster, such as a value from ``cluster_info().nodes``.

    Returns:
        Major version as int.

    Raises:
        ValueError: If *version_str* is empty, None, or cannot be parsed.
    """
    if not version_str:
        raise ValueError("version_str is empty or None")
    major_version = version_str.strip().split(".", 1)[0]
    # Handle prefixes like "v8" defensively.
    major_version = major_version.lstrip("vV")
    try:
        return int(major_version)
    except ValueError:
        raise ValueError(f"Cannot parse major version from {version_str!r}") from None


def resolve_cluster_major_version(cluster: Any) -> int:
    """Detect the cluster's major version via the SDK.

    Reads the per-node ``version`` field from ``cluster.cluster_info().nodes``
    (Python SDK 4.1+) and returns the *minimum* major version across all nodes
    so we only enable the 8.x+ query-service path when every node supports it.

    The high-level helper properties (``server_version`` /
    ``server_version_short`` / ``server_version_full``) are intentionally not
    used: the SDK collapses them to ``None`` whenever the cluster reports
    mixed node versions, which is exactly the case where we still need an
    answer. Each node entry, in contrast, always carries a ``version`` string.

    Args:
        cluster: An already-connected Couchbase ``Cluster`` instance.

    Raises if cluster_info() fails — callers should not silently degrade
    when version detection is unavailable.
    """
    info = cluster.cluster_info()

    nodes = info.nodes or []
    versions: list[str] = []
    for node in nodes:
        if isinstance(node, dict):
            version = node.get("version")
        else:
            version = getattr(node, "version", None)
        if version:
            versions.append(str(version))

    if not versions:
        raise RuntimeError(
            "cluster_info() reported no nodes — cannot determine cluster version"
        )

    majors = [parse_major_version(v) for v in versions]
    min_major = min(majors)

    logger.info(f"Detected cluster node versions={versions} (min major={min_major})")
    return min_major


def _build_query_params(
    bucket_name: str | None,
    scope_name: str | None,
    collection_name: str | None,
    index_name: str | None = None,
) -> dict[str, str]:
    """Build query parameters for the index REST API.

    Args:
        bucket_name: Optional bucket name
        scope_name: Optional scope name
        collection_name: Optional collection name
        index_name: Optional index name

    Returns:
        Dictionary of query parameters
    """
    params = {}
    if bucket_name:
        params["bucket"] = bucket_name
    if scope_name:
        params["scope"] = scope_name
    if collection_name:
        params["collection"] = collection_name
    if index_name:
        params["index"] = index_name
    return params


def fetch_indexes_from_rest_api(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
    index_name: str | None = None,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Fetch indexes from Couchbase Index Service REST API.

    Uses the /getIndexStatus endpoint to retrieve index information.
    This endpoint returns indexes with their definitions directly from the Index Service.

    Args:
        connection_string: Couchbase connection string (may contain multiple hosts)
        username: Username for authentication
        password: Password for authentication
        bucket_name: Optional bucket name to filter indexes
        scope_name: Optional scope name to filter indexes
        collection_name: Optional collection name to filter indexes
        index_name: Optional index name to filter indexes
        ca_cert_path: Optional path to CA certificate for SSL verification.
                     If not provided and using Capella, will use Capella root CA.
        timeout: Request timeout in seconds (default: 30)

    Returns:
        List of index status dictionaries containing name, definition, and other metadata
    """
    # Extract all hosts from connection string, bracketing IPv6 literals for URL use
    hosts = [
        f"[{host}]" if ":" in host else host
        for host in extract_hosts_from_connection_string(connection_string)
    ]

    # Determine protocol and port based on whether TLS is enabled
    is_tls_enabled = connection_string.lower().startswith("couchbases://")
    protocol = "https" if is_tls_enabled else "http"
    port = INDEX_REST_PORT_TLS if is_tls_enabled else INDEX_REST_PORT_PLAIN

    logger.info(
        f"TLS {'enabled' if is_tls_enabled else 'disabled'}, "
        f"using {protocol.upper()} with port {port}"
    )

    # Build query parameters and determine SSL verification
    params = _build_query_params(bucket_name, scope_name, collection_name, index_name)
    verify_ssl = determine_ssl_verification(connection_string, ca_cert_path)

    # Try each host one by one until we get a successful response
    last_error = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            try:
                url = f"{protocol}://{host}:{port}/getIndexStatus"
                logger.info(
                    f"Attempting to fetch indexes from: {url} with params: {params}"
                )

                response = client.get(
                    url,
                    params=params,
                    auth=(username, password),
                )

                response.raise_for_status()
                data = response.json()
                indexes = data.get("status", [])

                logger.info(f"Successfully fetched {len(indexes)} indexes from {host}")
                return indexes

            except httpx.HTTPError as e:
                logger.warning(f"Failed to fetch indexes from {host}: {e}")
                last_error = e
            except Exception as e:
                logger.warning(f"Unexpected error when fetching from {host}: {e}")
                last_error = e

    # If we get here, all hosts failed
    error_msg = f"Failed to fetch indexes from all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)


def format_stats_keyspace_part(part: str) -> str:
    """Backtick-quote and URL-encode one keyspace segment for the stats path.

    The result is then percent-encoded so a name containing path characters
    cannot escape ``/api/v1/stats/``. Without this, a ``bucket_name`` of
    ``x/../../../stats`` normalises to ``/api/stats`` and reaches a different
    endpoint than the caller asked for.
    """
    quoted = f"`{part}`" if "." in part else part
    return quote(quoted, safe="`")


def build_stats_path(
    bucket_name: str | None,
    scope_name: str | None,
    collection_name: str | None,
    index_name: str | None,
) -> str:
    """Build the ``/api/v1/stats`` path suffix for the requested granularity.

    Returns ``""`` for the node-wide endpoint, ``/<keyspace>`` for a bucket,
    scope or collection, and ``/<keyspace>/<index>`` for a single index. The
    keyspace accepts a partial path, so ``bucket.scope`` is valid and narrows
    to the indexes in that scope. Callers are expected to have run
    :func:`validate_filter_params` first, so the filters are already known to
    be hierarchically consistent.
    """
    if not bucket_name:
        return ""

    # Each level is appended only if the one above it is present, so a gap in
    # the hierarchy truncates the keyspace rather than silently skipping a level.
    parts = [bucket_name]
    if scope_name:
        parts.append(scope_name)
        if collection_name:
            parts.append(collection_name)
    keyspace = ".".join(format_stats_keyspace_part(p) for p in parts)

    if index_name:
        return f"/{keyspace}/{format_stats_keyspace_part(index_name)}"
    return f"/{keyspace}"


def parse_index_stats_key(key: str) -> dict[str, str]:
    """Split an index stats response key into its keyspace components.

    Keys are colon-joined and come in two shapes — ``bucket:index`` for the
    default scope/collection, and ``bucket:scope:collection:index`` for a
    named keyspace. The index name is always the final segment, so the name is
    taken from the right rather than by a fixed position. Anything unexpected
    is reported as-is under ``name`` so no index is silently dropped.
    """
    parts = key.split(":")
    if len(parts) == 4:
        bucket, scope, collection, name = parts
    elif len(parts) == 2:
        bucket, name = parts
        scope, collection = "_default", "_default"
    else:
        # Unrecognised shape (e.g. a name containing a colon). Keep the raw key
        # rather than guessing at a split that could mislabel the index.
        return {"name": key}

    return {
        "bucket": bucket,
        "scope": scope,
        "collection": collection,
        "name": name,
    }


def _bracket_ipv6(host: str) -> str:
    """Wrap a bare IPv6 literal in brackets so it can be used in a URL."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def resolve_management_endpoints(cluster: Any, connection_string: str) -> list[str]:
    """List the cluster's management endpoints as ``host:port``.

    The SDK has bootstrapped, fetched the cluster map and applied any
    alternate-address mapping, so it knows where management is actually
    reachable — which the connection string does not: the port it carries is a
    KV port (the SDK bootstraps over KV), and a port-mapped or NAT'd cluster
    serves management somewhere other than the default. Falls back to the
    connection string's hosts on the default port when the SDK reports nothing.

    Uses ``ping`` rather than ``diagnostics``: diagnostics only reports sockets
    the SDK happens to have open, and a freshly opened cluster has connected to
    nothing but key-value, so it would report no management endpoint at all.
    The ping is limited to the management service to keep it cheap.
    """
    endpoints: list[str] = []
    try:
        report = json.loads(
            cluster.ping(PingOptions(service_types=[ServiceType.Management])).as_json()
        )
        for endpoint in report.get("services", {}).get("management", []):
            remote = endpoint.get("remote")
            if remote and remote not in endpoints:
                endpoints.append(remote)
    except Exception as e:
        logger.warning(f"Could not ping the management service: {e}")

    if endpoints:
        return endpoints

    # Nothing to go on — assume the default port, which is right for an
    # ordinary deployment even though it cannot cover a remapped one.
    is_tls = connection_string.lower().startswith("couchbases://")
    port = MANAGEMENT_REST_PORT_TLS if is_tls else MANAGEMENT_REST_PORT_PLAIN
    return [
        f"{_bracket_ipv6(host)}:{port}"
        for host in extract_hosts_from_connection_string(connection_string)
    ]


def _external_addresses(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a node's ``alternateAddresses.external`` block, or an empty dict."""
    return (entry.get("alternateAddresses") or {}).get("external") or {}


def _parse_index_nodes(
    payload: dict[str, Any],
    scheme: str,
    index_key: str,
    reached_host: str,
) -> list[dict[str, Any]]:
    """Pick the index-service nodes out of a ``nodeServices`` payload.

    ``nodeServices`` reports internal hostnames and carries any externally
    reachable mapping alongside them, ignoring the ``network`` query parameter,
    so the client has to choose between them. Having reached this endpoint at
    *reached_host* is the evidence: if that address is one the cluster
    advertises externally, external addresses work from here. The other form is
    kept as a fallback, since a node may advertise only some services
    externally.

    Nodes with no index service are skipped — they have nothing listening on
    the index port, so querying them would only manufacture failures that look
    like outages.
    """
    advertised_external = {
        _external_addresses(entry).get("hostname")
        for entry in payload.get("nodesExt", [])
    }
    prefer_external = reached_host in advertised_external

    nodes: list[dict[str, Any]] = []
    for entry in payload.get("nodesExt", []):
        services = entry.get("services", {})
        external = _external_addresses(entry)
        external_ports = external.get("ports", {})

        internal_port = services.get(index_key)
        external_port = external_ports.get(index_key)
        if not internal_port and not external_port:
            continue

        # A node that only knows its own address reports no hostname; the host
        # that answered is the right stand-in, since that is how we reached it.
        internal_host = _bracket_ipv6(entry.get("hostname") or reached_host)
        external_host = _bracket_ipv6(external.get("hostname") or "")

        internal_url = (
            f"{scheme}://{internal_host}:{internal_port}/api/v1/stats"
            if internal_port
            else None
        )
        external_url = (
            f"{scheme}://{external_host}:{external_port}/api/v1/stats"
            if external_port and external_host
            else None
        )
        ordered = (
            [external_url, internal_url]
            if prefer_external
            else [internal_url, external_url]
        )

        use_external = prefer_external and external_host
        node_host = external_host if use_external else internal_host
        mgmt_port = (
            external_ports.get("mgmt") if use_external else None
        ) or services.get("mgmt")

        nodes.append(
            {
                "node": f"{node_host}:{mgmt_port}",
                "stats_urls": [url for url in ordered if url],
            }
        )
    return nodes


def discover_index_nodes(
    cluster: Any,
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """List the cluster's index-service nodes via ``/pools/default/nodeServices``.

    That endpoint is cluster-wide — any node answers for all of them — and
    reports each node's real service ports, so the index port is read from the
    response rather than assumed.

    Returns:
        One dict per index node with ``node`` (``host:<management port>``, the
        form ``getIndexStatus`` and the UI use) and ``stats_urls`` (that node's
        Index Service base URLs, most likely reachable first).

    Raises:
        RuntimeError: If no management endpoint answers.
    """
    endpoints = resolve_management_endpoints(cluster, connection_string)
    if not endpoints:
        raise ValueError(f"No hosts found in connection_string: {connection_string!r}")

    is_tls = connection_string.lower().startswith("couchbases://")
    scheme = "https" if is_tls else "http"
    index_key = "indexHttps" if is_tls else "indexHttp"
    verify_ssl = determine_ssl_verification(connection_string, ca_cert_path)

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for endpoint in endpoints:
            url = f"{scheme}://{endpoint}/pools/default/nodeServices"
            try:
                logger.info(f"Discovering index nodes from: {url}")
                response = client.get(url, auth=(username, password))
                response.raise_for_status()
                # Whichever address answered tells us which network this client
                # is on, and so which of each node's addresses to try first.
                reached_host = endpoint.rsplit(":", 1)[0]
                nodes = _parse_index_nodes(
                    response.json(), scheme, index_key, reached_host
                )
                logger.info(f"Found {len(nodes)} index node(s) via {endpoint}")
                return nodes
            except Exception as e:
                logger.warning(f"Failed to discover index nodes from {endpoint}: {e}")
                last_error = e

    raise RuntimeError(
        f"Failed to discover index nodes from all management endpoints: "
        f"{endpoints}. Last error: {last_error}"
    )


def fetch_index_stats_from_rest_api(
    cluster: Any,
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
    index_name: str | None = None,
    skip_empty: bool = False,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Fetch per-index statistics from every index node in the cluster.

    ``/api/v1/stats`` is served by an individual indexer and reports only the
    indexes that node holds, so one call never describes the cluster: an index
    on another node is absent with no error, and a partitioned index reports
    only the partitions stored locally. Every index node is therefore queried
    and the results are kept separate, keyed by node.

    Returns:
        ``(per_node, not_hosted, failures)`` — *per_node* maps each node that
        answered to its raw stats body, *not_hosted* names the nodes that
        answered but do not hold the requested keyspace or index, and
        *failures* holds ``{"node", "error"}`` for nodes that could not be
        reached. A node is reported rather than raised on so one unreachable
        indexer does not discard the others' results.

    Raises:
        RuntimeError: If discovery fails, if no index node could be reached, or
            if every node reported the keyspace or index as absent.
    """
    nodes = discover_index_nodes(
        cluster, connection_string, username, password, ca_cert_path, timeout
    )
    if not nodes:
        raise RuntimeError(
            "No index-service nodes found in the cluster — nothing to query for "
            "index statistics."
        )

    path = build_stats_path(bucket_name, scope_name, collection_name, index_name)
    # Booleans must be rendered lowercase for the Go-based index service.
    params = {"skipEmpty": "true"} if skip_empty else {}
    verify_ssl = determine_ssl_verification(connection_string, ca_cert_path)

    per_node: dict[str, dict[str, Any]] = {}
    not_hosted: list[str] = []
    failures: list[dict[str, str]] = []
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for node in nodes:
            last_error: Exception | None = None
            absent = False
            for base_url in node["stats_urls"]:
                url = f"{base_url}{path}"
                try:
                    logger.info(f"Fetching index stats from: {url}")
                    response = client.get(url, params=params, auth=(username, password))
                    if response.status_code == httpx.codes.NOT_FOUND:
                        # The server answered: it does not hold this keyspace or
                        # index. That is a definitive reply, not a transport
                        # fault, so stop here — trying this node's other address
                        # could only replace it with a connection error.
                        logger.info(f"Not hosted on {node['node']}: {url}")
                        absent = True
                        last_error = None
                        break
                    response.raise_for_status()
                    per_node[node["node"]] = response.json()
                    last_error = None
                    break
                except Exception as e:
                    logger.warning(f"Failed to fetch index stats from {url}: {e}")
                    last_error = e
            if absent:
                not_hosted.append(node["node"])
            elif last_error is not None:
                failures.append({"node": node["node"], "error": str(last_error)})

    if not per_node:
        if not_hosted and not failures:
            # Every reachable node denied it, so the name itself is wrong —
            # distinct from the cluster being unreachable.
            raise RuntimeError(
                f"No index node holds {path or 'any index'} — the bucket, scope, "
                f"collection or index name does not exist. Checked: {not_hosted}"
            )
        raise RuntimeError(
            f"Failed to fetch index stats from every index node: {failures}"
        )

    return per_node, not_hosted, failures
