"""Every settings key must be classified for the diagnostic record.

``_redacted_settings`` works from an allow-list: a key that is in neither the
safe list nor the presence-only list is **dropped silently** — no error, no
warning, nothing in any other test. That default is right for a record which
ends up in customer-shared support bundles, but it means the failure mode of
adding a setting is "the field quietly stops appearing in diagnostics", which
is exactly the kind of gap nobody notices until a support case needs it.

This module closes that by asserting the two sets agree: whatever the host puts
into ``settings``, the redaction policy has an opinion about.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import couchbase
import pytest
from click.testing import CliRunner

import mcp_server
from cb_mcp.servers.operational import SPEC
from cb_mcp.utils.environment import (
    _redacted_settings,
    presence_only_keys_for,
    safe_keys_for,
)

# Keys the host puts into settings that are deliberately *not* reported.
# Listing them here is the opt-out: it forces the omission to be a decision
# somebody wrote down, rather than an oversight.
DELIBERATELY_UNREPORTED: set[str] = set()


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    with patch.object(couchbase, "configure_logging"):
        yield


def _host_settings() -> dict:
    """The real settings dict, as the CLI builds it."""
    captured: dict = {}

    def capture(*_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        return MagicMock()

    with (
        patch("cb_mcp.core.app.FastMCP", side_effect=capture),
        patch("mcp_server.run_app"),
    ):
        result = CliRunner().invoke(mcp_server.main, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    out: dict = {}

    async def drive():
        async with captured["lifespan"](MagicMock()) as ctx:
            out.update(ctx.settings)

    asyncio.run(drive())
    return out


def test_every_settings_key_is_classified():
    """No host setting may fall through the allow-list unnoticed."""
    settings = _host_settings()
    classified = set(safe_keys_for(SPEC)) | set(presence_only_keys_for(SPEC))
    unclassified = set(settings) - classified - DELIBERATELY_UNREPORTED
    assert not unclassified, (
        f"settings keys {sorted(unclassified)} are in neither the safe nor the "
        "presence-only list, so they are silently dropped from the env-info "
        "record. Classify them on the ServerSpec, or add them to "
        "DELIBERATELY_UNREPORTED with a reason."
    )


def test_classification_lists_do_not_overlap():
    """A key in both lists would be emitted twice, once with its value."""
    overlap = set(safe_keys_for(SPEC)) & set(presence_only_keys_for(SPEC))
    assert not overlap, (
        f"{sorted(overlap)} appear in both lists; the presence-only entry would "
        "not prevent the raw value being logged by the safe entry"
    )


def test_spec_contributions_do_not_duplicate_the_shared_lists():
    """A server should declare only what is specific to it."""
    shared_safe = set(safe_keys_for(None))
    shared_secret = set(presence_only_keys_for(None))
    assert not (set(SPEC.safe_settings_keys) & shared_safe)
    assert not (set(SPEC.secret_settings_keys) & shared_secret)


def test_server_id_is_not_a_setting():
    """``server_id`` is server identity, not operator configuration.

    It lives on ``AppContext`` (populated from the spec by ``build_app``) so
    that any host gets it, not only one that happens to build the same settings
    dict as this CLI. Keeping it out of ``settings`` is what makes that true.
    """
    assert "server_id" not in _host_settings()
    assert "server_id" not in safe_keys_for(SPEC)
    assert "server_id" not in presence_only_keys_for(SPEC)


def test_secret_values_never_appear_verbatim():
    """End-to-end check on the real settings shape, not a synthetic one."""
    settings = dict(_host_settings())
    settings.update(
        {
            "password": "hunter2",
            "client_cert_path": "/etc/ssl/client.pem",
            "client_key_path": "/etc/ssl/client.key",
        }
    )
    out = _redacted_settings(settings, SPEC)
    rendered = repr(out)
    for secret in ("hunter2", "/etc/ssl/client.pem", "/etc/ssl/client.key"):
        assert secret not in rendered
    assert out["password_configured"] is True
    assert out["client_cert_path_configured"] is True
