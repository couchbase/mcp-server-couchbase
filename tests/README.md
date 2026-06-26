# Tests

The suite is split into three tiers by how much infrastructure each tier
needs. Each tier lives in its own directory so a glance at the layout tells
you what a file depends on, and so CI can opt into / out of each tier
independently.

```
tests/
├── _test_env.py         # shared env helpers (cluster creds, default bucket)
├── conftest.py          # top-level fixtures + helpers used by integration tests
├── unit/                # pure Python, no Couchbase, no LLM
├── integration/         # needs a live Couchbase cluster
└── accuracy/            # AI-in-the-loop — needs Couchbase + an OpenAI key
    ├── conftest.py
    ├── sdk/                  # the eval engine (agent, judge, scorer, matcher, ...)
    ├── tool_calling/         # "did the LLM pick the right tool + params?"
    └── result_validation/    # "is the LLM's final answer correct?"  (LLM-as-judge)
```

## Tiers

| Tier | Directory | Marker | Live cluster? | LLM cost? |
| --- | --- | --- | --- | --- |
| Unit | `tests/unit/` | — | No | No |
| Integration | `tests/integration/` | `integration` | Yes | No |
| Accuracy | `tests/accuracy/` | `accuracy` | Yes | Yes |
| └ Result validation | `tests/accuracy/result_validation/` | `accuracy` + `result_eval` | Yes | Yes |

