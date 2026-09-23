"""The one tool every server exposes, whatever it is backed by.

A module rather than a subpackage, and sitting beside ``operational/`` and
``operational_insights/`` rather than inside either, because its body names
no SDK: it reads the lifespan context (settings, server identity, logging
snapshot) and the ``ProviderLifecycle`` half of the provider contract, which
is exactly the surface ``cb_mcp.core.contracts`` defines as service-agnostic.

Why it is shared at all: ``get_server_configuration_status`` is the
first-line support tool — what read-only mode, disabled tools, OAuth state
and logging actually resolved to, answerable without a cluster. It lived in
``tools/operational/server.py`` because that was the only server. The
consequence once a second arrived was concrete, not cosmetic: the
Operational Insights server had no way to report its own configuration, and
``OperationalInsightsClusterProvider.get_configuration`` / ``is_connected``
were dead code — implemented to satisfy the contract, called by nothing.

Registered by both specs as the *same function object*, which
``tests/unit/test_server_specs.py`` asserts. That is what distinguishes it
from the four names the two servers genuinely collide on
(``create_index`` and friends), where each server has its own
implementation.
"""

from typing import Any

from fastmcp import Context

from ..utils.config import get_settings
from ..utils.context import (
    get_cluster_provider,
    get_logging_config,
    get_server_id,
    get_server_name,
)


def get_server_configuration_status(ctx: Context) -> dict[str, Any]:
    """Get the server status and configuration without establishing connection.
    This tool can be used to verify if the server is running and check the configuration.
    """
    settings = get_settings(ctx)
    provider = get_cluster_provider(ctx)

    provider_config = provider.get_configuration(ctx) if provider is not None else {}

    # Server-level keys are spread last so they always reflect what the server
    # actually enforces, even if a provider returns overlapping keys.
    configuration = {
        **provider_config,
        "read_only_mode": settings.get("read_only_mode", True),
        "disabled_tools": sorted(settings.get("disabled_tools", set())),
        "confirmation_required_tools": sorted(
            settings.get("confirmation_required_tools", set())
        ),
        # OAuth resource-server config (non-secret IdP coordinates). Mirrors
        # the env-info diagnostic record so the log file and this tool agree on
        # which OAuth state is exposed. ``oauth_enabled`` reflects whether OAuth
        # is actually active, not merely configured.
        "oauth_enabled": settings.get("oauth_enabled", False),
        "oauth_jwks_uri": settings.get("oauth_jwks_uri"),
        "oauth_issuer": settings.get("oauth_issuer"),
        "oauth_audience": settings.get("oauth_audience"),
        "oauth_algorithm": settings.get("oauth_algorithm"),
        "oauth_mcp_base_url": settings.get("oauth_mcp_base_url"),
        "oauth_scope_read_label": settings.get("oauth_scope_read_label"),
        "oauth_scope_write_label": settings.get("oauth_scope_write_label"),
    }

    connection_status = {
        "cluster_connected": (
            provider.is_connected(ctx) if provider is not None else False
        ),
    }

    # Surface the active logging configuration as provided by the server
    # entrypoint via the lifespan context. Falls back to ``None`` for
    # implementations that don't populate it.
    logging_status = get_logging_config(ctx)

    return {
        "server_name": get_server_name(ctx),
        "server_id": get_server_id(ctx),
        "status": "running",
        "configuration": configuration,
        "logging": logging_status,
        "connections": connection_status,
    }
