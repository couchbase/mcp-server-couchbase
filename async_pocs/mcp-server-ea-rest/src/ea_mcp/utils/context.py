"""Lifespan-scoped context for the stateless EA MCP server.

Unlike the SDK-based server, there is NO handle registry here. The only
server-side object is a shared HTTP client (a connection pool). It holds no
per-query state, so nothing is lost across restarts and nothing needs sharing
across replicas — query identity travels entirely in the strings returned to
and passed back by the client.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context

from .ea_rest_client import EARestClient


@dataclass
class AppContext:
    ea_client: EARestClient | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)


def get_ea_client(ctx: Context) -> EARestClient:
    """Return the shared EA REST client for this request."""
    app: AppContext = ctx.request_context.lifespan_context  # type: ignore[assignment]
    if app.ea_client is None:
        raise RuntimeError(
            "EA REST client not initialized. The lifespan must populate "
            "AppContext.ea_client before tools run."
        )
    return app.ea_client
