"""Shared driver that executes one accuracy case end-to-end.

Every tool-family test file builds a list of ``AccuracyCase`` objects and
hands them to ``run_accuracy_case``. The runner:

  1. Opens a fresh MCP session (via the supplied async context-manager
     factory) so anyio task groups enter/exit on the same Task.
  2. Runs optional ``seed`` and (always) ``cleanup`` hooks via
     ``call_tool_silent`` so they don't pollute the LLM tool-call log.
  3. Lists tools, drives the OpenAI agent, captures the LLM tool calls.
  4. Scores the calls and persists the model response to disk.

Tests then assert on the returned score.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from .agent import AgentResult, OpenAIAgent
from .client import AccuracyTestingClient, MockedToolFn
from .judge import JudgeVerdict, LLMJudge
from .result_storage import DiskResultStorage
from .scorer import calculate_tool_calling_accuracy
from .types import ExpectedToolCall, ModelResponse, TokensUsed

SetupHook = Callable[[AccuracyTestingClient], Awaitable[None]]


def extract_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull the tool outputs out of an agent transcript.

    Walks the OpenAI message list, mapping each ``role == "tool"`` message
    back to the tool name from the preceding assistant ``tool_calls`` entry
    (matched by ``tool_call_id``). Returns ``[{tool_name, content}, ...]`` in
    call order — exactly what the judge needs to verify grounding.
    """
    id_to_name: dict[str, str] = {}
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id")
            name = (tc.get("function") or {}).get("name")
            if tc_id and name:
                id_to_name[tc_id] = name

    results: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        results.append(
            {
                "tool_name": id_to_name.get(msg.get("tool_call_id", ""), "unknown"),
                "content": msg.get("content", ""),
            }
        )
    return results


@dataclass
class AccuracyCase:
    """A single accuracy test case, decoupled from any tool family."""

    test_id: str
    prompt: str
    expected_tools: list[ExpectedToolCall]
    seed: SetupHook | None = None
    cleanup: SetupHook | None = None
    mocks: dict[str, MockedToolFn] = field(default_factory=dict)
    # Overrides calculate_tool_calling_accuracy for cases it cannot express
    # (e.g. "either of two tools, with these arguments"), so the persisted
    # score is the one the test actually asserts on.
    scorer: Callable[[list[Any]], float] | None = None


@dataclass
class AccuracyCaseResult:
    accuracy: float
    actual_calls: list[Any]
    response: ModelResponse


