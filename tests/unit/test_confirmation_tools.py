"""Tests for confirmation-required tool behavior and related metadata.

Coverage map:
- Default confirmation-required configuration values and parsing.
- Generic tool-name parsing behavior used by disabled/confirmation tool lists.
- Tool annotations contract (read-only/destructive/idempotent/open-world hints).
- Confirmation schema + confirmation prompt message formatting.
- Confirmation wrapper behavior across:
  - user actions (accept/decline/cancel)
  - capability detection and fallback when elicitation is not advertised
  - fail-closed behavior for unexpected runtime errors
  - positional argument forwarding compatibility
"""

from types import SimpleNamespace

import pytest
from fastmcp import Context

from cb_mcp.tools import TOOL_ANNOTATIONS, get_tools
from cb_mcp.utils.config import parse_tool_names
from cb_mcp.utils.elicitation import (
    ConfirmationResult,
    _build_confirmation_message,
    wrap_with_confirmation,
)

VALID_TOOL_NAMES = {tool.__name__ for tool in get_tools(read_only_mode=False)}


class TestDefaultConfirmationRequiredTools:
    """Tests for default confirmation-required tools configuration."""

    def test_no_default_confirmation_tools(self):
        """No tools should require confirmation by default."""
        result = parse_tool_names(None, VALID_TOOL_NAMES)
        assert result == set()


class TestToolAnnotations:
    """Tests for tool annotations mapping."""

    def test_read_only_tools_have_read_only_hint(self):
        """All read-only tools should advertise readOnlyHint=True."""
        read_only_tool_names = [
            "get_server_configuration_status",
            "test_cluster_connection",
            "get_buckets_in_cluster",
            "get_scopes_and_collections_in_bucket",
            "get_collections_in_scope",
            "get_scopes_in_bucket",
            "get_cluster_health_and_services",
            "get_document_by_id",
            "get_schema_for_collection",
            "get_index_advisor_recommendations",
            "list_indexes",
            "get_longest_running_queries",
            "get_most_frequent_queries",
            "get_queries_with_largest_response_sizes",
            "get_queries_with_large_result_count",
            "get_queries_using_primary_index",
            "get_queries_not_using_covering_index",
            "get_queries_not_selective",
        ]
        for tool_name in read_only_tool_names:
            assert tool_name in TOOL_ANNOTATIONS, (
                f"{tool_name} missing from annotations"
            )
            assert TOOL_ANNOTATIONS[tool_name].readOnlyHint is True, (
                f"{tool_name} should have readOnlyHint=True"
            )

    def test_delete_tool_has_destructive_hint(self):
        """Delete tool should advertise destructive behavior."""
        assert "delete_document_by_id" in TOOL_ANNOTATIONS
        assert TOOL_ANNOTATIONS["delete_document_by_id"].destructiveHint is True

    def test_sql_query_tool_is_not_open_world(self):
        """SQL query tool operates on a closed Couchbase cluster, not the open web."""
        assert "run_sql_plus_plus_query" in TOOL_ANNOTATIONS
        assert TOOL_ANNOTATIONS["run_sql_plus_plus_query"].openWorldHint is not True

    def test_upsert_and_replace_are_idempotent(self):
        """Idempotent write tools should advertise idempotentHint=True."""
        for tool_name in ["upsert_document_by_id", "replace_document_by_id"]:
            assert tool_name in TOOL_ANNOTATIONS
            assert TOOL_ANNOTATIONS[tool_name].idempotentHint is True

    def test_all_registered_tools_have_annotations(self):
        """Every tool returned by get_tools should have annotations."""
        all_tools = get_tools(read_only_mode=False)
        for tool in all_tools:
            assert tool.__name__ in TOOL_ANNOTATIONS, (
                f"Tool '{tool.__name__}' is missing from TOOL_ANNOTATIONS"
            )


