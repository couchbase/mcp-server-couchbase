"""
Unit tests for prepare_tools_for_registration — tool disabling and confirmation wrapping.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cb_mcp.core.spec import Deployment
from cb_mcp.servers.operational.spec import SPEC as OPERATIONAL_SPEC
from cb_mcp.tool_registration import prepare_tools_for_registration as _prepare
from cb_mcp.tool_registration import unsupported_for_deployment
from cb_mcp.tools.operational import TOOL_DEPLOYMENT_REQUIREMENTS
from cb_mcp.utils.constants import SCOPE_READ, SCOPE_WRITE


def prepare_tools_for_registration(**kwargs):
    """Register the operational server, which is what these tests exercise.

    The real function takes a ``ServerSpec`` first; these tests are about the
    gating and wrapping pipeline rather than about which server is registered,
    so the spec is supplied here instead of at every call site.
    """
    return _prepare(OPERATIONAL_SPEC, **kwargs)


class TestPrepareToolsDisabling:
    """Tests that disabled tools are excluded from the registered tool list."""

    def test_disabled_tool_excluded_from_final_list(self):
        """A named disabled tool should not appear in the returned tool list."""
        tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools="get_document_by_id",
            confirmation_required_tools=None,
        )
        tool_names = {t.__name__ for t in tools}
        assert "get_document_by_id" not in tool_names
        assert disabled == {"get_document_by_id"}

    def test_non_disabled_tools_remain(self):
        """Tools that are not disabled should still appear in the final list."""
        tools, _, _ = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools="get_document_by_id",
            confirmation_required_tools=None,
        )
        tool_names = {t.__name__ for t in tools}
        assert "get_buckets_in_cluster" in tool_names

    def test_no_disabled_tools(self):
        """Passing None for disabled_tools should leave all tools enabled."""
        _tools_all, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools=None,
            confirmation_required_tools=None,
        )
        assert disabled == set()


class TestPrepareToolsConfirmation:
    """Tests that confirmation-required tools are wrapped correctly."""

    def test_confirmation_tool_is_in_returned_set(self):
        """Specified confirmation tool should appear in the returned confirmed set."""
        _, confirmed, _ = prepare_tools_for_registration(
            read_only_mode=False,
            disabled_tools=None,
            confirmation_required_tools="delete_document_by_id",
        )
        assert "delete_document_by_id" in confirmed

    def test_confirmation_tool_preserves_name(self):
        """Wrapped confirmation tool should retain its original __name__."""
        tools, confirmed, _ = prepare_tools_for_registration(
            read_only_mode=False,
            disabled_tools=None,
            confirmation_required_tools="delete_document_by_id",
        )
        assert "delete_document_by_id" in confirmed
        delete_tool = next(t for t in tools if t.__name__ == "delete_document_by_id")
        assert delete_tool is not None

    def test_unavailable_confirmation_tool_skipped(self):
        """A confirmation tool excluded by read_only_mode should not appear in confirmed set."""
        # delete_document_by_id is a write tool, not loaded in read_only_mode
        _, confirmed, _ = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools=None,
            confirmation_required_tools="delete_document_by_id",
        )
        assert "delete_document_by_id" not in confirmed


class TestPrepareToolsScopeEnforcement:
    """Tests that enforce_scopes wires per-tool scope checks correctly."""

    def test_enforce_scopes_false_leaves_tools_unwrapped(self):
        """Without enforce_scopes, tools are not gated by the access token."""
        tools, _, _ = prepare_tools_for_registration(
            read_only_mode=False,
            disabled_tools=None,
            confirmation_required_tools=None,
            enforce_scopes=False,
        )
        get_doc = next(t for t in tools if t.__name__ == "get_document_by_id")
        # No scope wrapper installed → calling without a token must NOT
        # raise PermissionError on missing scopes. Without a real cluster
        # the call may error for other reasons; we only assert it isn't
        # the scope-check error.
        with patch(
            "cb_mcp.utils.scope_enforcement.get_access_token", return_value=None
        ):
            try:
                asyncio.run(get_doc(None, "b", "s", "c", "id"))
            except PermissionError as e:
                pytest.fail(f"unexpected scope-check rejection: {e}")
            except Exception:
                pass  # expected — no cluster context

    def test_enforce_scopes_true_rejects_write_token_for_read_tool(self):
        """A token with only SCOPE_WRITE must be denied at a read tool."""
        tools, _, _ = prepare_tools_for_registration(
            read_only_mode=False,
            disabled_tools=None,
            confirmation_required_tools=None,
            enforce_scopes=True,
        )
        get_doc = next(t for t in tools if t.__name__ == "get_document_by_id")
        token = SimpleNamespace(scopes=[SCOPE_WRITE])

        with (
            patch(
                "cb_mcp.utils.scope_enforcement.get_access_token", return_value=token
            ),
            pytest.raises(PermissionError),
        ):
            asyncio.run(get_doc(None, "b", "s", "c", "id"))

    def test_enforce_scopes_true_rejects_read_token_for_write_tool(self):
        """A token with only SCOPE_READ must be denied at a write tool."""
        tools, _, _ = prepare_tools_for_registration(
            read_only_mode=False,
            disabled_tools=None,
            confirmation_required_tools=None,
            enforce_scopes=True,
        )
        upsert = next(t for t in tools if t.__name__ == "upsert_document_by_id")
        token = SimpleNamespace(scopes=[SCOPE_READ])

        with (
            patch(
                "cb_mcp.utils.scope_enforcement.get_access_token", return_value=token
            ),
            pytest.raises(PermissionError),
        ):
            asyncio.run(upsert(None, "b", "s", "c", "id", {}))


class TestDisabledAndConfirmationOverlap:
    """Behavior when a tool is named in BOTH --disabled-tools and
    --confirmation-required-tools.
    """

    def test_disabled_tool_in_confirmation_list_is_dropped(self):
        """A tool that's both disabled and confirmation-required should end
        up disabled (not registered), and the confirmation wrapping should
        be silently skipped — disable wins."""
        tools, confirmed, disabled = prepare_tools_for_registration(
            read_only_mode=False,  # load all tools incl. write tools
            disabled_tools="delete_document_by_id",
            confirmation_required_tools="delete_document_by_id",
        )

        tool_names = {t.__name__ for t in tools}

        # The tool is not registered with the server — disable wins.
        assert "delete_document_by_id" not in tool_names

        # It's still in the user-supplied "configured" confirmation set
        # (we report what the user asked for, not what survived filtering).
        assert "delete_document_by_id" in confirmed

        # And it's in the disabled set.
        assert "delete_document_by_id" in disabled

    def test_disable_only_with_confirmation_on_sibling(self):
        """Disabling one tool while requiring confirmation on a different
        tool must leave the second tool registered AND wrapped — the
        precedence rule applies per-tool, not globally."""
        tools, confirmed, disabled = prepare_tools_for_registration(
            read_only_mode=False,
            disabled_tools="upsert_document_by_id",
            confirmation_required_tools="delete_document_by_id",
        )

        tool_names = {t.__name__ for t in tools}
        assert "upsert_document_by_id" not in tool_names  # disabled
        assert "delete_document_by_id" in tool_names  # still registered
        assert "upsert_document_by_id" in disabled
        assert "delete_document_by_id" in confirmed


def test_spec_wires_the_declared_deployment_requirements():
    """The mapping must actually reach the spec, as the same object.

    Declaring ``TOOL_DEPLOYMENT_REQUIREMENTS`` and wiring it into ``SPEC`` are
    two edits in two files, and the gate is silent when only the first is
    present: an unwired spec has empty requirements, so every invariant about
    them passes vacuously and every tool registers. A merge that touches
    ``servers/operational/spec.py`` is exactly where that loss would happen,
    which is why this asserts identity rather than equality.
    """
    assert OPERATIONAL_SPEC.deployment_requirements is TOOL_DEPLOYMENT_REQUIREMENTS
    assert OPERATIONAL_SPEC.deployment_resolver is not None
    assert TOOL_DEPLOYMENT_REQUIREMENTS, (
        "No tool declares a deployment requirement; the gate can never fire."
    )


#: Derived from the spec rather than spelled out, so adding a tool to
#: ``TOOL_DEPLOYMENT_REQUIREMENTS`` does not silently falsify these tests.
WITHHELD_ON_CAPELLA = {
    name
    for name, required in OPERATIONAL_SPEC.deployment_requirements.items()
    if required is not Deployment.CAPELLA
}


class TestDeploymentGating:
    """Tools the resolved deployment cannot support are not registered.

    ``get_cluster_metrics`` is the standing example: it calls the Management
    REST stats endpoint, which Capella does not expose.
    """

    def test_capella_withholds_on_prem_only_tool(self):
        tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools=None,
            confirmation_required_tools=None,
            deployment=Deployment.CAPELLA,
        )
        assert "get_cluster_metrics" not in {t.__name__ for t in tools}
        assert "get_cluster_metrics" in disabled

    def test_on_prem_keeps_on_prem_only_tool(self):
        tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools=None,
            confirmation_required_tools=None,
            deployment=Deployment.ON_PREM,
        )
        assert "get_cluster_metrics" in {t.__name__ for t in tools}
        assert disabled == set()

    def test_unresolved_deployment_withholds_nothing(self):
        """A host that cannot tell must not guess — the tool's own runtime
        check is what covers that case."""
        tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools=None,
            confirmation_required_tools=None,
            deployment=None,
        )
        assert "get_cluster_metrics" in {t.__name__ for t in tools}
        assert disabled == set()

    def test_deployment_is_optional(self):
        """Omitting the argument entirely behaves as before the gate existed."""
        tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools=None,
            confirmation_required_tools=None,
        )
        assert "get_cluster_metrics" in {t.__name__ for t in tools}
        assert disabled == set()

    def test_withheld_and_operator_disabled_are_reported_together(self):
        """Both are tools deliberately not registered; one set reports both."""
        _tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools="get_document_by_id",
            confirmation_required_tools=None,
            deployment=Deployment.CAPELLA,
        )
        assert disabled == WITHHELD_ON_CAPELLA | {"get_document_by_id"}

    def test_operator_may_still_name_a_withheld_tool(self):
        """Naming a tool the deployment also withholds is not an error, and
        must not double-count or warn about an unknown tool."""
        _tools, _, disabled = prepare_tools_for_registration(
            read_only_mode=True,
            disabled_tools="get_cluster_metrics",
            confirmation_required_tools=None,
            deployment=Deployment.CAPELLA,
        )
        assert disabled == WITHHELD_ON_CAPELLA


class TestUnsupportedForDeployment:
    """The filter itself, away from the registration pipeline."""

    def test_none_deployment_withholds_nothing(self):
        assert (
            unsupported_for_deployment({"a", "b"}, {"a": Deployment.ON_PREM}, None)
            == set()
        )

    def test_only_mismatched_requirements_are_returned(self):
        requirements = {"a": Deployment.ON_PREM, "b": Deployment.CAPELLA}
        assert unsupported_for_deployment(
            {"a", "b", "c"}, requirements, Deployment.CAPELLA
        ) == {"a"}

    def test_tools_not_loaded_are_not_reported(self):
        """A write tool already absent under read-only mode is not withheld a
        second time under a different reason."""
        assert (
            unsupported_for_deployment(
                {"b"}, {"a": Deployment.ON_PREM}, Deployment.CAPELLA
            )
            == set()
        )
