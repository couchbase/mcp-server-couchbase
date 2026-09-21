"""Every settings key must be classified for the diagnostic record.

``_redacted_settings`` works from an allow-list: a key that is in neither the
safe list nor the presence-only list is **dropped silently** — no error, no
warning, nothing in any other test. That default is right for a record which
ends up in customer-shared support bundles, but it means the failure mode of
adding a setting is "the field quietly stops appearing in diagnostics", which
is exactly the kind of gap nobody notices until a support case needs it.

This module closes that by asserting the two sets agree: whatever the host puts
into ``settings``, the redaction policy has an opinion about. Parameterized
over every server this distribution ships, so a second server's settings are
covered by the same guarantee.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import couchbase
import pytest
from click.testing import CliRunner

import mcp_server
from cb_mcp.servers.operational.spec import SPEC as OPERATIONAL_SPEC
from cb_mcp.servers.operational_insights.spec import SPEC as OI_SPEC
from cb_mcp.utils.environment import (
    _redacted_settings,
    presence_only_keys_for,
    safe_keys_for,
)

# (argv, spec) for every server this distribution ships. [] invokes the
# DefaultGroup's default command (operational); a real subcommand name
# invokes the others explicitly.
SERVERS = [
    pytest.param([], OPERATIONAL_SPEC, id="operational"),
    pytest.param(["operational-insights"], OI_SPEC, id="operational-insights"),
]

# Keys the host puts into settings that are deliberately *not* reported.
# Listing them here is the opt-out: it forces the omission to be a decision
# somebody wrote down, rather than an oversight. Keyed by server id.
DELIBERATELY_UNREPORTED: dict[str, set[str]] = {
    "operational": set(),
    "operational-insights": set(),
}


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    # Only the operational server's sdk_log_hook calls a "once per process"
    # SDK entry point (couchbase.configure_logging). The OI server's hook
    # (bridge_sdk_logging) only rewires stdlib logging objects and is safe to
    # call repeatedly, so it needs no equivalent patch here.
    with patch.object(couchbase, "configure_logging"):
        yield


def _host_settings(argv: list[str]) -> dict:
    """The real settings dict, as the CLI builds it for ``argv``."""
    captured: dict = {}

    def capture(*_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        return MagicMock()

    with (
        patch("cb_mcp.core.app.FastMCP", side_effect=capture),
        patch("mcp_server.run_app"),
    ):
        result = CliRunner().invoke(mcp_server.main, argv, catch_exceptions=False)
    assert result.exit_code == 0, result.output

    out: dict = {}

    async def drive():
        async with captured["lifespan"](MagicMock()) as ctx:
            out.update(ctx.settings)

    asyncio.run(drive())
    return out


@pytest.mark.parametrize(("argv", "spec"), SERVERS)
def test_every_settings_key_is_classified(argv, spec):
    """No host setting may fall through the allow-list unnoticed."""
    settings = _host_settings(argv)
    classified = set(safe_keys_for(spec)) | set(presence_only_keys_for(spec))
    unclassified = set(settings) - classified - DELIBERATELY_UNREPORTED[spec.id]
    assert not unclassified, (
        f"settings keys {sorted(unclassified)} are in neither the safe nor the "
        "presence-only list, so they are silently dropped from the env-info "
        "record. Classify them on the ServerSpec, or add them to "
        "DELIBERATELY_UNREPORTED with a reason."
    )


@pytest.mark.parametrize(("argv", "spec"), SERVERS)
def test_classification_lists_do_not_overlap(argv, spec):
    """A key in both lists would be emitted twice, once with its value."""
    overlap = set(safe_keys_for(spec)) & set(presence_only_keys_for(spec))
    assert not overlap, (
        f"{sorted(overlap)} appear in both lists; the presence-only entry would "
        "not prevent the raw value being logged by the safe entry"
    )


@pytest.mark.parametrize(("argv", "spec"), SERVERS)
def test_spec_contributions_do_not_duplicate_the_shared_lists(argv, spec):
    """A server should declare only what is specific to it."""
    shared_safe = set(safe_keys_for(None))
    shared_secret = set(presence_only_keys_for(None))
    assert not (set(spec.safe_settings_keys) & shared_safe)
    assert not (set(spec.secret_settings_keys) & shared_secret)


@pytest.mark.parametrize(("argv", "spec"), SERVERS)
def test_server_id_is_not_a_setting(argv, spec):
    """``server_id`` is server identity, not operator configuration.

    It lives on ``AppContext`` (populated from the spec by ``build_app``) so
    that any host gets it, not only one that happens to build the same settings
    dict as this CLI. Keeping it out of ``settings`` is what makes that true.
    """
    assert "server_id" not in _host_settings(argv)
    assert "server_id" not in safe_keys_for(spec)
    assert "server_id" not in presence_only_keys_for(spec)


@pytest.mark.parametrize(("argv", "spec"), SERVERS)
def test_secret_values_never_appear_verbatim(argv, spec):
    """End-to-end check on the real settings shape, not a synthetic one.

    ``password`` is a shared presence-only key every server has; the rest of
    the injected payload comes from the spec's own ``secret_settings_keys``,
    so this exercises exactly what that server declares as secret.
    """
    settings = dict(_host_settings(argv))
    secret_payload = {"password": "hunter2"}
    secret_payload.update(
        {key: f"/etc/ssl/{key}" for key in spec.secret_settings_keys}
    )
    settings.update(secret_payload)
    out = _redacted_settings(settings, spec)
    rendered = repr(out)
    for secret in secret_payload.values():
        assert secret not in rendered
    assert out["password_configured"] is True
    for key in spec.secret_settings_keys:
        assert out[f"{key}_configured"] is True
