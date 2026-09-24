# Tests

The suite is split into four tiers by how much infrastructure each tier
needs. Each tier lives in its own directory so a glance at the layout tells
you what a file depends on, and so CI can opt into / out of each tier
independently.

```
tests/
├── _test_env.py         # shared env helpers (cluster creds, default bucket)
├── _all_specs.py        # test-only ALL_SPECS registry, for cross-server invariant tests
├── unit/                # pure Python, no Couchbase, no LLM
│   ├── <cross-server>        # shared machinery + invariants over every spec
│   ├── operational/          # unit tests exclusive to the operational server
│   └── operational_insights/ # unit tests exclusive to the OI server
├── integration/         # needs a live cluster
│   ├── conftest.py           # server-agnostic session plumbing + response helpers
│   ├── operational/          # needs a live Couchbase cluster
│   │   └── _census.py        #   what the operational server must register
│   └── operational_insights/ # needs a live Operational Insights cluster; env-gated
├── perf/                # in-process performance tests, opt-in via CB_MCP_PERF=1
└── accuracy/            # AI-in-the-loop — needs Couchbase + an OpenAI key
    ├── conftest.py
    ├── sdk/                  # the eval engine (agent, judge, scorer, matcher, ...)
    ├── tool_calling/         # "did the LLM pick the right tool + params?"
    └── result_validation/    # "is the LLM's final answer correct?"  (LLM-as-judge)
```

## Tiers

| Tier | Directory | Marker | Live cluster? | LLM cost? |
| --- | --- | --- | --- | --- |
| Unit (cross-server) | `tests/unit/` | — | No | No |
| └ Unit (operational) | `tests/unit/operational/` | — | No | No |
| └ Unit (Operational Insights) | `tests/unit/operational_insights/` | — | No | No |
| Integration (operational) | `tests/integration/operational/` | `integration` | Yes (Couchbase) | No |
| Integration (Operational Insights) | `tests/integration/operational_insights/` | `integration` + `operational_insights` | Yes (Operational Insights) | No |
| Perf | `tests/perf/` | `perf` | Optional (`test_live_cluster.py` only) | No |
| Accuracy | `tests/accuracy/` | `accuracy` | Yes | Yes |
| └ Result validation | `tests/accuracy/result_validation/` | `accuracy` + `result_eval` | Yes | Yes |