The accuracy tier has two axes (see [Accuracy tier details](#accuracy-tier-details)):
tool-calling tests under `tool_calling/` (marker `accuracy`), and answer-correctness
tests under `result_validation/` (markers `accuracy` **and** `result_eval`). Markers are
applied automatically by directory — test files carry no `@pytest.mark.accuracy` decorators.

- **Unit** — call functions in `cb_mcp.*` directly, with fakes / `SimpleNamespace`.
  Fast, deterministic, runnable anywhere.
- **Integration** — spawn the real MCP server over stdio (`create_mcp_session`)
  and talk to a running Couchbase cluster. Requires `CB_CONNECTION_STRING`,
  `CB_USERNAME`, `CB_PASSWORD`, plus `CB_MCP_TEST_BUCKET` for tests that
  need a bucket. Missing env vars cause `pytest.skip(...)` rather than a
  failure.
- **Accuracy** — drive an OpenAI tool-calling agent against the live MCP
  server and score the resulting tool calls. See [Accuracy tier
  details](#accuracy-tier-details) below.

## Running

Install dev dependencies (pulls in pytest + the accuracy SDK's `openai`):

```bash
uv sync --extra dev
# or: pip install -e ".[dev]"
```

Common commands:

```bash
# everything
pytest

# fast pass — unit only (no Couchbase, no API cost)
pytest tests/unit

# integration only (needs Couchbase env vars)
pytest tests/integration               # or: pytest -m integration

# accuracy only (needs Couchbase + OPENAI_API_KEY)
pytest tests/accuracy -v               # or: pytest -m accuracy

# just the tool-calling axis (which tool + params)
pytest tests/accuracy/tool_calling     # or: pytest -m "accuracy and not result_eval"

# just the result-validation axis (answer correctness, LLM-as-judge)
pytest tests/accuracy/result_validation  # or: pytest -m result_eval

# CI fast-path: skip the LLM tier
pytest -m "not accuracy"
```

Env vars used across the tiers:

```bash
# Couchbase (integration + accuracy)
export CB_CONNECTION_STRING="couchbases://..."
export CB_USERNAME="..."
export CB_PASSWORD="..."
export CB_MCP_TEST_BUCKET="travel-sample"
export CB_MCP_TEST_SCOPE="_default"
export CB_MCP_TEST_COLLECTION="_default"

# OpenAI (accuracy only)
export OPENAI_API_KEY="sk-..."
# Optional accuracy overrides:
# export CB_ACCURACY_OPENAI_MODEL="gpt-4o"     # agent model (default)
# export CB_ACCURACY_JUDGE_MODEL="gpt-4o"           # result-validation judge (default: agent model)
# export CB_ACCURACY_OPENAI_BASE_URL="https://..."  # Azure / proxy
# export CB_ACCURACY_RUN_ID="ci-2026-05-22"
# export CB_ACCURACY_RESULTS_DIR="/tmp/acc"
```

## Adding a test

- Pure logic, no I/O? → `tests/unit/`.
- Needs the running MCP server or a Couchbase round-trip? → `tests/integration/`.
- Verifies that an LLM picks the right tool / extracts the right params?
  → `tests/accuracy/tool_calling/`. See the [recipe below](#adding-a-tool-calling-case).
- Verifies that the LLM's *final answer* is correct? →
  `tests/accuracy/result_validation/`. See the [recipe below](#adding-a-result-validation-case).

If you're tempted to drop a unit test into `integration/` because it's
"close enough", don't — keeping the unit tier free of cluster
dependencies is what lets `pytest tests/unit` stay fast and runnable on
any laptop.

## Shared helpers

- [`_test_env.py`](_test_env.py) — env builders (`_build_env`,
  `require_test_bucket`, `get_test_scope`, `get_test_collection`).
  Imported by both the integration and accuracy conftests.
- [`conftest.py`](conftest.py) — re-exports the helpers and adds
  integration-only utilities (`create_mcp_session`, `extract_payload`,
  `ensure_list`, the `EXPECTED_TOOLS` / `TOOLS_BY_CATEGORY` /
  `TOOL_REQUIRED_PARAMS` tables).
- [`accuracy/conftest.py`](accuracy/conftest.py) — accuracy-only
  fixtures (`accuracy_client`, `openai_agent`, `judge`, `result_storage`,
  etc.) and the directory-based auto-marking.

---

# Accuracy tier details

The accuracy tier is AI-in-the-loop and has **two axes**, each in its own
subdirectory:

| Axis | Directory | Question | Scoring |
| --- | --- | --- | --- |
| Tool calling | `accuracy/tool_calling/` | Did the LLM pick the right tool with the right parameters? | matcher score (0 / 0.75 / 1.0) |
| Result validation | `accuracy/result_validation/` | Is the LLM's final answer correct? | LLM-as-judge (pass/fail) |

Both drive an OpenAI tool-calling agent against the **real** MCP server +
Couchbase. The difference is what gets asserted: the *tool calls* vs the
*final answer*.

```
Test → AccuracyTestingClient → MCP Server → Couchbase
         ↑                ↓
   records LLM        OpenAI agent ──► final answer ──► [result_validation]
   tool calls                                            LLM judge → pass/fail
         │
         └► [tool_calling] matcher scorer → 0 / 0.75 / 1.0
```

## Axis 1 — tool calling (`accuracy/tool_calling/`)

42 cases across five per-family files. Each file mixes two kinds of cases:

- **Parameter-extraction cases** — explicit prompts that verify both the
  right tool *and* the right parameters.
- **Conversational cases** (`test_id` prefixed `conversational_`) —
  natural prompts that only assert tool selection (parameters use
  `Matcher.any_value()`), decoupling intent recognition from parameters.

| File | Family | Cases |
| --- | --- | --- |
| `accuracy/tool_calling/test_kv.py` | KV (get/insert/upsert/replace/delete + multi-step + negative + conversational) | 8 |
| `accuracy/tool_calling/test_server.py` | Server / cluster + conversational | 11 |
| `accuracy/tool_calling/test_query.py` | SQL++ query + conversational | 6 |
| `accuracy/tool_calling/test_index.py` | Indexes + conversational | 6 |
| `accuracy/tool_calling/test_query_performance.py` | Query performance + conversational | 11 |

### Scoring

`accuracy/sdk/scorer.py` implements the 0 / 0.75 / 1.0 rubric:

- **1.0** — exact expected tool calls with exact parameters.
- **0.75** — right tools called but with extras (extra calls / extra params).
- **0** — a required expected tool call was missing, or a matched call had
  incorrect parameters.

Tests fail when the score drops below 0.75.

### Flexible parameter matching

`accuracy/sdk/matcher.py` provides matchers for the inherent
non-determinism of LLM output, used directly inside `parameters`:

```python
from accuracy.sdk import Matcher, ExpectedToolCall

ExpectedToolCall(
    tool_name="upsert_document_by_id",
    parameters={
        "bucket_name": "travel-sample",
        "scope_name": "inventory",
        "collection_name": "airline",
        "document_id": "airline_42",
        "document_content": Matcher.any_value(),  # body is LLM-derived
    },
)
```

Available matchers: `any_value`, `empty_object_or_undefined`, `undefined`,
`null`, `boolean`, `number`, `string`, `case_insensitive_string`, `any_of`,
`not_`, and the default `value` (literal match with recursion).

## Axis 2 — result validation (`accuracy/result_validation/`)

28 cases across five per-family files. The agent runs end-to-end, then an
**LLM judge** (`accuracy/sdk/judge.py`) scores the final answer via OpenAI
structured output, returning `{passed, score, reasoning}`. The test asserts
`passed`.

Each case carries an `expectation` — a natural-language description of what
a correct answer must contain. There are two expectation styles:

- **Seeded ground truth** — the case seeds known data (invented tokens like
  country `Zubrowka`) and the expectation names exact facts the answer must
  state. Used where the result is deterministic (KV `get`, `run_sql` via
  `USE KEYS`).
- **Faithfulness** — for tools whose output isn't seedable/deterministic
  (server topology, health, index advisor, performance history). The
  expectation asks the judge to confirm the answer is *consistent with the
  tool output and invents nothing*. Performance cases also treat an empty
  result ("no completed queries") as a valid PASS.

| File | Family | Cases | Mode |
| --- | --- | --- | --- |
| `accuracy/result_validation/test_kv.py` | get / numeric / summarize / nonexistent / insert / upsert / replace / delete | 8 | seeded + faithfulness |
| `accuracy/result_validation/test_query.py` | run_sql (USE KEYS) / schema / explain | 4 | seeded + faithfulness |
| `accuracy/result_validation/test_index.py` | list_indexes / advisor | 2 | faithfulness |
| `accuracy/result_validation/test_server.py` | buckets / scopes / collections / health / config / connection | 7 | faithfulness |
| `accuracy/result_validation/test_query_performance.py` | all 7 query-performance tools | 7 | faithfulness (empty-OK) |

The judge defaults to the agent model; set `CB_ACCURACY_JUDGE_MODEL` to grade
with a stronger model than the one under test.

## Results

Each run writes one JSON file to `tests/accuracy/results/<run_id>.json` with:

- `prompt_results[]` — tool-calling outcomes (prompt, expected calls,
  accuracy score, captured calls, transcript, token usage).
- `result_evals[]` — result-validation outcomes (prompt, expectation, judge
  verdict + reasoning, the agent's answer, and the raw tool results).

## Adding a tool-calling case

1. Pick the family file in `accuracy/tool_calling/` (or add a new one).
2. Append an `AccuracyCase` inside `_build_cases(...)`:
   ```python
   AccuracyCase(
       test_id="my_new_case",
       prompt="...",
       expected_tools=[ExpectedToolCall(...)],
       seed=...,       # optional; use accuracy.sdk.seed_document(...)
       cleanup=...,    # optional; use accuracy.sdk.delete_document(...)
   )
   ```
3. Add the new `test_id` to the file's `*_CASE_IDS` list.

## Adding a result-validation case

1. Pick the family file in `accuracy/result_validation/` (or add a new one).
2. Append a `ResultCase` inside `_build_cases(...)`:
   ```python
   ResultCase(
       test_id="my_new_case",
       prompt="...",
       expectation="What a correct answer must state (seeded facts) "
                   "OR a faithfulness rubric the judge applies.",
       seed=seed_document(bucket, scope, collection, doc_id, {...}),  # optional
       cleanup=delete_document(bucket, scope, collection, doc_id),    # optional
   )
   ```
3. Add the new `test_id` to the file's `*_CASE_IDS` list.

Write the `expectation` to describe **the one property under test** — exact
seeded facts, or a faithfulness rubric — not incidental phrasing. Seed/cleanup
hooks use `call_tool_silent` under the hood so they never pollute the recorded
LLM tool-call log.

## Accuracy SDK reference

- [`accuracy/sdk/runner.py`](accuracy/sdk/runner.py) — `run_accuracy_case`
  (tool-calling) and `run_result_case` (result validation) drive one case
  end-to-end; `extract_tool_results` pulls tool outputs from the transcript.
- [`accuracy/sdk/client.py`](accuracy/sdk/client.py) —
  `AccuracyTestingClient` (MCP ↔ OpenAI bridge, tool-call recording,
  mock support, `call_tool_silent`).
- [`accuracy/sdk/agent.py`](accuracy/sdk/agent.py) — `OpenAIAgent`
  (tool-call loop; returns final answer + transcript).
- [`accuracy/sdk/judge.py`](accuracy/sdk/judge.py) — `LLMJudge`
  (structured pass/fail/score/reasoning verdict).
- [`accuracy/sdk/scorer.py`](accuracy/sdk/scorer.py) — 0 / 0.75 / 1.0
  tool-calling scoring.
- [`accuracy/sdk/matcher.py`](accuracy/sdk/matcher.py) — flexible
  parameter matchers.
- [`accuracy/sdk/seeding.py`](accuracy/sdk/seeding.py) — shared
  `doc_id` / `seed_document` / `delete_document` hooks.
- [`accuracy/sdk/result_storage.py`](accuracy/sdk/result_storage.py) —
  disk JSON storage (`save_model_response`, `save_result_eval`).
