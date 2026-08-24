"""Cluster access for the Tasks-based EA MCP server.

Important difference from the other servers
-------------------------------------------
A task-enabled tool runs inside a **docket worker**, which is detached from the
MCP request: ``ctx.request_context`` is ``None`` there, so the usual
``ctx.request_context.lifespan_context`` path (used by the other servers) is not
available inside a task.

So instead of resolving the cluster through the request context, we hold it in a
**process-level lazy singleton** built from environment settings. The docket
worker runs in the same process as the server, so this singleton is reachable
from both the (rare) inline path and the task-worker path. Settings come from
the environment, which is also what the CLI reads, so the two agree.
"""

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from couchbase_analytics.cluster import Cluster

from .connection import connect_to_ea_cluster

_cluster: Cluster | None = None
_lock = threading.Lock()


@dataclass
class AppContext:
    """Lifespan context. Kept for symmetry with the other servers; the cluster
    itself is resolved via the process-level singleton so it also works from
    detached task workers."""

    cluster_provider: Any = None
    settings: Mapping[str, Any] = field(default_factory=dict)


def _settings_from_env() -> dict[str, str]:
    return {
        "endpoint": os.environ.get("EA_ENDPOINT", "http://localhost:9095"),
        "username": os.environ.get("EA_USERNAME", ""),
        "password": os.environ.get("EA_PASSWORD", ""),
    }


def get_cluster_connection(ctx: Any = None) -> Cluster:
    """Return the shared EA cluster, connecting lazily on first use.

    Works both inline and inside a detached task worker: it does not depend on
    ``ctx`` (which is ``None`` in a task worker) — it uses a process-level
    singleton built from environment settings.
    """
    global _cluster
    if _cluster is not None:
        return _cluster
    with _lock:
        if _cluster is None:
            s = _settings_from_env()
            _cluster = connect_to_ea_cluster(
                s["endpoint"], s["username"], s["password"]
            )
    return _cluster


def close_cluster() -> None:
    global _cluster
    if _cluster is not None:
        try:
            _cluster.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _cluster = None
