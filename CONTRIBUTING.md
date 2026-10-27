# Contributing to Couchbase MCP Server

Thank you for your interest in contributing to the Couchbase MCP Server! This guide explains how to set up your development environment, the design principles we expect contributions to follow, and what we look for when reviewing pull requests.

## 🧭 Servers in This Repository

This repository ships **more than one MCP server** in a single distribution (`couchbase-mcp-server`). Each server targets one Couchbase service, uses that service's SDK, and runs as its own process, selected by a CLI subcommand. Before you start, work out which server your change belongs to — several rules in this guide differ between them.

| | Operational | Operational Insights |
| --- | --- | --- |
| Subcommand | `operational` (also the current default) | `operational-insights` |
| Talks to | Couchbase Server / Capella clusters | Operational Insights clusters |
| Supported deployments | **Capella and self-managed** | **Self-managed only** (no Capella support today) |
| SDK | [`couchbase`](https://docs.couchbase.com/python-sdk/current/hello-world/start-using-sdk.html) | [`couchbase-operational-insights`](https://github.com/couchbaselabs/operational-insights-python-client) |
| Connection string | `couchbase://` / `couchbases://` | HTTP(S) URL, e.g. `http://localhost:8095` |
| Credential env vars | `CB_CONNECTION_STRING`, `CB_USERNAME`, `CB_PASSWORD`, ... | `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING`, `CB_OPERATIONAL_INSIGHTS_USERNAME`, ... |
| Tool names | Unprefixed (`get_document_by_id`, ...) | Prefixed `operational_insights_` |
| Spec / tools | `src/cb_mcp/servers/operational/`, `src/cb_mcp/tools/operational/` | `src/cb_mcp/servers/operational_insights/`, `src/cb_mcp/tools/operational_insights/` |
| Provider | `src/providers/operational.py` | `src/providers/operational_insights.py` |
| Tests | `tests/{unit,integration}/operational/`, `tests/accuracy/` | `tests/{unit,integration}/operational_insights/` |
| CI workflow | `test.yml` | `operational-insights-integration-tests.yml` |
| Default HTTP port / log file | `8000` / `mcp_server.log` | `8001` / `mcp_server_operational_insights.log` |
| Multiple workers (`--workers`) | Supported | Not supported (async query handles live in process memory) |

Everything else — the app builder, CLI option stacks, tool registration and gating, OAuth, logging, auditing, and `get_server_configuration_status` — is **shared machinery** under `src/cb_mcp/core/`, `src/cb_mcp/audit/`, and `src/cb_mcp/utils/`. A change there affects every server, so it needs tests that cover all of them (see [Testing](#-testing)).

This guide is organised the same way: [guidelines for every server](#guidelines-for-every-server), then [operational-specific](#operational-server) and [Operational Insights-specific](#operational-insights-server) rules. To add a new server, see [Adding a new MCP server](#adding-a-new-mcp-server).

## 📜 Contribution Principles

These are the ground rules we review every contribution against. Reading this section first will save you review round-trips.

1. **Discuss before you build.** Open an issue before writing code for any new tool or significant change — a GitHub issue for external contributors, or a JIRA ticket for Couchbase internal contributors (see the [issue template](.github/JIRA_ISSUE_TEMPLATE.md)). Say which server the change targets. For new tools, we want to design the tool interface (name, parameters, return shape, annotations) together *before* implementation — a tool's interface is a public API for MCP clients and is hard to change later.
   - Use a **single issue per class of related tools** (e.g., one issue proposing a set of Search tools) rather than one issue per tool.
   - Explain **why the tools are needed** with as detailed a use case as possible — what an AI agent cannot accomplish today, and how it would use the proposed tools.
2. **Keep pull requests small and focused.** One bug fix, one refactor, or one tool per PR — or a small set of closely related tools (e.g., the read and write tools for one feature) when they are reviewed best together. Don't mix changes to different servers in one PR unless the change is to shared machinery. Small PRs get reviewed and merged much faster. If your change is large, split it into a series of smaller PRs and mention the plan in the issue.
3. **Use the official SDK for the server's service.** Each server is a thin adapter over its service's SDK (see the [table above](#-servers-in-this-repository)). Only fall back to raw REST calls when the SDK genuinely does not expose the capability, and call that out explicitly in your PR.
4. **Support every deployment the server targets.** Operational server features must work on Couchbase Capella *and* self-managed Couchbase Server, unless declared otherwise (see [Capella and self-managed support](#capella-and-self-managed-support)). The Operational Insights server targets self-managed deployments only.
5. **Don't break the managed MCP interfaces.** The `cb_mcp` package is shared with managed (hosted) MCP server implementations via the host-agnostic contracts in `src/cb_mcp/core/contracts.py` (notably `ProviderLifecycle` and `ClusterProvider`). See [Host-agnostic design](#host-agnostic-design) below.
6. **Test your changes — and show it.** New code needs unit tests and, where it touches a live cluster, integration tests. PRs must include evidence of testing (see [Evidence of testing](#evidence-of-testing)).
7. **Preserve backwards compatibility.** Tool names, parameters, return shapes, CLI flags, environment variables, server identities, and audit event IDs are all public interfaces. Breaking changes need prior discussion in an issue.

## 🚀 Development Setup

### Prerequisites

- **Python 3.11–3.14**: Code must stay 3.11-compatible
- **[uv](https://docs.astral.sh/uv/)**: Fast Python package installer and dependency manager
- **Git**: For version control
- **A cluster for the server you're changing**:
  - *Operational server*: a [Capella free tier](https://docs.couchbase.com/cloud/get-started/create-account.html) cluster *and* a self-managed Couchbase Server (e.g., [via Docker](https://docs.couchbase.com/server/current/install/getting-started-docker.html)) — changes must be tested against both. CI tests against Couchbase Server 8.0 and 7.6
  - *Operational Insights server*: a self-managed Operational Insights cluster. The easiest route is the Docker setup described in [tests/README.md](tests/README.md)
- **Docker** (optional for the operational server, effectively required for Operational Insights): used by `scripts/run_matrix_local.sh` and the Operational Insights integration tier
- **VS Code** (recommended): With Python extension for the best development experience

### Clone and Setup

```bash
# Clone the repository
git clone https://github.com/couchbase/mcp-server-couchbase.git
cd mcp-server-couchbase
```

**Note:** External contributors do not have commit permissions on the main repository. [Fork the repo](https://github.com/couchbase/mcp-server-couchbase/fork) to your own GitHub account and clone your fork instead of this repo.

```bash
# Install dependencies (including development tools)
uv sync --extra dev
```

### Install Development Tools

```bash
# Install pre-commit hooks (runs linting on every commit)
uv run pre-commit install

# Verify installation
uv run pre-commit run --all-files
```

## 🧹 Code Quality & Linting

We use **[Ruff](https://docs.astral.sh/ruff/)** for fast linting and code formatting to maintain consistent code quality.

```bash
# Check code quality (no changes made)
./scripts/lint.sh
# or: uv run ruff check src/

# Auto-fix issues
./scripts/lint_fix.sh
# or: uv run ruff check src/ --fix && uv run ruff format src/
```

- **Pre-commit hooks**: Ruff runs automatically on every `git commit`
- **VS Code**: Auto-format on save using the [Ruff extension](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff)

### Code Style Guidelines

- **Line length**: 88 characters (enforced by Ruff)
- **Import organization**: isort-style grouping (standard library, third-party, local)
- **Type hints**: Use modern Python type hints
- **Docstrings**: Add docstrings for public functions and classes — tool docstrings are shown to LLMs, so make them precise and unambiguous
- **Error handling**: Catch specific exceptions, log them, and return actionable error messages (tool errors are read by an LLM, which will try to recover based on your message)
- **Logging**: Use the hierarchical logging pattern. Shared modules log under
  the package namespace — `logger = logging.getLogger(f"{LOGGER_NAMESPACE}.module.name")`
  — while a server's own modules nest under that server, e.g.
  `logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.query")`. Do not log
  on the bare `LOGGER_ROOT` (`couchbase`): that namespace belongs to the
  Couchbase SDK, which registers its own loggers there. `LOGGER_ROOT` is the
  single point handlers attach to, which is why SDK records are captured too.
  Never log credentials, connection strings with passwords, or document contents.

## 🏛️ Design Guidelines

### Guidelines for every server

#### Host-agnostic design

The `cb_mcp` package is reused by managed MCP server implementations, not just the standalone CLI in this repo. To keep it portable:

- **Tools must obtain their client through the request context and the server's provider** — never from global state, CLI arguments, or environment variables read inside `cb_mcp`.
- **Don't read CLI/env configuration inside `cb_mcp`.** Configuration parsing belongs to the host (`src/mcp_server.py` and `src/providers/`).
  - **Named exception:** `src/cb_mcp/utils/cli_params.py` is host-configuration code (it turns parsed Click params into settings) that lives inside `cb_mcp` anyway, grouped with the other CLI/config helpers in `utils/` rather than with `src/providers/`'s provider implementations. It is imported only by `src/mcp_server.py`. Nothing else under `cb_mcp` may import it, and it must never be re-exported from `cb_mcp/utils/__init__.py` — that would make every `cb_mcp` consumer inherit a dependency on Click.
- **Don't change the provider protocols** (or other contracts in `core/`) without prior discussion — managed implementations depend on them. `ProviderLifecycle` (`core/contracts.py`) is the service-agnostic half (`close`, `get_configuration`, `is_connected`) that the shared machinery calls. Each server extends it with what its own tools need, next to that service's helpers: `ClusterProvider` adds `get_cluster` for the operational server, and `cb_mcp/utils/operational_insights/contracts.py` declares the Operational Insights provider. Never widen `ProviderLifecycle` for one server.
- **Keep SDKs out of shared code.** `core/`, `audit/`, and the shared `utils/` modules must not import any server's SDK; a process loads only the SDK of the server it runs (`tests/unit/test_sdk_isolation.py` enforces this).
- Provider configuration returned for status reporting must never include secrets (return `_configured` booleans instead).

#### Tool design

New tools are the most common contribution. Before implementing any, open a single issue covering the class of related tools you're proposing, following the [issue template](.github/JIRA_ISSUE_TEMPLATE.md). The issue should include:

- **Which server** the tools belong to
- **The use case, in as much detail as possible**: what task an AI agent cannot accomplish with the existing tools, a concrete example prompt/workflow, and how the agent would use the proposed tools to complete it
- **The proposed interface** for each tool: name, parameters, return shape, and annotations
- **Deployment and SDK considerations**: which of the server's target deployments support the capability, and whether the SDK exposes it

When implementing, in the server's tool package (`src/cb_mcp/tools/<server>/`):

1. **Create the tool function** in the appropriate module — see the per-server sections below for the module layout — or propose a new module in your issue if none fits. Follow the server's naming convention (Operational Insights tools are prefixed `operational_insights_`); tool names must be unique across all servers
2. **Export the tool** in that package's `__init__.py` and add it to `__all__`
3. **Add it to the correct half of `TOOL_SET`**: `read_only` if it only reads data, or `write` if it modifies data (so it is not loaded at all under read-only mode). This split also decides the OAuth scope the tool requires
4. **Add an entry to `TOOL_ANNOTATIONS`** with accurate hints (`readOnlyHint`, `idempotentHint`, `destructiveHint`) — clients rely on these for safety decisions
5. **Classify it for auditing**: add the tool to that server's table in `TOOL_CLASSIFICATION` (`src/cb_mcp/audit/classification.py`) under the right category and operation class. Unclassified tools fail closed (recorded as an unfilterable write) and `tests/unit/test_audit_classification.py` fails the build
6. **Add a `TOOL_SCOPE_HINTS` entry** if the generic scope-denial message would under-explain why the tool needs its scope
7. **Respect read-only mode**: any tool that can modify data or cluster state must be excluded when `read_only_mode` is enabled. A tool that accepts arbitrary statements must enforce read-only mode at runtime — see each server's section for how
8. **Support confirmation**: destructive tools should work with the confirmation/elicitation mechanism (`src/cb_mcp/utils/elicitation.py`)
9. **Use the standard result envelope** for write tools and for read tools that report their own failures: `tool_success(...)` / `tool_error(...)` from `src/cb_mcp/utils/responses.py`, so the LLM sees *why* something failed
10. **Be token-conscious**: tool output goes into an LLM context window. Bound result sizes rather than returning unbounded data — a `limit` parameter with a sensible default, or stream rows through `collect_rows_within_budget` (`src/cb_mcp/utils/query_limits.py`) instead of buffering a whole result set
11. **Don't add state that must survive between calls** unless the server already accounts for it. On a server that supports `--workers`, a follow-up call may land on a different process, so any cross-call state must be raised in the issue (the server would need `supports_multiple_workers=False`)
12. **Document it**: add the tool to that server's tool table in `README.md`
13. **Write tests** (unit + integration, plus accuracy cases where useful) and verify the tool end-to-end with an MCP client

#### Security

- Never log or return credentials, connection strings containing passwords, or certificate contents.
- Validate and constrain inputs at the tool boundary; remember tool parameters are LLM-generated and may be malformed or adversarial.
- Never interpolate a database/bucket/scope/collection/index/field name into SQL++ unquoted. Use `safe_ident`, `safe_field_path`, and `quote_literal` from `src/cb_mcp/utils/sqlpp.py` (shared by both servers' SQL++ dialects), and bind values as query parameters.
- Don't weaken a server's read-only enforcement for statement-running tools (see the per-server sections).
- Refusals that happen inside the server (read-only blocks, scope denials, declined confirmations) must raise the typed `AuditableRefusalError` subclasses in `src/cb_mcp/audit/exceptions.py` rather than returning a plain error, so they reach the audit log.
- Outbound network calls are limited to the MCP transport, the configured cluster, the opt-in embedding provider used by operational vector search, the OAuth JWKS endpoint when OAuth is configured, and a single best-effort startup telemetry ping (`src/cb_mcp/utils/telemetry.py`, which honours `DO_NOT_TRACK`). Don't add new outbound calls, update checks, or per-tool-call telemetry without discussing it in an issue first.

#### Dependencies

Keep the dependency footprint small — all servers ship in one distribution via PyPI and Docker, and are embedded in managed environments.

- Discuss any new runtime dependency in an issue before adding it.
- Prefer the standard library or existing dependencies (`couchbase`, `couchbase-operational-insights`, `fastmcp`, `click`, `lark-sqlpp`, ...).
- Import a server's SDK only from that server's own modules, never from shared code.
- Give every runtime dependency an upper bound, and comment any bound that is tighter than the next major, saying why it is needed.
- Dependencies needed by only one opt-in feature belong in an optional extra (like `bedrock-embeddings`), not the core list.
- New dependencies must be actively maintained and use a permitted license (see [Dependency licenses](#dependency-licenses) below).
- Dependency bumps arrive via Dependabot; don't mix unrelated version bumps into a feature PR.

##### Dependency licenses

The rules below apply to every new dependency, including any new transitive dependencies it brings in, and to existing dependencies whose license changes on upgrade. State each new package's license in your PR. A package offered under more than one license must say whether *all* of them apply (X **and** Y) or you can choose one (X **or** Y).

**✅ Permitted.** Use freely; most only require keeping the copyright notice and license text.

- *Permissive / public domain*: CC0 1.0, Unlicense, WTFPL, Beerware
- *Notice*:
  - Apache-2.0, Apache Software License 1.1
  - MIT / X11, ISC
  - BSD-2-Clause, BSD-3-Clause, Eclipse Distribution License
  - Python Software Foundation License
  - zlib/libpng, bzip2, Boost Software License
  - AFL 2.1 / 3.0, Artistic License 1.0 / 2.0
  - CC BY (Attribution), Community Data License Agreement – Permissive 2.0
  - Microsoft Public License (MS-PL), NCSA, OpenSSL, PHP License 2.02 / 3.0
  - Unicode License, W3C Software License, ZPL
  - Lucent Public License 1.02, FreeType, libjpeg-turbo, LibTIFF, TCP Wrappers, X.Net, Zend Engine License 2.00, Autodesk DWF Toolkit

**⚠️ Needs approval before use.** These are reciprocal licenses: you can use the code freely, but you must publish any changes you make to it. Raise the dependency in your issue first. Couchbase product management and legal must approve it before the PR can merge, so expect a delay.

- MPL 1.0 / 1.1 / 2.0
- EPL 1.0 / 2.0, CPL 1.0, IPL 1.0
- CDDL 1.0 / 1.1, Apple Public Source License 2.0, Ruby License
- GNU LGPL (any version) and GNU GPL 2.0 with Classpath Exception, only where the linking requirements can be met

**⛔ Not accepted.** Choose an alternative. These are restrictive (copyleft) or source-available licenses:

- GNU GPL (any version), AGPL, GNU LGPL where the linking requirements can't be met
- CC BY-SA, CC BY-ND
- Netscape Public License 1.0 / 1.1, OSL, QPL, Sleepycat, Sybase Open Watcom, Common Public Attribution License, BCL, qmail terms
- Source-available licenses: Business Source License (BSL), Server Side Public License (SSPL), Elastic License, Commons Clause, Redis Source Available License, and Microsoft Ms-LPL / Ms-LRL / Ms-RSL

If a license isn't listed here, treat it as needing approval and ask in the issue.

#### Backwards compatibility

Treat all of the following as public API, for every server — changes require prior discussion and, usually, a deprecation path:

- Tool names, parameter names/types, and return shapes
- CLI subcommands, flags, and environment variables (new ones must also be added to `server.json` for the MCP Registry, and to `README.md`)
- The provider protocols and other `cb_mcp.core` interfaces, including `ServerSpec` field order — append new fields at the end
- Server identity: `spec.id`, `spec.fastmcp_name`, and `spec.logger_namespace`
- Audit event IDs and `src/cb_mcp/audit/descriptor.json` — the ID space is frozen; once records exist against an ID it can never mean anything else
- Transport behavior (stdio, Streamable HTTP, multi-worker HTTP)

All servers ship together under one version, so a fix to one server releases every server. Note in your PR which server(s) a change affects so release notes can be written per server.

### Operational server

Backed by the Couchbase Python SDK (`couchbase>=4.4`), targeting Couchbase Capella and self-managed Couchbase Server.

**Tool modules** (`src/cb_mcp/tools/operational/`): `server.py`, `kv.py`, `query.py`, `query_admin.py`, `index.py`, `vector_index.py`, `fts.py`, `vector_search.py`, `collection_management.py`, `reference.py`.

#### Use the SDK first

- Prefer SDK APIs (`Cluster`, `Bucket`, `Scope`, `Collection`, query/index/search managers) for all cluster operations.
- If a capability is missing from the SDK, raise it in your proposal issue. A direct REST call may be acceptable as a last resort, but it must work over TLS endpoints, must be flagged in the PR, and — if the endpoint is not reachable on Capella — must be gated as described below.

#### Capella and self-managed support

- Connections must work over both `couchbase://` and `couchbases://` (TLS), including Capella's certificate requirements (handled in `src/cb_mcp/utils/operational/connection.py`).
- Prefer features that work on both deployment models. The dividing line is usually REST ports, not features: Capella does not expose the Management (8091/18091) or Index (9102/19102) REST endpoints, and the Query admin endpoints are self-managed only.
- A tool that can only work on one deployment must:
  1. reject the wrong deployment at call time with a clear, actionable error (this is the real enforcement — deployment detection is a hostname heuristic and may resolve nothing), **and**
  2. be listed in `TOOL_DEPLOYMENT_REQUIREMENTS` (`src/cb_mcp/tools/operational/__init__.py`) with a comment naming the endpoint it needs, so it is withheld from clients connected to a deployment where it cannot work.
- Test against **both** Capella and self-managed Couchbase Server, and state the environments you used in your PR (see [Evidence of testing](#evidence-of-testing)). The [Capella free tier](https://docs.couchbase.com/cloud/get-started/create-account.html) and [Couchbase Server via Docker](https://docs.couchbase.com/server/current/install/getting-started-docker.html) make this easy to do locally.

#### Read-only enforcement for SQL++

`run_sql_plus_plus_query` is registered as a read-only tool but accepts any statement, so read-only mode is enforced at runtime by parsing the statement with `lark-sqlpp` (`modifies_data` / `modifies_structure`) and raising `ReadOnlyWriteBlockedError` (`src/cb_mcp/tools/operational/query.py`). Preserve this for any tool that runs user-supplied SQL++, and don't replace it with keyword or regex matching.

#### Multiple workers

The operational server supports `--workers N`, and CI runs its whole integration suite against a 4-worker HTTP server. Its tools must not keep state in process memory between calls.

### Operational Insights server

Backed by the `couchbase-operational-insights` SDK, targeting **self-managed Operational Insights clusters only** — Capella is not supported today.

**Tool modules** (`src/cb_mcp/tools/operational_insights/`): `metadata.py` (databases, scopes, collections, schema), `query.py` (sync/async queries, explain, `COPY TO` export), `index.py`.

- **Connections are HTTP(S) URLs**, not `couchbase://` connection strings (e.g. `http://localhost:8095`), configured through the `CB_OPERATIONAL_INSIGHTS_*` flags in `src/cb_mcp/servers/operational_insights/cli.py`.
- **Prefix every tool name with `operational_insights_`** so it can never collide with an operational tool in a client connected to both servers.
- **Read-only enforcement for queries:** `operational_insights_run_query_sync` / `_async` accept DDL and DML, so read-only mode (and a token lacking the write scope) is enforced at runtime by running the statement with `QueryOptions(readonly=True)`, so the cluster itself rejects DML/DDL — there is no client-side parser. Keep that for any new statement-running tool.
- **Audit classification of statements** uses a deliberate `SELECT`/`EXPLAIN` allow-list (`_audit_statement_class` in `query.py`), because `lark-sqlpp` does not parse the Analytics dialect. Anything not provably a read is audited as a write — keep it fail-closed.
- **`COPY ... TO` needs its own read-only check.** The cluster classifies it as read-only, so `readonly=True` does not block it, but it writes to external storage or a KV collection. The query tools block it client-side in read-only mode — keep that check, and treat `copy_to_*` arguments as a write for annotations and auditing.
- **Async query handles are per-process state** (`src/cb_mcp/utils/operational_insights/handle_registry.py`), which is why this server sets `supports_multiple_workers=False`. New async-style tools should reuse the registry rather than add a second store, and must not assume multi-worker deployment.
- **Result size**: row-returning tools stream through `collect_rows_within_budget(..., limit_bytes=max_query_result_size_for(ctx))`, which honours `--operational-insights-query-result-max-size` and reports `truncated: true` when the budget is hit. Do the same in new row-returning tools.
- **SDK logging:** this server owns the `couchbase_operational_insights` SDK's log forwarding (`sdk_logging.py`); don't initialise it anywhere else.

## 🧪 Testing

The test suite is split into tiers (see [tests/README.md](tests/README.md) for full details):

| Tier | Directory | Requires | Purpose |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | Nothing | Pure Python, fakes/mocks, fast and deterministic |
| Integration | `tests/integration/<server>/` | A live cluster for that server | Real MCP server (stdio or HTTP) against a real cluster |
| Accuracy | `tests/accuracy/` | Couchbase cluster + `OPENAI_API_KEY` | AI-in-the-loop: does an LLM pick the right tool (`tool_calling/`) and get correct answers (`result_validation/`)? Operational server only today |
| Perf | `tests/perf/` | `CB_MCP_PERF=1` | In-process performance tests; opt-in, not run in CI |

Tests mirror `src/`: a server's own tests live in `tests/unit/<server>/` and `tests/integration/<server>/`. Tests of shared machinery, or tests asserting over every server spec (driven by `ALL_SPECS` in `tests/_all_specs.py`), belong at the tier root.

### What we expect from contributions

- **Unit tests are required** for all new logic — call functions in `cb_mcp.*` directly with fakes (Operational Insights has shared fakes in `tests/unit/operational_insights/_operational_insights_fakes.py`).
- **Integration tests are required** for any tool or change that talks to a cluster. Follow the existing pattern (`create_mcp_session` in `tests/integration/conftest.py`) and use `pytest.skip` when required env vars are missing.
- **Changes to shared machinery must be tested across all servers** — parametrize over `ALL_SPECS` rather than testing only the operational server.
- **Test both success and error paths**, plus read-only mode interactions if your tool writes data.
- **Consider adding accuracy test cases** for new operational tools (`tests/accuracy/tool_calling/`) so we catch regressions in how LLMs use them.
- **Let the cross-cutting tests guide you.** Several unit tests fail on purpose when a new tool or setting is missing a declaration — e.g. `test_server_specs.py` (annotations, tool-name collisions, deployment requirements), `test_audit_classification.py`, and `test_settings_classification.py`. Fix the declaration rather than the test.
- **All existing tests must pass.**

### Running tests

```bash
# Unit tests for every server (no cluster needed)
uv run pytest tests/unit/

# Everything (integration/accuracy tiers whose env vars are unset are skipped)
uv run pytest
```

**Operational server** integration tests:

```bash
export CB_CONNECTION_STRING="couchbases://..."
export CB_USERNAME="username"
export CB_PASSWORD="password"
export CB_MCP_TEST_BUCKET="travel-sample"

# Populate test data / indexes first
uv run scripts/setup_test_data.py

uv run pytest tests/integration/operational/
```

**Operational Insights server** integration tests (skipped unless `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING` is set). [tests/README.md](tests/README.md) has the full Docker setup for a local cluster with an S3 mock; once it is running:

```bash
export CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING="http://127.0.0.1:8095"
export CB_OPERATIONAL_INSIGHTS_USERNAME="Administrator"
export CB_OPERATIONAL_INSIGHTS_PASSWORD="password"

uv run pytest tests/integration/operational_insights/
```

#### What CI runs

Every PR runs both workflows:

- **`test.yml`** — unit tests for all servers, plus the operational integration suite across transport (`stdio`, `http`) × Couchbase Server (`8.0.0`, `7.6.11`) × server binary (`source`, `docker`, `pypi`), and a 4-worker HTTP run for each binary. Reproduce locally with `./scripts/run_matrix_local.sh` (requires Docker), or a subset:

  ```bash
  CB_VERSIONS="8.0.0" TRANSPORTS="stdio" SERVER_BINARIES="source" ./scripts/run_matrix_local.sh
  ```

- **`operational-insights-integration-tests.yml`** — the Operational Insights integration suite against a self-managed Operational Insights cluster (Enterprise Analytics 2.2.1 image) with an S3 mock. Reproduce locally with `./scripts/run_operational_insights_matrix_local.sh`.

### Evidence of testing

Every PR must describe how the change was tested. Include in the PR description:

- **Test output**: the `pytest` command(s) you ran and a summary of the results
- **Environment**: the cluster(s) you tested against, with versions —
  - *Operational server*: both Capella and self-managed Couchbase Server (e.g. via Docker)
  - *Operational Insights server*: a self-managed Operational Insights cluster
  - *Shared machinery*: unit tests for all servers, plus integration evidence for each server whose behaviour the change touches
- **Manual verification**: which MCP client you used (Claude Desktop, Cursor, MCP Inspector, etc.), which server, and what you exercised — screenshots or transcripts of tool calls are very helpful
- **Read-only mode**: for write tools, confirmation that the tool is correctly hidden/blocked in read-only mode

PRs without testing evidence will be sent back for it before review.

## 🛠️ Development Workflow

1. **Find or open an issue** describing the change and which server it targets. For new tools, agree on the interface in the issue first.
2. **Create a branch** for your feature/fix:

   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes**, following existing patterns and the design guidelines above.
4. **Run checks locally**:

   ```bash
   uv run pre-commit run --all-files   # Ruff lint + format, plus file hygiene hooks
   uv run pytest tests/unit/
   uv run pytest tests/integration/<server>/   # with that server's cluster configured
   ```

5. **Verify manually** with an MCP client. Always pass the subcommand explicitly — a bare invocation still defaults to `operational`, but that default will be deprecated.

   ```bash
   # Operational server
   uv run src/mcp_server.py operational --connection-string "couchbases://..." --username "..." --password "..."

   # Operational Insights server (HTTP URL, not couchbase://)
   uv run src/mcp_server.py operational-insights --connection-string "http://localhost:8095" --username "..." --password "..."
   ```

   Useful options for either server:

   ```bash
   # With write operations enabled
   ... --read-only-mode false

   # With confirmation required / tools disabled
   ... --confirmation-required-tools "tool_a,tool_b"
   ... --disabled-tools "tool_a,tool_b"

   # Over Streamable HTTP
   ... --transport http

   # With several workers (operational server only — checks your tool keeps no per-process state)
   ... --transport http --workers 4

   # With audit logging on, to check your tool's audit records
   ... --audit-log-enabled true
   ```

   Call the `get_server_configuration_status` tool from your client to see what the server actually resolved.

6. **Update documentation** if you changed user-facing behavior: `README.md` (the right server's tool table and configuration section), `DOCKER.md`, and `server.json` for new environment variables. Note in the PR if the [product docs](https://docs.couchbase.com/mcp-server/get-started/overview.html) need a matching update.
7. **Commit** using descriptive messages. PRs are squash-merged, so the PR title becomes the commit message on `main`: start it with the JIRA key when there is one, e.g. `DA-2029: Add query health tools`, otherwise a short imperative summary such as `Add: get bucket config tool` or `Fix: handle missing scope in kv get`. Pre-commit hooks will auto-fix formatting.

## 🤝 Submitting Changes

1. **Push your branch** and create a pull request. If you are working from a fork, follow [these instructions](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork).
2. **Keep it small**: if the diff is doing more than one thing, split it. A small set of closely related tools counts as one thing; unrelated tools do not.
3. **Fill in the PR description** — the [PR template](.github/PULL_REQUEST_TEMPLATE.md) is pre-populated when you open a PR:
   - Which server(s) does this change affect?
   - What does this change do, and which issue does it resolve?
   - Why is this change needed?
   - How have you tested it? (see [Evidence of testing](#evidence-of-testing))
   - Any compatibility considerations (deployment support, managed MCP, tool interface changes)?
4. **Respond to review feedback** — we aim to review promptly, and small focused PRs get merged fastest.

### PR Checklist

- [ ] Linked to an issue (required for new tools). The issue can be on JIRA (preferred for internal contributors) or GitHub.
- [ ] States which server(s) the change affects
- [ ] Uses the server's SDK (REST fallback justified in the description, if any)
- [ ] Works on every deployment the server targets (operational: Capella + self-managed, or declared in `TOOL_DEPLOYMENT_REQUIREMENTS` with a runtime check; Operational Insights: self-managed)
- [ ] No changes to `cb_mcp.core` contracts / managed MCP interfaces (or discussed first)
- [ ] Unit tests added/updated (across all servers for shared-machinery changes)
- [ ] Integration tests added/updated (for cluster-touching changes)
- [ ] Read-only mode, tool annotations, and audit classification handled (for new/changed tools)
- [ ] Results are bounded and no new cross-call state was added without discussion
- [ ] Evidence of testing included in the PR description
- [ ] Docs updated (README, DOCKER.md, `server.json`) for user-facing changes
- [ ] Lint and pre-commit pass

### A note on AI-generated code

We welcome contributions developed with AI assistance — this is an MCP server, after all. However, you are responsible for the code you submit: review and understand every line, verify it actually runs against a real cluster, and don't submit unverified, generated PRs. Testing evidence is required precisely so that "it looks plausible" is never the bar.

## 🏗️ Project Structure

```
mcp-server-couchbase/
├── src/
│   ├── mcp_server.py            # CLI entry point: a click group, one subcommand per server
│   ├── providers/               # Standalone-host provider implementations
│   │   ├── operational.py       # OperationalClusterProvider (CLI/env config)
│   │   └── operational_insights.py  # OperationalInsightsClusterProvider
│   └── cb_mcp/                  # Reusable package shared with managed MCP implementations
│       ├── core/                # Server-agnostic machinery
│       │   ├── contracts.py     # Host-agnostic contracts (ProviderLifecycle, ClusterProvider)
│       │   ├── spec.py          # ServerSpec / ToolSet / ScopeSpec / Deployment — a server, as data
│       │   ├── app.py           # build_app(spec, ...) -> FastMCP, and run_app
│       │   ├── serving.py       # HTTP serving, including the multi-worker supervisor
│       │   └── cli/             # DefaultGroup + reusable Click option stacks
│       ├── audit/               # Audit logging: event catalogue, tool classification, middleware
│       ├── servers/             # One package per server
│       │   ├── operational/     # Identity constants and the SPEC itself
│       │   └── operational_insights/  # Same, plus its own credential stack (cli.py)
│       ├── tools/               # Tool implementations, one subpackage per server
│       │   ├── status.py        # get_server_configuration_status, shared by every server
│       │   ├── operational/     # server.py, kv.py, query.py, index.py, fts.py, vector_search.py, ...
│       │   └── operational_insights/  # metadata.py, query.py, index.py
│       ├── utils/               # Shared helpers (config, context, logging, telemetry,
│       │   │                    #   query_limits, responses, sqlpp, scope_enforcement)
│       │   ├── operational/     # Couchbase-SDK-specific helpers, embedding providers, bundled CA certs
│       │   └── operational_insights/  # couchbase_operational_insights-specific helpers
│       ├── auth.py              # OAuth JWT verification
│       └── tool_registration.py # Gating and wrapping: disabled, deployment, confirmation, scopes
├── scripts/                     # Lint, test-data setup, local CI matrix, version bump scripts
├── tests/
│   ├── unit/                    # Pure Python tests (no cluster); shared-machinery tests at the root
│   │   ├── operational/           # Operational-server-only unit tests
│   │   └── operational_insights/  # Operational Insights-only unit tests
│   ├── integration/             # Shared session plumbing; tests live per server
│   │   ├── operational/           # Tests against a live Couchbase cluster
│   │   └── operational_insights/  # Tests against a live Operational Insights cluster, env-gated
│   ├── perf/                    # In-process performance tests, opt-in via CB_MCP_PERF=1
│   ├── accuracy/                # AI-in-the-loop accuracy tests (see tests/README.md)
│   └── _all_specs.py            # Test-only ALL_SPECS registry (see its docstring)
├── pyproject.toml               # Dependencies, Ruff and pytest config
├── server.json                  # MCP Registry manifest (packages, env vars)
├── Dockerfile / DOCKER.md       # Container build and usage
├── RELEASE.md                   # Release process
└── README.md                    # Usage documentation
```

### How a server is assembled

Everything server-specific is declared as data in a `ServerSpec` (`core/spec.py`):
its tools, OAuth scope labels, logger namespace, wire-visible name, default port
and log file. `core/app.py`'s `build_app(spec, ...)` turns that into a running
`FastMCP` application, so adding a server means adding a spec, a provider and a
subcommand — not touching the shared machinery.

Three names are deliberately distinct, because they have different
compatibility contracts:

| name | example | contract |
|---|---|---|
| `spec.id` | `operational` | the CLI subcommand and telemetry dimension |
| `spec.fastmcp_name` | `couchbase-operational` | wire-visible; clients see it as `serverInfo.name` |
| `spec.logger_namespace` | `couchbase.mcp.operational` | operator-facing; appears in every log line |

The second server, `operational-insights` / `couchbase-operational-insights` /
`couchbase.mcp.operational-insights`, follows the same pattern — see
`src/cb_mcp/servers/operational_insights/` and the
[servers table](#-servers-in-this-repository).

### Adding a new MCP server

A server is a spec plus a provider plus a subcommand. The shared machinery in
`core/` should not need changing — if it does, that is worth raising in the
issue first.

**1. Pick the three names.** They are permanent once shipped, and they are not
interchangeable (see the table above). Check the logger namespace against the
backing SDK's own modules before committing to it:

```python
import couchbase, pkgutil
{m.name for m in pkgutil.walk_packages(couchbase.__path__, "couchbase.")}
```

`couchbase.analytics`, for instance, is a real module in the operational SDK, so
it cannot be used as a logger namespace. Nesting under `couchbase.mcp.<id>`
avoids the problem entirely.

**2. Declare the tools** in `src/cb_mcp/tools/<id>/`, exporting a `ToolSet` and a
`TOOL_ANNOTATIONS` mapping from the package `__init__`. Tool *names* should be
globally unique across all servers — a client connected to two servers sees one
flat namespace, so a duplicate name is ambiguous to it. Prefer a unique name,
e.g. a server-specific prefix (`operational-insights` prefixes every
tool name with `operational_insights_` for exactly this reason). If a name genuinely must be
duplicated — each server having its *own* implementation behind it — add it to
`KNOWN_DUPLICATE_TOOL_NAMES` in `tests/unit/test_server_specs.py` with a
one-line reason. The test still fails on any *new*, undocumented collision.

A name on two servers because they register the **same function object** is a
different thing, and goes in `SHARED_TOOL_NAMES` instead: that is not a
collision — a client connected to both gets identical behaviour whichever it
reaches. Today that is `get_server_configuration_status`
(`src/cb_mcp/tools/status.py`), which every server must expose so an operator
can always ask what read-only mode, disabled tools, OAuth and logging resolved
to without a cluster. The tests enforce the distinction rather than trusting
the list: a "shared" name whose servers register *different* functions fails,
as does a shared name some server forgot to register. Put a genuinely
server-agnostic tool in `src/cb_mcp/tools/` next to `status.py`, not inside a
server's subpackage — if its body needs an SDK, it is not shared.

**3. Declare the spec** in `src/cb_mcp/servers/<id>/spec.py`:

```python
SPEC = ServerSpec(
    id=SERVER_ID,                       # CLI subcommand, telemetry dimension
    fastmcp_name=FASTMCP_SERVER_NAME,   # wire-visible; must differ per server
    logger_namespace=...,               # under LOGGER_NAMESPACE; must differ
    display_name=...,                   # shown in OAuth protected-resource metadata
    tools=TOOL_SET,
    scopes=ScopeSpec(read=SCOPE_READ, write=SCOPE_WRITE),
    default_port=8001,                  # required, and must differ per server
    default_log_file="mcp_server_<id>.log",   # required, and must differ
    annotations=TOOL_ANNOTATIONS,
    scope_hints=...,                    # optional per-tool scope explanations
    deployment_requirements=...,        # optional; tools that only work on one deployment
    deployment_resolver=...,            # required if deployment_requirements is non-empty
    sdk_log_hook=...,                   # see below
    reported_dependencies=("your-sdk",),
    safe_settings_keys=(),
    secret_settings_keys=(),            # anything beyond the shared set
    audit_package="<id>",               # None = not audited; see step 6
    supports_multiple_workers=True,     # False only if tools keep state between calls
)
```

Pass fields by keyword. `ServerSpec` is a shared `cb_mcp.core` contract, so a
new field is always appended at the end of the class, never inserted mid-way.

Keep `src/cb_mcp/servers/<id>/__init__.py` **inert** — a docstring and nothing
else. Importing any submodule runs the package `__init__` first, so if it
imported the spec (which imports the tools, which import the server's
constants) you get a circular import. `tests/unit/test_logger_names.py`
imports each package first in a clean subprocess to catch this.

**4. Write the provider** in `src/providers/<id>.py`. It must satisfy
`ProviderLifecycle` in `core/contracts.py` — that is all the shared machinery
calls — and `close()` should encapsulate whatever teardown its client needs;
the shared lifespan just calls `close()` and should never type-switch on the
client. If your tools need more from the provider than those three methods
(a `get_cluster` of your SDK's type, or extra state like the Operational
Insights server's `handle_registry`), declare a protocol extending
`ProviderLifecycle` next to your service's helpers, not in `core/` — `core`
must stay free of every SDK. Add both to
`tests/unit/test_provider_contracts.py`, which is what replaced the
misleading `runtime_checkable`/`isinstance` check these protocols used to
carry.

**5. Add the subcommand** in `src/mcp_server.py`. Every subcommand has the same
shape, and `server_options` (`src/cb_mcp/utils/cli_params.py`) supplies the
whole shared flag surface in one canonical order, so you only pass what
differs:

```python
@main.command("<id>", short_help="...")
@server_options(
    credentials=YOUR_CREDENTIALS,       # a CredentialProfile in cli_params.py, if
                                        # your credentials differ from the cluster ones
    default_port=DEFAULT_YOUR_PORT,
    default_log_file=DEFAULT_YOUR_LOG_FILE,
)
@click.version_option(package_name="couchbase-mcp-server", prog_name="couchbase-mcp-server <id>")
def your_server(**params: Any) -> None:
    """Run the ... MCP server."""
    _start_server("<id>", params)
```

Then add a loader for your spec/provider pair to `_SERVERS`, keyed by the
subcommand name — that is also how `--workers` processes find their server
without the command line. The loader imports the spec and provider *lazily* so
a process only loads the SDK of the server it is actually running.
`_build_server` should not need to change; if it does, the thing you are adding
is probably not a server.

**6. Register for auditing** (if the server should be audited — new servers
should be). In `src/cb_mcp/audit/`: add a Tier-2 block to
`SERVICE_PACKAGE_BLOCKS` and its slots to `CATEGORY_SLOTS` in `catalog.py`, a
classification table in `classification.py` (including the fail-closed
category), and regenerate `descriptor.json` — `tests/unit/test_audit_catalog.py`
asserts code and descriptor never drift. Then set `audit_package` on the spec.
Existing block numbers and IDs are never renumbered.

**7. Add tests.** Beyond the tool tests:
- add the new spec to `tests/_all_specs.py`'s `ALL_SPECS` — that alone feeds
  most of the cross-server invariants in `tests/unit/test_server_specs.py`
  (unique ports/log files/namespaces, every tool annotated, tool-name
  collisions against the allow-list);
- add every logger-bearing module to `EXPECTED_LOGGER_NAMES` in
  `tests/unit/test_logger_names.py`, and its packages to
  `test_packages_import_in_any_order`;
- add a `(argv, spec)` case to `tests/unit/test_settings_classification.py`'s
  `SERVERS`;
- add explicit-subcommand cases to `tests/unit/test_default_group.py`, and the
  subcommand to `SUBCOMMANDS` in `tests/unit/test_workers_cli.py`;
- add a case to `tests/unit/test_sdk_isolation.py` asserting your server's
  spec + provider load *your* SDK and no other server's — this is what keeps
  the lazy-import rule in step 5 true rather than aspirational;
- put your server's own unit tests in `tests/unit/<id>/` and its integration
  tests in `tests/integration/<id>/`, mirroring `src/`. Only tests covering
  shared machinery or asserting over every spec belong at the tier root;
- if the server needs a live cluster to test against, make that integration
  subdirectory env-gated so it auto-skips when its credentials are unset —
  see `tests/integration/operational_insights/conftest.py` for the pattern.

#### Constraints that are easy to miss

- **Only one server may own an SDK's logging.** `couchbase.configure_logging`
  is one-shot per process. A server that does not own its SDK's logging must
  pass `NO_SDK_LOG_HOOK`, not leave `sdk_log_hook` unset and hope.
- **Handlers attach at `LOGGER_ROOT` (`couchbase`), never per server.** That
  root is also the Couchbase SDK's own namespace, which is the *only* reason
  its `threshold` / `metrics` / `transactions` records get captured. Moving the
  attach point silently drops them.
- **`default_port` and `default_log_file` are required for a reason.** Two
  servers on one port cannot both bind; two servers sharing a log base path
  corrupt each other's rotation, since `RotatingFileHandler` is not
  multi-process safe.
- **Every settings key must be classified.** Redaction is allow-list based, so
  an unclassified key is silently dropped from the diagnostic record. Declare
  it in `safe_settings_keys` or `secret_settings_keys`;
  `tests/unit/test_settings_classification.py` fails the build otherwise.
- **One distribution, one version.** All servers ship together, so a fix in one
  bumps the version for every user of the others. Release notes need
  per-server sections.
- **Multi-worker mode assumes tools are stateless.** `--workers N` runs N
  processes behind one socket with stateless HTTP sessions, so consecutive
  calls can land on different processes. A server whose tools keep state
  between calls in process memory must set `supports_multiple_workers=False`.
- **The Docker image's `ENV CB_MCP_PORT` would override every server's
  `default_port`**, since an env var always beats a Click default — this is
  why the runtime stage does not hard-set it. Don't reintroduce a bare
  `CB_MCP_PORT` default in the Dockerfile without namespacing it per server.

## 💡 Tips for Contributors

```bash
# Install new dependencies (discuss runtime deps in an issue first)
uv add package-name

# Install new dev dependencies (they live in the `dev` extra)
uv add --optional dev package-name

# Update all package dependencies to the latest compatible versions
uv sync --upgrade
```

### Debugging

- **Use logging**: hierarchical loggers under `LOGGER_NAMESPACE` (`couchbase.mcp`),
  or under a server's own namespace for server-specific modules
- **Check connection**: ensure your Couchbase cluster is reachable and credentials have the needed RBAC roles
- **Validate configuration**: make sure required environment variables / CLI flags are set, then call the `get_server_configuration_status` tool to see what read-only mode, disabled/withheld tools, OAuth, logging, and auditing actually resolved to (no cluster needed)
- **Missing tool?** It may be withheld by `TOOL_DEPLOYMENT_REQUIREMENTS` (e.g., a self-managed-only tool on a Capella connection), excluded by read-only mode, or listed in `--disabled-tools` — the status tool and the startup log say which
- **MCP Inspector**: [`npx @modelcontextprotocol/inspector`](https://modelcontextprotocol.io/docs/tools/inspector) is handy for exercising tools without a full client

## 📖 Additional Resources

- **[Model Context Protocol Documentation](https://modelcontextprotocol.io/)**
- **[Couchbase Python SDK Documentation](https://docs.couchbase.com/python-sdk/current/hello-world/start-using-sdk.html)**
- **[SQL++ Query Language](https://www.couchbase.com/sqlplusplus/)**
- **[Ruff Documentation](https://docs.astral.sh/ruff/)**

## 🆘 Getting Help

- **Open an issue** for bugs, feature requests, or tool proposals
- **Check existing issues** for similar problems or in-flight work
- **Review the code** for examples and patterns

Thank you for contributing to the Couchbase MCP Server! 🚀