async def run_accuracy_case(
    case: AccuracyCase,
    *,
    accuracy_client_factory: Callable[
        [], AbstractAsyncContextManager[AccuracyTestingClient]
    ],
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> AccuracyCaseResult:
    async with accuracy_client_factory() as client:
        if case.seed is not None:
            await case.seed(client)

        client.reset()
        if case.mocks:
            client.mock_tools(case.mocks)
        tools = await client.openai_tools()

        try:
            agent_result = await openai_agent.run(
                case.prompt,
                tools=tools,
                execute_tool=client.execute_tool,
            )
        finally:
            if case.cleanup is not None:
                # Cleanup is best-effort; we always want to score and store
                # the result even if teardown fails.
                with contextlib.suppress(Exception):
                    await case.cleanup(client)

        actual_calls = client.llm_tool_calls()

    if case.scorer is not None:
        accuracy = case.scorer(actual_calls)
    else:
        accuracy = calculate_tool_calling_accuracy(case.expected_tools, actual_calls)

    response = ModelResponse(
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
    )

    result_storage.save_model_response(
        run_id=accuracy_run_id,
        commit_sha=commit_sha,
        prompt=case.prompt,
        expected_tool_calls=case.expected_tools,
        model_response=response,
    )

    return AccuracyCaseResult(
        accuracy=accuracy,
        actual_calls=actual_calls,
        response=response,
    )


# ---------------------------------------------------------------------------
# Result validation (LLM-as-judge)
# ---------------------------------------------------------------------------


@dataclass
class ResultCase:
    """A case that validates the *answer* the LLM produces, not just the call.

    The agent runs end-to-end against real Couchbase; an LLM judge then scores
    whether the final answer reflects ``expectation`` (the seeded ground truth)
    and is grounded in the tool results.

    ``seed`` should plant known data so the correct answer is deterministic.
    ``expected_tools`` is optional — when provided, tool-calling accuracy is
    computed alongside the judge verdict (purely informational; the test
    asserts on the verdict).
    """

    test_id: str
    prompt: str
    expectation: str
    seed: SetupHook | None = None
    cleanup: SetupHook | None = None
    expected_tools: list[ExpectedToolCall] | None = None
    mocks: dict[str, MockedToolFn] = field(default_factory=dict)


@dataclass
class ResultCaseResult:
    verdict: JudgeVerdict
    answer: str
    tool_results: list[dict[str, Any]]
    actual_calls: list[Any]
    tool_calling_accuracy: float | None
    response: ModelResponse


def _model_response(
    agent_result: AgentResult,
    *,
    openai_model: str,
    actual_calls: list[Any],
    tool_calling_accuracy: float,
) -> ModelResponse:
    return ModelResponse(
        provider="OpenAI",
        requested_model=openai_model,
        responding_model=agent_result.responding_model,
        llm_response_time_ms=agent_result.elapsed_ms,
        tool_calling_accuracy=tool_calling_accuracy,
        llm_tool_calls=actual_calls,
        tokens_used=TokensUsed(
            prompt_tokens=agent_result.prompt_tokens,
            completion_tokens=agent_result.completion_tokens,
            total_tokens=agent_result.total_tokens,
        ),
        text=agent_result.text,
        messages=agent_result.messages,
    )


async def run_result_case(
    case: ResultCase,
    *,
    accuracy_client_factory: Callable[
        [], AbstractAsyncContextManager[AccuracyTestingClient]
    ],
    openai_agent: OpenAIAgent,
    judge: LLMJudge,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> ResultCaseResult:
    async with accuracy_client_factory() as client:
        if case.seed is not None:
            await case.seed(client)

        client.reset()
        if case.mocks:
            client.mock_tools(case.mocks)
        tools = await client.openai_tools()

        try:
            agent_result = await openai_agent.run(
                case.prompt,
                tools=tools,
                execute_tool=client.execute_tool,
            )
        finally:
            if case.cleanup is not None:
                with contextlib.suppress(Exception):
                    await case.cleanup(client)

        actual_calls = client.llm_tool_calls()

    tool_results = extract_tool_results(agent_result.messages)

    verdict = await judge.evaluate(
        question=case.prompt,
        answer=agent_result.text,
        expectation=case.expectation,
        tool_results=tool_results,
    )

    tool_calling_accuracy: float | None = None
    if case.expected_tools is not None:
        tool_calling_accuracy = calculate_tool_calling_accuracy(
            case.expected_tools, actual_calls
        )

    response = _model_response(
        agent_result,
        openai_model=openai_model,
        actual_calls=actual_calls,
        # Reuse the existing field to carry the judge's correctness score so
        # the stored ModelResponse stays comparable across run types.
        tool_calling_accuracy=verdict.score,
    )

    result_storage.save_result_eval(
        run_id=accuracy_run_id,
        commit_sha=commit_sha,
        prompt=case.prompt,
        expectation=case.expectation,
        verdict=verdict,
        answer=agent_result.text,
        tool_results=tool_results,
        tool_calling_accuracy=tool_calling_accuracy,
        model_response=response,
    )

    return ResultCaseResult(
        verdict=verdict,
        answer=agent_result.text,
        tool_results=tool_results,
        actual_calls=actual_calls,
        tool_calling_accuracy=tool_calling_accuracy,
        response=response,
    )