class TestConfirmationResult:
    """Tests for the ConfirmationResult schema."""

    def test_default_value_is_true(self):
        """Confirmation model defaults to confirm=True."""
        result = ConfirmationResult()
        assert result.confirm is True

    def test_can_set_to_false(self):
        """Confirmation model should accept explicit confirm=False."""
        result = ConfirmationResult(confirm=False)
        assert result.confirm is False

    def test_schema_has_title(self):
        """Generated schema should include confirm property metadata."""
        schema = ConfirmationResult.model_json_schema()
        assert "properties" in schema
        assert "confirm" in schema["properties"]


class TestBuildConfirmationMessage:
    """Tests for the confirmation message builder."""

    def test_basic_message(self):
        """Base message should include tool name."""
        msg = _build_confirmation_message("delete_document_by_id", {})
        assert "delete_document_by_id" in msg

    def test_message_with_identifiers(self):
        """Message should include common identifier fields when present."""
        msg = _build_confirmation_message(
            "delete_document_by_id",
            {
                "bucket_name": "test-bucket",
                "scope_name": "_default",
                "collection_name": "users",
                "document_id": "doc123",
            },
        )
        assert "test-bucket" in msg
        assert "doc123" in msg
        assert "delete_document_by_id" in msg

    def test_message_with_partial_identifiers(self):
        """Message should include whichever identifiers are available."""
        msg = _build_confirmation_message(
            "delete_document_by_id",
            {"bucket_name": "mybucket"},
        )
        assert "mybucket" in msg


