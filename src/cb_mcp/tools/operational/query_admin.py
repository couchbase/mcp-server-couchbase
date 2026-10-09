"""
Tools for N1QL (Query service) admin health and remediation.

These call the Query service's own ``/admin/*`` REST API — distinct from
``query.py``'s SDK-driven SQL++ tools and from ``server.py``'s Management-port
admin tools. Self-managed Couchbase Server only: Capella does not expose this
port.
"""

import logging
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import Context

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.config import get_settings
from ...utils.operational.connection_string import (
    determine_ssl_verification,
    is_capella_connection,
    validate_connection_settings,
)
from ...utils.operational.context import get_cluster_connection
from ...utils.operational.index_utils import resolve_query_endpoints
from ...utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.query_admin")


def get_cluster_query_vitals(ctx: Context, timeout: int = 30) -> dict[str, Any]:
    """Get query-engine health (vitals) from every query-service node.

    Distinguishes "the workload is heavy" from "the query engine itself is
    stressed" — get_cluster_metrics covers node hardware; this covers the N1QL
    service layer specifically: request rate, active/queued request counts,
    memory, GC, service uptime. Use it after get_cluster_metrics/
    get_cluster_health_snapshot show healthy infra but queries are still slow.

    Calls GET /admin/vitals. Self-managed Couchbase Server 7.6+ only (Capella
    is rejected without a REST call); needs at minimum the Read-Only Admin
    (ro_admin) role — verify against your cluster's RBAC before relying on
    this in production, since the N1QL Admin API's exact privilege
    requirements are not fully documented.

    /admin/vitals answers per node, not cluster-wide — each query node only
    describes itself — so every query-service node is queried and the results
    are keyed by node ("host:port").

    Returns {"status": "success", "vitals": {"<host:port>": {...}, ...},
    "unreachable_nodes": [{"node", "error"}]} (the last key omitted when every
    node answered), or {"status": "error", "error": ...} if no query node
    could be reached at all, or the cluster is Capella.
    """
    try:
        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError(
                "get_cluster_query_vitals is not supported on Capella clusters"
            )

        protocol = (
            "https" if connection_string.lower().startswith("couchbases://") else "http"
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        endpoints = resolve_query_endpoints(
            get_cluster_connection(ctx), connection_string
        )
        if not endpoints:
            raise ValueError(
                f"No query-service endpoints found for connection_string: "
                f"{connection_string!r}"
            )

        vitals: dict[str, Any] = {}
        unreachable_nodes: list[dict[str, str]] = []
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for endpoint in endpoints:
                try:
                    response = client.get(
                        f"{protocol}://{endpoint}/admin/vitals",
                        auth=(settings["username"], settings["password"]),
                    )
                    response.raise_for_status()
                    vitals[endpoint] = response.json()
                except Exception as e:
                    logger.warning(f"Failed to fetch query vitals from {endpoint}: {e}")
                    unreachable_nodes.append({"node": endpoint, "error": str(e)})

        if not vitals:
            raise RuntimeError(
                f"Failed to reach any query node in {endpoints}: {unreachable_nodes}"
            )

        logger.info(
            f"Retrieved query vitals from {len(vitals)}/{len(endpoints)} query node(s)"
        )
        result: dict[str, Any] = {"status": "success", "vitals": vitals}
        if unreachable_nodes:
            result["unreachable_nodes"] = unreachable_nodes
        return result
    except ValueError as e:
        # Up-front, documented rejections (Capella, no endpoints) — not a
        # system fault, so no traceback noise in the logs.
        logger.warning(f"Rejected get_cluster_query_vitals request: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster query vitals",
        }
    except Exception as e:
        logger.error(f"Error getting cluster query vitals: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster query vitals",
        }


def get_active_queries(ctx: Context, timeout: int = 30) -> dict[str, Any]:
    """Get all queries executing right now, across every query-service node.

    Use this to find a runaway or stuck statement: elapsed time, the
    statement text, the client, and current state. Pair with
    get_cluster_query_vitals to confirm the engine is actually under load
    from what this returns, and pass a result's "requestId" to
    delete_active_query to cancel it.

    Calls GET /admin/active_requests. Self-managed Couchbase Server 7.6+ only
    (Capella is rejected without a REST call); needs at minimum the Read-Only
    Admin (ro_admin) role — verify against your cluster's RBAC before relying
    on this in production.

    /admin/active_requests answers per node — a request only shows up when
    the node running it is asked — so every query-service node is queried and
    the lists are merged into one. Each entry already carries its own "node"
    field, so there is no need to tag entries by which endpoint answered.

    Returns {"status": "success", "active_requests": [...merged...],
    "unreachable_nodes": [{"node", "error"}]} (the last key omitted when every
    node answered), or {"status": "error", "error": ...} if no query node
    could be reached at all, or the cluster is Capella.
    """
    try:
        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError("get_active_queries is not supported on Capella clusters")

        protocol = (
            "https" if connection_string.lower().startswith("couchbases://") else "http"
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        endpoints = resolve_query_endpoints(
            get_cluster_connection(ctx), connection_string
        )
        if not endpoints:
            raise ValueError(
                f"No query-service endpoints found for connection_string: "
                f"{connection_string!r}"
            )

        active_requests: list[dict[str, Any]] = []
        unreachable_nodes: list[dict[str, str]] = []
        any_success = False
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for endpoint in endpoints:
                try:
                    response = client.get(
                        f"{protocol}://{endpoint}/admin/active_requests",
                        auth=(settings["username"], settings["password"]),
                    )
                    response.raise_for_status()
                    items = response.json()
                    any_success = True
                    if isinstance(items, list):
                        active_requests.extend(items)
                    else:
                        logger.warning(
                            f"/admin/active_requests on {endpoint} returned "
                            f"{type(items).__name__}, expected a list; ignoring it"
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch active queries from {endpoint}: {e}"
                    )
                    unreachable_nodes.append({"node": endpoint, "error": str(e)})

        if not any_success:
            raise RuntimeError(
                f"Failed to reach any query node in {endpoints}: {unreachable_nodes}"
            )

        logger.info(
            f"Retrieved {len(active_requests)} active request(s) from "
            f"{len(endpoints) - len(unreachable_nodes)}/{len(endpoints)} query node(s)"
        )
        result: dict[str, Any] = {
            "status": "success",
            "active_requests": active_requests,
        }
        if unreachable_nodes:
            result["unreachable_nodes"] = unreachable_nodes
        return result
    except ValueError as e:
        logger.warning(f"Rejected get_active_queries request: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get active queries",
        }
    except Exception as e:
        logger.error(f"Error getting active queries: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get active queries",
        }


def delete_active_query(
    ctx: Context, request_id: str, timeout: int = 30
) -> dict[str, Any]:
    """Cancel an in-flight query by its request ID.

    This is the one tool in the query-health set with remediation power, and
    its blast radius is small and reversible in effect — it ends one query,
    not a topology or data change. A killed query cannot be resumed; confirm
    the request ID and statement with get_active_queries first.

    Calls DELETE /admin/active_requests/{request_id}. Self-managed Couchbase
    Server 7.6+ only (Capella is rejected without a REST call); needs the Full
    Admin or Cluster Admin role — verify against your cluster's RBAC before
    relying on this in production.

    A request lives on exactly one query node, and request_id alone does not
    say which, so every query-service node is tried in turn until one reports
    it cancelled; a "not found" on a node just means the request isn't there
    and the next node is tried.

    Returns {"success": True, "request_id": ..., "node": "<host:port that
    cancelled it>"} on success, or {"success": False, "error": ...,
    "request_id": ..., "nodes_tried": [...]} if no node reports having that
    request (already finished, wrong ID), the cluster is Capella, or
    request_id is empty.
    """
    try:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError(
                f"request_id must be a non-empty string, got {request_id!r}"
            )

        settings = get_settings(ctx)
        validate_connection_settings(settings)
        connection_string = settings["connection_string"]
        if is_capella_connection(connection_string):
            raise ValueError("delete_active_query is not supported on Capella clusters")

        protocol = (
            "https" if connection_string.lower().startswith("couchbases://") else "http"
        )
        verify_ssl = determine_ssl_verification(
            connection_string, settings.get("ca_cert_path")
        )
        endpoints = resolve_query_endpoints(
            get_cluster_connection(ctx), connection_string
        )
        if not endpoints:
            raise ValueError(
                f"No query-service endpoints found for connection_string: "
                f"{connection_string!r}"
            )

        encoded_id = quote(request_id, safe="")
        nodes_tried: list[dict[str, str]] = []
        with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
            for endpoint in endpoints:
                try:
                    response = client.delete(
                        f"{protocol}://{endpoint}/admin/active_requests/{encoded_id}",
                        auth=(settings["username"], settings["password"]),
                    )
                    if response.status_code == 404:
                        nodes_tried.append({"node": endpoint, "result": "not found"})
                        continue
                    response.raise_for_status()
                    logger.info(f"Cancelled query {request_id!r} on {endpoint}")
                    return tool_success(request_id=request_id, node=endpoint)
                except httpx.HTTPStatusError as e:
                    nodes_tried.append({"node": endpoint, "result": str(e)})
                except Exception as e:
                    logger.warning(
                        f"Failed to reach {endpoint} while cancelling "
                        f"{request_id!r}: {e}"
                    )
                    nodes_tried.append({"node": endpoint, "result": str(e)})

        return tool_error(
            f"Request {request_id!r} was not found on any of {len(endpoints)} "
            f"query node(s) — it may have already finished, or the ID may be "
            f"wrong. Confirm with get_active_queries.",
            request_id=request_id,
            nodes_tried=nodes_tried,
        )
    except ValueError as e:
        logger.warning(f"Rejected delete_active_query request: {e}")
        return tool_error(e, request_id=request_id)
    except Exception as e:
        logger.error(f"Error cancelling query {request_id!r}: {e}", exc_info=True)
        return tool_error(e, request_id=request_id)
