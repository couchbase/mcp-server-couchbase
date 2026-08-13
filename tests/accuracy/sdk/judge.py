"""LLM-as-judge: scores whether the agent's final answer is correct.

This is the second axis of accuracy testing. The tool-calling scorer
(``sdk/scorer.py``) checks *which* tool the LLM picked and with what
parameters. The judge here checks the *result*: given the question, the
agent's final natural-language answer, and the raw tool output the agent
saw, is the answer correct and grounded in real data?

The judge is itself an OpenAI call. It receives:
  - the original user question
  - the agent's final answer
  - the raw tool results returned by the MCP server (so it can confirm the
    answer is grounded, not hallucinated)
  - the expected ground truth for the case (seeded facts / criteria)

It returns a structured verdict via OpenAI JSON-schema structured output:
``{passed: bool, score: float in [0,1], reasoning: str}``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from .openai_compat import supports_custom_temperature

logger = logging.getLogger(__name__)

# Each tool result is truncated to this many characters before being shown
# to the judge, as a guard against pathologically huge payloads (e.g. a
# SELECT returning thousands of rows). Keep this generous: list-returning
# tools like list_indexes can produce several KB of legitimate output, and
# truncating mid-list makes the judge wrongly flag the answer's faithful
# mentions of cut-off items as "fabricated". The judge is also told (in its
# system prompt) to never treat truncated-away content as fabricated.
MAX_TOOL_RESULT_CHARS = 24000

JUDGE_SYSTEM_PROMPT = "\n".join(
    [
        "You are a strict, impartial evaluator for a Couchbase AI assistant.",
        "You are given:",
        "  1. The user's original question.",
        "  2. The assistant's final answer.",
        "  3. The raw tool results the assistant retrieved from the database.",
        "  4. The expected ground truth (facts the answer must reflect).",
        "",
        "Your job is to decide whether the assistant's final answer is CORRECT.",
        "An answer is correct when it:",
        "  - states the facts required by the expected ground truth, AND",
        "  - is consistent with the raw tool results (no hallucinated values), AND",
        "  - does not contradict the ground truth or invent data.",
        "",
        "Be strict: if a required fact is missing, wrong, or fabricated, fail it.",
        "Ignore differences in phrasing, formatting, politeness, or extra correct",
        "detail. Judge substance, not style.",
        "",
        "IMPORTANT about truncation: a tool result may end with a marker like",
        "'... [truncated N chars]'. When present, the tool returned MORE data than",
        "you can see. In that case you MUST NOT conclude that an item mentioned in",
        "the answer is fabricated merely because it is absent from the visible ",
        "portion — it may lie in the truncated remainder. Only flag fabrication",
        "you can positively confirm contradicts the visible tool output.",
        "",
        "Return your verdict using the provided JSON schema:",
        "  - passed: true only if the answer is correct as defined above.",
        "  - score: 1.0 = fully correct, 0.0 = wrong/missing; partial in between.",
        "  - reasoning: one or two sentences citing the specific fact(s) that drove",
        "    your decision.",
    ]
)

JUDGE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "judge_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "passed": {
                    "type": "boolean",
                    "description": "True only if the answer is correct.",
                },
                "score": {
                    "type": "number",
                    "description": "Correctness in [0,1]; 1 perfect, 0 wrong.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Short justification citing the deciding fact(s).",
                },
            },
            "required": ["passed", "score", "reasoning"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class JudgeVerdict:
    passed: bool
    score: float
    reasoning: str
    judge_model: str


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _format_tool_results(tool_results: list[dict[str, Any]]) -> str:
    if not tool_results:
        return "(the assistant did not call any tools)"
    lines: list[str] = []
    for i, tr in enumerate(tool_results, start=1):
        name = tr.get("tool_name") or "unknown_tool"
        content = tr.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        lines.append(f"[{i}] tool={name}\n{_truncate(content)}")
    return "\n\n".join(lines)


class LLMJudge:
    """Scores answer correctness via a separate OpenAI structured-output call."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = 0.0,
    ) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        # Reasoning models (gpt-5*, o-series) reject a custom temperature.
        self._temperature = (
            temperature if supports_custom_temperature(model) else None
        )

    async def evaluate(
        self,
        *,
        question: str,
        answer: str,
        expectation: str,
        tool_results: list[dict[str, Any]],
    ) -> JudgeVerdict:
        user_content = "\n\n".join(
            [
                f"## User question\n{question}",
                f"## Assistant's final answer\n{answer or '(empty answer)'}",
                f"## Raw tool results\n{_format_tool_results(tool_results)}",
                f"## Expected ground truth\n{expectation}",
                "Evaluate the assistant's final answer and return the verdict.",
            ]
        )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "response_format": JUDGE_RESPONSE_FORMAT,
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        completion = await self._client.chat.completions.create(**kwargs)
        raw = completion.choices[0].message.content or "{}"

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Judge returned non-JSON content: %r", raw)
            return JudgeVerdict(
                passed=False,
                score=0.0,
                reasoning=f"Judge returned unparseable output: {raw[:200]!r}",
                judge_model=completion.model,
            )

        return JudgeVerdict(
            passed=bool(data.get("passed", False)),
            score=float(data.get("score", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            judge_model=completion.model,
        )
