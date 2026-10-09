"""Accuracy tests for the Operational Insights async query lifecycle.

Covers:
  - oi_get_async_query_results
  - oi_discard_async_query_results
  - oi_cancel_async_query

These three are stateful in a way the other OI tools are not: each takes a
``query_handle`` that only exists after ``oi_run_query_async`` has actually
started a query on the server. So a case here cannot be a pure prompt — the
handle has to be minted first and then named in the prompt.

How that is done: the test body starts the query itself (via
``oi_run_query_async`` through ``call_tool_silent``, so starting it is not
scored as a model tool call), reads back the handle, and only then builds the
``AccuracyCase`` with that handle interpolated into the prompt. The case is
therefore constructed per-test rather than in a fixture.

The discriminating accuracy question here is discard-vs-cancel. Both free a
query by handle, and the tool docs draw the line by state: discard is for a
*finished* query's results, cancel is for one still *running*. The two cases
below differ only in which state the prompt describes, so a model that treats
the pair as interchangeable fails one of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import pytest

from accuracy.sdk import (
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    calculate_tool_calling_accuracy,
)
from accuracy.sdk.client import AccuracyTestingClient
from accuracy.sdk.runner import SetupHook
from accuracy.sdk.types import ExpectedToolCall, ModelResponse, TokensUsed

# Substituted into a prompt before the real handle is known. The seed replaces
# it, so a case that somehow ran unseeded would send this literal rather than
# a plausible-looking fake handle the model might act on.
_PLACEHOLDER = "<handle-not-seeded>"


@dataclass
class _Handle:
    """Mutable box holding the async query handle across hooks.

    The handle is minted by the seed hook and read again by the cleanup hook,
    which run at different points in the case's lifecycle, so it needs a
    carrier that both close over.
    """

    value: str = _PLACEHOLDER

    @property
    def seeded(self) -> bool:
        return self.value != _PLACEHOLDER


def _extract_handle(payload: Any) -> str | None:
    """Pull ``query_handle`` out of an MCP CallToolResult, however it arrives.

    ``call_tool_silent`` returns the raw result object rather than the
    serialized string the LLM path produces, so the handle may be in
    structured content or in a JSON text block.
    """
    structured = getattr(payload, "structuredContent", None)
    if isinstance(structured, dict):
        handle = structured.get("query_handle")
        if handle:
            return handle

    for block in getattr(payload, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("query_handle"):
            return data["query_handle"]
    return None


def _discard_handle(holder: _Handle) -> SetupHook:
    """Cleanup hook: free the query's results if they are still held.

    Best-effort by design — the runner already suppresses cleanup errors, and
    the handle may legitimately be gone (the model cancelled it, or the case
    discarded it itself).
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        if not holder.seeded:
            return
        await client.call_tool_silent(
            "oi_discard_async_query_results", {"query_handle": holder.value}
        )

    return _hook