class TestWrapWithConfirmation:
    """High-level behavioral tests for confirmation gating.

    Coverage in this suite:
    - User decision outcomes: decline/cancel block execution.
    - Accept paths: confirm=False blocks, confirm=True executes.
    - Capability/error behavior: no elicitation support falls back to execution,
      while elicitation runtime failures fail closed.
    - Invocation compatibility: wrapper supports positional argument forwarding.
    """

    @staticmethod
    def _make_context(*, supports_elicitation: bool, elicit_callback):
        """Build a minimal fake Context/session pair for wrapper tests."""

        class FakeSession:
            def __init__(self, supports):
                self.supports = supports

            def check_client_capability(self, capability):
                return self.supports

        class FakeContext:
            def __init__(self):
                self.request_context = SimpleNamespace(
                    lifespan_context=SimpleNamespace(),
                    session=FakeSession(supports_elicitation),
                )

            async def elicit(self, message, schema):
                return await elicit_callback(message, schema)

        return FakeContext()

    @pytest.mark.asyncio
    async def test_decline_raises_permission_error(self):
        """Decline action must block execution with PermissionError."""

        def sample_tool(
            ctx: Context,
        ) -> bool:  # pragma: no cover - function under wrapper
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def decline_elicit(message, schema):
            return SimpleNamespace(action="decline")

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=decline_elicit,
        )

        with pytest.raises(
            PermissionError,
            match="was not confirmed by the user",
        ):
            await wrapped(ctx=fake_ctx)

    @pytest.mark.asyncio
    async def test_cancel_raises_permission_error(self):
        """Cancel action must block execution with PermissionError."""

        def sample_tool(
            ctx: Context,
        ) -> bool:  # pragma: no cover - function under wrapper
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def cancel_elicit(message, schema):
            return SimpleNamespace(action="cancel")

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=cancel_elicit,
        )

        with pytest.raises(
            PermissionError,
            match="was not confirmed by the user",
        ):
            await wrapped(ctx=fake_ctx)

    @pytest.mark.asyncio
    async def test_accept_with_confirm_false_raises_permission_error(self):
        """Accept action with confirm=False must still block execution."""

        def sample_tool(
            ctx: Context,
        ) -> bool:  # pragma: no cover - function under wrapper
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def reject_confirm_elicit(message, schema):
            return SimpleNamespace(
                action="accept",
                data=SimpleNamespace(confirm=False),
            )

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=reject_confirm_elicit,
        )

        with pytest.raises(PermissionError):
            await wrapped(ctx=fake_ctx)

    @pytest.mark.asyncio
    async def test_accept_with_confirm_true_executes_tool(self):
        """Accept action with confirm=True should allow tool execution."""
        called = False

        def sample_tool(
            ctx: Context,
        ) -> bool:  # pragma: no cover - function under wrapper
            nonlocal called
            called = True
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def accept_elicit(message, schema):
            return SimpleNamespace(
                action="accept",
                data=SimpleNamespace(confirm=True),
            )

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=accept_elicit,
        )

        result = await wrapped(ctx=fake_ctx)
        assert result is True
        assert called is True

    @pytest.mark.asyncio
    async def test_client_without_elicitation_support_falls_back_to_execution(self):
        """If client does not advertise elicitation, tool should still execute."""

        def sample_tool(
            ctx: Context,
        ) -> bool:  # pragma: no cover - function under wrapper
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def should_not_be_called_elicit(message, schema):
            raise AssertionError("elicit should not be called without client support")

        fake_ctx = self._make_context(
            supports_elicitation=False,
            elicit_callback=should_not_be_called_elicit,
        )

        result = await wrapped(ctx=fake_ctx)
        assert result is True

    @pytest.mark.asyncio
    async def test_unexpected_elicitation_error_blocks_execution(self):
        """Unexpected elicitation failures should fail closed (raise)."""

        def sample_tool(
            ctx: Context,
        ) -> bool:  # pragma: no cover - function under wrapper
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def transient_error_elicit(message, schema):
            raise RuntimeError("Transient elicitation failure")

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=transient_error_elicit,
        )

        with pytest.raises(RuntimeError, match="Transient elicitation failure"):
            await wrapped(ctx=fake_ctx)

    @pytest.mark.asyncio
    async def test_positional_arguments_are_supported(self):
        """Wrapper should preserve positional argument passing semantics."""
        captured_document_id = None

        def sample_tool(
            ctx: Context,
            document_id: str,
        ) -> bool:  # pragma: no cover - function under wrapper
            nonlocal captured_document_id
            captured_document_id = document_id
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        async def accept_elicit(message, schema):
            return SimpleNamespace(
                action="accept",
                data=SimpleNamespace(confirm=True),
            )

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=accept_elicit,
        )

        result = await wrapped(fake_ctx, "doc-123")
        assert result is True
        assert captured_document_id == "doc-123"

    @pytest.mark.asyncio
    async def test_missing_session_falls_back_to_execution(self):
        """When the request_context has no session, the wrapper should
        treat the client as elicitation-incapable and execute the tool
        without prompting."""

        def sample_tool(ctx: Context) -> bool:
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        # Build a context whose request_context has NO `session` attribute.
        class FakeContext:
            def __init__(self):
                self.request_context = SimpleNamespace(
                    lifespan_context=SimpleNamespace(),
                    session=None,
                )

            async def elicit(self, message, schema):
                raise AssertionError(
                    "elicit must not be called when session is None"
                )

        result = await wrapped(ctx=FakeContext())
        assert result is True

    @pytest.mark.asyncio
    async def test_session_without_capability_check_falls_back(self):
        """A session object that lacks check_client_capability should be
        treated as not advertising elicitation support."""

        async def sample_tool(ctx: Context) -> bool:
            return True

        wrapped = wrap_with_confirmation(sample_tool)

        # Session present but missing the capability-check method.
        class FakeContext:
            def __init__(self):
                self.request_context = SimpleNamespace(
                    lifespan_context=SimpleNamespace(),
                    session=SimpleNamespace(),  # no check_client_capability
                )

            async def elicit(self, message, schema):
                raise AssertionError(
                    "elicit must not be called when capability check is unavailable"
                )

        result = await wrapped(ctx=FakeContext())
        assert result is True

    @pytest.mark.asyncio
    async def test_async_tool_is_awaited(self):
        """Async tool functions should be awaited by the wrapper, not just called."""
        called = False

        async def async_sample_tool(ctx: Context) -> bool:
            nonlocal called
            called = True
            return True

        wrapped = wrap_with_confirmation(async_sample_tool)

        async def accept_elicit(message, schema):
            return SimpleNamespace(
                action="accept",
                data=SimpleNamespace(confirm=True),
            )

        fake_ctx = self._make_context(
            supports_elicitation=True,
            elicit_callback=accept_elicit,
        )

        result = await wrapped(ctx=fake_ctx)
        assert result is True
        assert called is True
