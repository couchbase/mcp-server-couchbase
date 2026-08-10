"""OpenAI tool-calling agent used by the accuracy tests.

The agent receives:
  * A natural-language prompt (or sequence of prompts).
  * A list of OpenAI-formatted tool definitions.
  * An async callback that executes a tool by name and returns the result.

It drives the OpenAI chat completion loop until the model produces a final
assistant message without further tool calls (or a step cap is hit), then
returns the conversation and a usage summary.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .openai_compat import supports_custom_temperature

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "\n".join(
    [
        "You are an expert AI assistant with access to tools for interacting with a Couchbase database.",
        "You MUST use the most relevant tool to answer the user's request.",
        "When calling a tool, you MUST strictly follow its input schema and MUST provide all required arguments.",
        "If a task requires multiple tool calls, you MUST call all necessary tools in sequence.",
        "Assume you are already connected to Couchbase — do NOT call any connection or authentication tool.",
        'If you do not know the answer or the request cannot be fulfilled, respond with "I don\'t know".',
    ]
)

DEFAULT_MAX_STEPS = 25

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass
class AgentResult:
    text: str
    responding_model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0


class OpenAIAgent:
    """Minimal tool-calling loop around OpenAI's chat completions API."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        system_prompt: str | None = None,
        extra_system_prompt: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        temperature: float | None = 0.0,
    ) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._max_steps = max_steps
        # Reasoning models (gpt-5*, o-series) reject a custom temperature.
        self._temperature = (
            temperature if supports_custom_temperature(model) else None
        )

        system_parts: list[str] = []
        if system_prompt is None:
            system_parts.append(DEFAULT_SYSTEM_PROMPT)
        elif system_prompt:
            system_parts.append(system_prompt)
        if extra_system_prompt:
            system_parts.append(extra_system_prompt)
        self._system_prompt = "\n".join(system_parts)

    async def run(  # noqa: PLR0912
        self,
        prompts: str | list[str],
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
    ) -> AgentResult:
        """Run the prompt(s) and drive the tool-call loop to completion."""
        if isinstance(prompts, str):
            prompts = [prompts]

        result = AgentResult(text="", responding_model=self.model)
        start = time.perf_counter()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
        ]

        for user_prompt in prompts:
            messages.append({"role": "user", "content": user_prompt})

            for _ in range(self._max_steps):
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if self._temperature is not None:
                    kwargs["temperature"] = self._temperature

                completion = await self._client.chat.completions.create(**kwargs)

                result.responding_model = completion.model
                if completion.usage is not None:
                    result.prompt_tokens += completion.usage.prompt_tokens or 0
                    result.completion_tokens += completion.usage.completion_tokens or 0
                    result.total_tokens += completion.usage.total_tokens or 0

                choice = completion.choices[0]
                msg = choice.message

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or "",
                }
                tool_calls = msg.tool_calls or []
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]
                messages.append(assistant_msg)

                if not tool_calls:
                    if msg.content:
                        result.text += msg.content
                    break

                for tc in tool_calls:
                    try:
                        raw_args = tc.function.arguments or "{}"
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        tool_result_text = await execute_tool(tc.function.name, args)
                    except Exception as exc:  # surface errors so the LLM can recover
                        logger.warning(
                            "Tool '%s' raised during accuracy run: %s",
                            tc.function.name,
                            exc,
                        )
                        tool_result_text = json.dumps(
                            {"error": str(exc), "tool": tc.function.name}
                        )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result_text,
                        }
                    )
            else:
                logger.warning(
                    "Agent hit max_steps=%d without a final answer; stopping.",
                    self._max_steps,
                )

        result.messages = messages
        result.elapsed_ms = (time.perf_counter() - start) * 1000
        return result