The accuracy tier has two axes (see [Accuracy tier details](#accuracy-tier-details)):
tool-calling tests under `tool_calling/` (marker `accuracy`), and answer-correctness
tests under `result_validation/` (markers `accuracy` **and** `result_eval`). Markers are
applied automatically by directory — test files carry no `@pytest.mark.accuracy` decorators.

- **Unit** — call functions in `cb_mcp.*` directly, with fakes / `SimpleNamespace`.
  Fast, deterministic, runnable anywhere. Includes cross-server invariant checks
  (`test_server_specs.py`) driven by `tests/_all_specs.py::ALL_SPECS` — extend that
  registry, not this doc, when a third server is added.
- **Integration (operational)** — spawn the real MCP server over stdio
  (`create_mcp_session`) and talk to a running Couchbase cluster. Requires
  `CB_CONNECTION_STRING`, `CB_USERNAME`, `CB_PASSWORD`, plus `CB_MCP_TEST_BUCKET`
  for tests that need a bucket. Missing env vars cause `pytest.skip(...)`
  rather than a failure.
- **Integration (Operational Insights)** — spawn `mcp_server operational-insights`
  (stdio, the default), or connect to an already-running instance over HTTP
  when `CB_MCP_TRANSPORT=http`/`MCP_SERVER_URL` are set
  (`create_oi_mcp_session`, defined in that directory's own `conftest.py`,
  mirrors the operational tier's `create_mcp_session` transport branching)
  — and talk to a running Operational Insights cluster. Requires
  `CB_OI_CONNECTION_STRING`, `CB_OI_USERNAME`, `CB_OI_PASSWORD` (see
  `tests/_test_env.py`). The whole directory is
  skipped at collection time, not per-test, when those are unset — see its
  `conftest.py`. Local setup uses the same containers and config CI does
  (`scripts/oi_ci_cluster/` — steps 2-5 of
  https://docs.couchbase.com/enterprise-analytics/current/intro/do-a-quick-install.html):
  ```bash
  export OI_IMAGE=couchbase/enterprise-analytics:2.2.1
  export S3MOCK_IMAGE=adobe/s3mock:5.2.3
  export S3MOCK_BUCKET=cloud-storage-container
  export S3MOCK_STORE_ROOT=fs
  export S3MOCK_RETAIN_FILES_ON_EXIT=true
  export OI_ADMIN_PORT=8091      # already in use? pick a free port (e.g. 9091) —
  export OI_ANALYTICS_PORT=8095  # a local Couchbase Server install claims these
  export BLOB_STORAGE_SCHEME=s3
  export BLOB_STORAGE_REGION=us-east-1
  export BLOB_STORAGE_ENDPOINT=http://s3mock:9090
  export BLOB_STORAGE_ANONYMOUS_AUTH=true
  export BLOB_STORAGE_PATH_STYLE_ADDRESSING=true
  export NUM_STORAGE_PARTITIONS=16
  export CLUSTER_USERNAME=Administrator
  export CLUSTER_PASSWORD=password
  export CLUSTER_MEMORY_QUOTA=100
  export CLUSTER_NAME="OI Local Cluster"

  docker compose -f scripts/oi_ci_cluster/docker-compose.yml up -d --wait
  ./scripts/oi_ci_cluster/configure_cluster.sh

  # 127.0.0.1, not localhost: the couchbase_operational_insights SDK picks
  # a random address when a hostname resolves to more than one (localhost
  # -> 127.0.0.1 and ::1), and about half the time that's an ::1 this
  # container's port publishing doesn't forward, causing an immediate
  # connection reset. A literal IP isn't looked up, so it can't happen.
  export CB_OI_CONNECTION_STRING=http://127.0.0.1:${OI_ANALYTICS_PORT}
  export CB_OI_USERNAME=$CLUSTER_USERNAME
  export CB_OI_PASSWORD=$CLUSTER_PASSWORD
  uv run --extra dev pytest tests/integration/operational_insights -v

  docker compose -f scripts/oi_ci_cluster/docker-compose.yml down -v
  ```
  To exercise every transport x server-binary combination (mirroring
  `scripts/run_matrix_local.sh` for the operational server), use
  `scripts/run_oi_matrix_local.sh` instead, which manages this same
  container lifecycle itself.
- **Perf** — in-process performance tests, opt-in via `CB_MCP_PERF=1`; not run in CI.
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
# export CB_ACCURACY_OPENAI_MODEL="gpt-5.5"     # agent model (default)
# export CB_ACCURACY_JUDGE_MODEL="gpt-5.5"           # result-validation judge (default: agent model)
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
  `require_test_bucket`, `get_test_scope`, `get_test_collection`, and their
  `oi_env_available` / `build_oi_env` / `get_oi_test_*` equivalents for the
  Operational Insights tier). Imported by the integration and accuracy
  conftests.
- [`_all_specs.py`](_all_specs.py) — test-only `ALL_SPECS` registry (every
  `ServerSpec` this distribution ships). `src/` deliberately has no such
  registry (each process loads only its own SDK); this exists purely so
  `tests/unit/test_server_specs.py` can check cross-server invariants.
- [`integration/conftest.py`](integration/conftest.py) — session plumbing and
  response helpers shared by every integration tier (`create_mcp_session`,
  `streamable_http_session`, `extract_payload`, `ensure_list`), plus
  `create_session_for_subcommand`, a small public wrapper a second server's
  integration tests can reuse to spawn `mcp_server <subcommand>`. Nothing
  here describes a particular server's tools.
- [`integration/operational/_census.py`](integration/operational/_census.py) —
  the `EXPECTED_TOOLS` / `TOOLS_BY_CATEGORY` / `TOOL_REQUIRED_PARAMS` tables
  for the operational server. Beside the tests that assert on them rather
  than in the shared conftest, so the Operational Insights tier no longer
  imports ~170 lines describing a tool set its server does not register.
- [`integration/operational_insights/conftest.py`](integration/operational_insights/conftest.py) —
  the Operational Insights tier's own session helper (`create_oi_mcp_session`,
  built on `create_session_for_subcommand` for stdio and on
  `integration/conftest.py`'s `streamable_http_session` for http) and its
  directory-based auto-marking + env-gated skip.
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