async def _score_handle_case(
    *,
    test_id: str,
    prompt_template: str,
    expected_tools: list[ExpectedToolCall],
    statement: str,
    wait_for_ready: bool,
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    """Mint a handle, build the case around it, and score the model's calls.

    This does not go through ``run_accuracy_case``: that helper opens the
    session itself and reads ``case.prompt`` from an already-constructed
    case, but here the prompt cannot exist until a handle has been minted
    *inside* that same session (handles live in the server process's lifespan
    registry, so one from an earlier session is unknown to a later one).

    The sequence below is otherwise identical to the shared runner — seed,
    ``client.reset()`` so seeding is not scored, drive the agent, best-effort
    cleanup, score, persist — just with the prompt built in between.
    """
    holder = _Handle()

    async with accuracy_client() as client:
        start = await client.call_tool_silent(
            "oi_run_query_async", {"statement": statement}
        )
        handle = _extract_handle(start)
        if not handle:
            pytest.skip(
                "Could not start an async query to mint a query_handle "
                f"(statement: {statement!r})."
            )
        holder.value = handle

        if wait_for_ready:
            await _await_ready(client, holder.value)

        # Starting the query is setup, not a model decision.
        client.reset()
        tools = await client.openai_tools()
        prompt = prompt_template.replace(_PLACEHOLDER, holder.value)

        try:
            agent_result = await openai_agent.run(
                prompt, tools=tools, execute_tool=client.execute_tool
            )
        finally:
            with contextlib.suppress(Exception):
                await _discard_handle(holder)(client)

        actual_calls = client.llm_tool_calls()

    accuracy = calculate_tool_calling_accuracy(expected_tools, actual_calls)

    result_storage.save_model_response(
        run_id=accuracy_run_id,
        commit_sha=commit_sha,
        prompt=prompt,
        expected_tool_calls=expected_tools,
        model_response=ModelResponse(
            provider="OpenAI",
            requested_model=openai_model,
            responding_model=agent_result.responding_model,
            llm_response_time_ms=agent_result.elapsed_ms,
            tool_calling_accuracy=accuracy,
            llm_tool_calls=actual_calls,
            tokens_used=TokensUsed(
                prompt_tokens=agent_result.prompt_tokens,
                completion_tokens=agent_result.completion_tokens,
                total_tokens=agent_result.total_tokens,
            ),
            text=agent_result.text,
            messages=agent_result.messages,
        ),
    )

    assert accuracy >= 0.75, (
        f"Accuracy for case '{test_id}' was {accuracy}. "
        f"Expected: {expected_tools}. "
        f"Actual: {json.dumps([c.__dict__ for c in actual_calls], indent=2, default=str)}"
    )


async def _await_ready(
    client: AccuracyTestingClient, handle: str, attempts: int = 10
) -> None:
    """Poll until the async query reports ready, or give up quietly.

    Used by the discard case, which is only meaningful once the query has
    finished. Giving up quietly rather than failing is deliberate: a query
    that is still running makes the case weaker (discard would return
    ``discarded: false``) but it still scores *tool selection*, which is what
    this tier measures.
    """
    for _ in range(attempts):
        result = await client.call_tool_silent(
            "oi_get_async_query_results", {"query_handle": handle}
        )
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            with contextlib.suppress(ValueError, TypeError):
                if json.loads(text).get("ready") is True:
                    return
        await asyncio.sleep(0.5)


# A statement with enough rows to be worth polling, but cheap enough that the
# accuracy run is not waiting on the cluster. Uses a range generator so the
# case does not depend on any seeded collection existing.
_SLOW_STATEMENT = (
    "SELECT r AS n, r * 2 AS doubled FROM RANGE(1, 2000) AS r WHERE r % 7 = 0;"
)


@pytest.mark.asyncio
async def test_oi_get_async_query_results_accuracy(
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    await _score_handle_case(
        test_id="oi_get_async_query_results",
        prompt_template=(
            "I started an async SQL++ query earlier and its query handle is "
            f"'{_PLACEHOLDER}'. Check whether it has finished and, if it has, "
            "show me the rows it returned."
        ),
        expected_tools=[
            ExpectedToolCall(
                tool_name="oi_get_async_query_results",
                parameters={"query_handle": Matcher.string()},
            ),
        ],
        statement=_SLOW_STATEMENT,
        wait_for_ready=False,
        accuracy_client=accuracy_client,
        openai_agent=openai_agent,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )


@pytest.mark.asyncio
async def test_oi_discard_async_query_results_accuracy(
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    """Discard, not cancel: the prompt says the query has already finished."""
    await _score_handle_case(
        test_id="oi_discard_async_query_results",
        prompt_template=(
            f"My async query with handle '{_PLACEHOLDER}' has already finished "
            "and I've read everything I need from it. Free its results on the "
            "server — I'm done with them."
        ),
        expected_tools=[
            ExpectedToolCall(
                tool_name="oi_discard_async_query_results",
                parameters={"query_handle": Matcher.string()},
            ),
            # Checking status first is a reasonable way to confirm it really
            # has finished, so it is allowed but not required.
            ExpectedToolCall(
                tool_name="oi_get_async_query_results",
                parameters=Matcher.any_value(),
                optional=True,
            ),
        ],
        statement=_SLOW_STATEMENT,
        wait_for_ready=True,
        accuracy_client=accuracy_client,
        openai_agent=openai_agent,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )


@pytest.mark.asyncio
async def test_oi_cancel_async_query_accuracy(
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    """Cancel, not discard: the prompt says the query is still running.

    The mirror image of the discard case. Both free a query by handle, and
    only the described state distinguishes them.
    """
    await _score_handle_case(
        test_id="oi_cancel_async_query",
        prompt_template=(
            f"The async query with handle '{_PLACEHOLDER}' is still running and "
            "I don't want to wait for it any more. Stop it."
        ),
        expected_tools=[
            ExpectedToolCall(
                tool_name="oi_cancel_async_query",
                parameters={"query_handle": Matcher.string()},
            ),
        ],
        statement=_SLOW_STATEMENT,
        wait_for_ready=False,
        accuracy_client=accuracy_client,
        openai_agent=openai_agent,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )
