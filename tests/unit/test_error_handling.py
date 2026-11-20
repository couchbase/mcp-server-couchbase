"""Tests for the tool error-handling wrapper and its place in registration."""

import asyncio
import inspect

import pytest
from _all_specs import ALL_SPECS
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from cb_mcp.tool_registration import prepare_tools_for_registration
from cb_mcp.utils.constants import MAX_ERROR_MESSAGE_LENGTH
from cb_mcp.utils.error_handling import wrap_with_error_handling


def sync_tool(ctx: Context, bucket_name: str, limit: int = 1) -> dict:
    """Sync tool."""
    raise RuntimeError("boom " + "x" * 5_000)


async def async_tool(ctx: Context, bucket_name: str) -> dict:
    """Async tool."""
    raise KeyError("missing")


class TestWrapper:
    @pytest.mark.asyncio
    async def test_sync_failure_becomes_bounded_tool_error(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await wrap_with_error_handling(sync_tool)(None, "b")
        assert len(str(exc_info.value)) == MAX_ERROR_MESSAGE_LENGTH
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    @pytest.mark.asyncio
    async def test_async_failure_becomes_tool_error(self) -> None:
        with pytest.raises(ToolError, match="missing") as exc_info:
            await wrap_with_error_handling(async_tool)(None, "b")
        assert isinstance(exc_info.value.__cause__, KeyError)

    @pytest.mark.asyncio
    async def test_a_message_the_tool_framed_is_capped_too(self) -> None:
        """No message passes the limit, whoever worded it."""

        def tool() -> None:
            raise ValueError("framed by the tool " + "z" * 5_000)

        with pytest.raises(ToolError) as exc_info:
            await wrap_with_error_handling(tool)()
        assert str(exc_info.value).startswith("framed by the tool ")
        assert str(exc_info.value).endswith("... [truncated]")
        assert len(str(exc_info.value)) == MAX_ERROR_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_a_tool_raised_tool_error_is_capped_too(self) -> None:
        def tool() -> None:
            raise ToolError("w" * 5_000)

        with pytest.raises(ToolError) as exc_info:
            await wrap_with_error_handling(tool)()
        assert len(str(exc_info.value)) == MAX_ERROR_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_short_message_is_untouched(self) -> None:
        def tool() -> None:
            raise ValueError("limit must be positive")

        with pytest.raises(ToolError) as exc_info:
            await wrap_with_error_handling(tool)()
        assert str(exc_info.value) == "limit must be positive"

    @pytest.mark.asyncio
    async def test_success_returns_the_tool_result(self) -> None:
        def tool(n: int) -> dict:
            return {"n": n}

        assert await wrap_with_error_handling(tool)(n=3) == {"n": 3}

    @pytest.mark.asyncio
    async def test_cancellation_is_not_converted(self) -> None:
        async def tool() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await wrap_with_error_handling(tool)()

    def test_signature_is_preserved_for_schema_generation(self) -> None:
        assert inspect.signature(wrap_with_error_handling(sync_tool)) == (
            inspect.signature(sync_tool)
        )


class TestThroughFastMCP:
    @pytest.mark.asyncio
    async def test_client_sees_a_bounded_error_result_and_the_same_schema(
        self,
    ) -> None:
        raw, wrapped = FastMCP("raw"), FastMCP("wrapped")
        raw.tool(sync_tool)
        wrapped.tool(wrap_with_error_handling(sync_tool))

        async with Client(raw) as raw_client, Client(wrapped) as wrapped_client:
            raw_schema = (await raw_client.list_tools())[0].inputSchema
            wrapped_schema = (await wrapped_client.list_tools())[0].inputSchema
            result = await wrapped_client.call_tool(
                "sync_tool", {"bucket_name": "b"}, raise_on_error=False
            )

        assert wrapped_schema == raw_schema
        assert "ctx" not in wrapped_schema["properties"]
        assert result.is_error
        assert len(result.content[0].text) == MAX_ERROR_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_context_is_injected_into_the_wrapped_tool(self) -> None:
        def tool(ctx: Context, bucket_name: str) -> dict:
            """Reports whether it received a context."""
            return {"has_ctx": ctx is not None}

        wrapped = FastMCP("w")
        wrapped.tool(wrap_with_error_handling(tool))
        async with Client(wrapped) as client:
            result = await client.call_tool("tool", {"bucket_name": "b"})
        assert result.data == {"has_ctx": True}


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.id)
def test_registration_wraps_every_tool(spec) -> None:
    tools, _, _ = prepare_tools_for_registration(
        spec,
        read_only_mode=False,
        disabled_tools=None,
        confirmation_required_tools=None,
    )
    for tool in tools:
        assert inspect.iscoroutinefunction(tool), tool.__name__
        assert getattr(tool, "__wrapped__", None) is not None, tool.__name__
