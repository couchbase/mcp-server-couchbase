# Contributing to Couchbase MCP Server

Thank you for your interest in contributing to the Couchbase MCP Server! This guide explains how to set up your development environment, the design principles we expect contributions to follow, and what we look for when reviewing pull requests.

## 📜 Contribution Principles

These are the ground rules we review every contribution against. Reading this section first will save you review round-trips.

1. **Discuss before you build.** Open an issue before writing code for any new tool or significant change — a GitHub issue for external contributors, or a JIRA ticket for Couchbase internal contributors (see the [issue template](.github/JIRA_ISSUE_TEMPLATE.md)). For new tools, we want to design the tool interface (name, parameters, return shape, annotations) together *before* implementation — a tool's interface is a public API for MCP clients and is hard to change later.
   - Use a **single issue per class of related tools** (e.g., one issue proposing a set of Search tools) rather than one issue per tool.
   - Explain **why the tools are needed** with as detailed a use case as possible — what an AI agent cannot accomplish today, and how it would use the proposed tools.
2. **Keep pull requests small and focused.** One tool, one bug fix, or one refactor per PR. Small PRs get reviewed and merged much faster. If your change is large, split it into a series of smaller PRs and mention the plan in the issue.
3. **Use the Couchbase Python SDK wherever possible.** Prefer the official [Couchbase Python SDK](https://docs.couchbase.com/python-sdk/current/hello-world/start-using-sdk.html) over raw REST calls. Only fall back to REST endpoints when the SDK genuinely does not expose the capability, and call that out explicitly in your PR.
4. **Support both Capella and self-managed Couchbase Server.** Every feature must work against Couchbase Capella *and* the self-managed Couchbase Server versions supported by this MCP server. Don't rely on APIs, ports, or settings that are unavailable on Capella (e.g., features requiring direct node access) without discussing it in an issue first.
5. **Don't break the managed MCP interfaces.** The `cb_mcp` package is shared with managed (hosted) MCP server implementations via the host-agnostic contracts in `src/cb_mcp/core/contracts.py` (notably `ClusterProvider`). See [Host-agnostic design](#host-agnostic-design) below.
6. **Test your changes — and show it.** New code needs unit tests and, where it touches a live cluster, integration tests. PRs must include evidence of testing (see [Evidence of testing](#evidence-of-testing)).
7. **Preserve backwards compatibility.** Tool names, parameters, return shapes, CLI flags, and environment variables are all public interfaces. Breaking changes need prior discussion in an issue.

## 🚀 Development Setup

### Prerequisites

- **Python 3.10+**: Required for the project
- **[uv](https://docs.astral.sh/uv/)**: Fast Python package installer and dependency manager
- **Git**: For version control
- **Couchbase clusters**: a [Capella free tier](https://docs.couchbase.com/cloud/get-started/create-account.html) cluster *and* a self-managed Couchbase Server (e.g., [via Docker](https://docs.couchbase.com/server/current/install/getting-started-docker.html)) — changes must be tested against both
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

### Use the SDK first

The server is a thin adapter between MCP and the Couchbase Python SDK (`couchbase>=4.4`):

- Prefer SDK APIs (`Cluster`, `Bucket`, `Scope`, `Collection`, query/index managers) for all cluster operations.
- If a capability is missing from the SDK, raise it in your proposal issue. A direct REST call may be acceptable as a last resort, but it must work on both Capella and self-managed clusters (including TLS endpoints) and must be flagged in the PR.

### Capella and self-managed support

- Connections must work over both `couchbase://` and `couchbases://` (TLS), including Capella's certificate requirements (handled in `src/cb_mcp/utils/operational/connection.py`).
- Avoid features that only exist in one deployment model, or gate them gracefully with a clear error message when unavailable.
- Test against **both** Capella and self-managed Couchbase Server, and state the environments you used in your PR (see [Evidence of testing](#evidence-of-testing)). The [Capella free tier](https://docs.couchbase.com/cloud/get-started/create-account.html) and [Couchbase Server via Docker](https://docs.couchbase.com/server/current/install/getting-started-docker.html) make this easy to do locally.

### Host-agnostic design

The `cb_mcp` package is reused by managed MCP server implementations, not just the standalone CLI in this repo. To keep it portable:

- **Tools must obtain the cluster through the request context / `ClusterProvider`** (`src/cb_mcp/core/contracts.py`) — never from global state, CLI arguments, or environment variables read inside `cb_mcp`.
- **Don't read CLI/env configuration inside `cb_mcp`.** Configuration parsing belongs to the host (`src/mcp_server.py` and `src/providers/`).
- **Don't change the `ClusterProvider` protocol** (or other contracts in `core/`) without prior discussion — managed implementations depend on it.
- Provider configuration returned for status reporting must never include secrets (return `_configured` booleans instead).

### Tool design

New tools are the most common contribution. Before implementing any, open a single issue covering the class of related tools you're proposing, following the [issue template](.github/JIRA_ISSUE_TEMPLATE.md). The issue should include:

- **The use case, in as much detail as possible**: what task an AI agent cannot accomplish with the existing tools, a concrete example prompt/workflow, and how the agent would use the proposed tools to complete it
- **The proposed interface** for each tool: name, parameters, return shape, and annotations
- **Deployment considerations**: whether the capability is available on both Capella and self-managed Couchbase Server, and via the SDK

When implementing:

1. **Create the tool function** in the appropriate module under the server's tool package — for the operational server, `src/cb_mcp/tools/operational/` (`server.py`, `kv.py`, `query.py`, or `index.py`) — or propose a new module in your issue if none fits
2. **Export the tool** in that package's `__init__.py` and add it to `__all__`
3. **Add it to the correct half of `TOOL_SET`**: `read_only` if it only reads data, or `write` if it modifies data (so it is not loaded at all under read-only mode)
4. **Add an entry to `TOOL_ANNOTATIONS`** with accurate hints (`readOnlyHint`, `idempotentHint`, `destructiveHint`) — clients rely on these for safety decisions
5. **Respect read-only mode**: any tool that can modify data or cluster state must be excluded when `read_only_mode` is enabled
6. **Support confirmation**: destructive tools should work with the confirmation/elicitation mechanism (`src/cb_mcp/utils/elicitation.py`)
7. **Be token-conscious**: tool output goes into an LLM context window. Bound result sizes rather than returning unbounded data — e.g., a `limit` parameter with a sensible default, as existing query tools use
8. **Write tests** (unit + integration) and verify the tool end-to-end with an MCP client

### Security

- Never log or return credentials, connection strings containing passwords, or certificate contents.
- Validate and constrain inputs at the tool boundary; remember tool parameters are LLM-generated and may be malformed or adversarial.
- Query tools must preserve the SQL++ read/write classification (`src/cb_mcp/utils/operational/query_utils.py`) so read-only mode cannot be bypassed.
- The server makes no network calls other than MCP transport and the configured Couchbase cluster. Don't add telemetry, update checks, or third-party callbacks.

### Dependencies

Keep the dependency footprint small — this server is distributed via PyPI, Docker, and embedded in managed environments.

- Discuss any new runtime dependency in an issue before adding it.
- Prefer the standard library or existing dependencies (`couchbase`, `fastmcp`, `click`).
- New dependencies must be permissively licensed and actively maintained.

### Backwards compatibility

Treat all of the following as public API — changes require prior discussion and, usually, a deprecation path:

- Tool names, parameter names/types, and return shapes
- CLI flags and environment variables
- The `ClusterProvider` contract and other `cb_mcp.core` interfaces
- Transport behavior (stdio, Streamable HTTP)

## 🧪 Testing

The test suite is split into three tiers (see [tests/README.md](tests/README.md) for full details):

| Tier | Directory | Requires | Purpose |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | Nothing | Pure Python, fakes/mocks, fast and deterministic |
| Integration | `tests/integration/` | Live Couchbase cluster | Real MCP server over stdio against a real cluster |
| Accuracy | `tests/accuracy/` | Cluster + `OPENAI_API_KEY` | AI-in-the-loop: does an LLM pick the right tool and get correct answers? |

### What we expect from contributions

- **Unit tests are required** for all new logic — call functions in `cb_mcp.*` directly with fakes.
- **Integration tests are required** for any tool or change that talks to a cluster. Follow the existing pattern (`create_mcp_session`) and use `pytest.skip` when required env vars are missing.
- **Test both success and error paths**, plus read-only mode interactions if your tool writes data.
- **Consider adding accuracy test cases** for new tools (`tests/accuracy/tool_calling/`) so we catch regressions in how LLMs use them.
- **All existing tests must pass.**

### Running tests

```bash
# Unit tests (no cluster needed)
uv run pytest tests/unit/

# Integration tests (needs a live cluster)
export CB_CONNECTION_STRING="couchbases://..."
export CB_USERNAME="username"
export CB_PASSWORD="password"
export CB_MCP_TEST_BUCKET="travel-sample"
uv run pytest tests/integration/

# Populate test data / indexes for integration tests
uv run scripts/setup_test_data.py

# Everything
uv run pytest
```

### Evidence of testing

Every PR must describe how the change was tested. Include in the PR description:

- **Test output**: the `pytest` command(s) you ran and a summary of the results
- **Environment**: confirmation that you tested against both Capella and self-managed Couchbase Server (and the server version, e.g. via Docker)
- **Manual verification**: which MCP client you used (Claude Desktop, Cursor, MCP Inspector, etc.) and what you exercised — screenshots or transcripts of tool calls are very helpful
- **Read-only mode**: for write tools, confirmation that the tool is correctly hidden/blocked in read-only mode

PRs without testing evidence will be sent back for it before review.

## 🛠️ Development Workflow

1. **Find or open an issue** describing the change. For new tools, agree on the interface in the issue first.
2. **Create a branch** for your feature/fix:

   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes**, following existing patterns and the design guidelines above.
4. **Run checks locally**:

   ```bash
   ./scripts/lint.sh
   uv run pre-commit run --all-files
   uv run pytest tests/unit/
   uv run pytest tests/integration/   # with a cluster configured
   ```

5. **Verify manually** with an MCP client:

   ```bash
   # Run the server for testing
   # Bare invocation runs the operational server; `operational` is equivalent.
   uv run src/mcp_server.py --connection-string "..." --username "..." --password "..."

   # With write operations enabled
   uv run src/mcp_server.py ... --read-only-mode false

   # With confirmation required for specific tools
   uv run src/mcp_server.py ... --confirmation-required-tools "delete_document_by_id,replace_document_by_id"

   # With specific tools disabled
   uv run src/mcp_server.py ... --disabled-tools "upsert_document_by_id,delete_document_by_id"
   ```

6. **Update documentation** if you changed user-facing behavior: `README.md`, `DOCKER.md`.
7. **Commit** using descriptive messages (e.g., `feat: add collection stats tool`, `fix: handle missing scope in kv get`). Pre-commit hooks will auto-fix formatting.

## 🤝 Submitting Changes

1. **Push your branch** and create a pull request. If you are working from a fork, follow [these instructions](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork).
2. **Keep it small**: if the diff is doing more than one thing, split it.
3. **Fill in the PR description** — the [PR template](.github/PULL_REQUEST_TEMPLATE.md) is pre-populated when you open a PR:
   - What does this change do, and which issue does it resolve?
   - Why is this change needed?
   - How have you tested it? (see [Evidence of testing](#evidence-of-testing))
   - Any compatibility considerations (Capella vs. self-managed, managed MCP, tool interface changes)?
4. **Respond to review feedback** — we aim to review promptly, and small focused PRs get merged fastest.

### PR Checklist

- [ ] Linked to an issue (required for new tools). The issue can be on JIRA (preferred for internal contributors) or GitHub.
- [ ] Uses the Couchbase SDK (REST fallback justified in the description, if any)
- [ ] Works on both Capella and self-managed Couchbase Server
- [ ] No changes to `cb_mcp.core` contracts / managed MCP interfaces (or discussed first)
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated (for cluster-touching changes)
- [ ] Read-only mode and tool annotations handled (for new/changed tools)
- [ ] Evidence of testing included in the PR description
- [ ] Docs updated (README, DOCKER.md) for user-facing changes
- [ ] Lint and pre-commit pass

### A note on AI-generated code

We welcome contributions developed with AI assistance — this is an MCP server, after all. However, you are responsible for the code you submit: review and understand every line, verify it actually runs against a real cluster, and don't submit unverified, generated PRs. Testing evidence is required precisely so that "it looks plausible" is never the bar.

## 🏗️ Project Structure

```
mcp-server-couchbase/
├── src/
│   ├── mcp_server.py            # CLI entry point: a click group, one subcommand per server
│   ├── providers/               # Standalone-host provider implementations
│   │   └── static.py            # StaticClusterProvider (CLI/env config)
│   └── cb_mcp/                  # Reusable package shared with managed MCP implementations
│       ├── core/                # Server-agnostic machinery
│       │   ├── contracts.py     # Host-agnostic contracts (ClusterProvider, ...)
│       │   ├── spec.py          # ServerSpec / ToolSet / ScopeSpec — a server, as data
│       │   ├── app.py           # build_app(spec, ...) -> FastMCP, and run_app
│       │   └── cli/             # DefaultGroup + reusable Click option stacks
│       ├── servers/             # One package per server
│       │   └── operational/     # Identity constants and the SPEC itself
│       ├── tools/               # Tool implementations, one subpackage per server
│       │   └── operational/     # server.py, kv.py, query.py, index.py, ...
│       ├── utils/               # Shared helpers (config, context, logging, telemetry)
│       │   └── operational/     # Couchbase-SDK-specific helpers + bundled CA certs
│       └── tool_registration.py # Gating and wrapping: disabled, confirmation, scopes
├── scripts/                     # Lint, test-data setup, version bump scripts
├── tests/
│   ├── unit/                    # Pure Python tests (no cluster)
│   ├── integration/             # Tests against a live Couchbase cluster
│   └── accuracy/                # AI-in-the-loop accuracy tests (see tests/README.md)
├── pyproject.toml               # Dependencies, Ruff and pytest config
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
`TOOL_ANNOTATIONS` mapping from the package `__init__`. Tool *names* must be
globally unique across all servers — a client connected to two servers sees one
flat namespace.

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
    sdk_log_hook=...,                   # see below
    reported_dependencies=("your-sdk",),
    safe_settings_keys=(), secret_settings_keys=(),   # anything beyond the shared set
)
```

Keep `src/cb_mcp/servers/<id>/__init__.py` **inert** — a docstring and nothing
else. Importing any submodule runs the package `__init__` first, so if it
imported the spec (which imports the tools, which import the server's
constants) you get a circular import. `tests/unit/test_logger_names.py`
imports each package first in a clean subprocess to catch this.

**4. Write the provider** in `src/providers/<id>.py`. It must satisfy the
`ClusterProvider` shape in `core/contracts.py`, and `close()` should encapsulate
whatever teardown its client needs — the shared lifespan just calls `close()`
and should never type-switch on the client.

**5. Add the subcommand** in `src/mcp_server.py`. Reuse the shared option stacks
and supply only what differs:

```python
@main.command("<id>")
@your_credential_options          # per-server: different SDK, different auth
@read_only_option
@transport_options(default_port=SPEC.default_port)
@tool_gating_options
@logging_options(default_log_file=SPEC.default_log_file)
@oauth_options
def your_server(...):
    ...
```

Import the spec lazily inside the subcommand so a process only loads the SDK of
the server it is actually running.

**6. Add tests.** Beyond the tool tests, extend the cross-server checks: tool
names unique across specs, annotations covering every tool, and the logger
snapshot in `tests/unit/test_logger_names.py`.

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

## 💡 Tips for Contributors

```bash
# Install new dependencies (discuss runtime deps in an issue first)
uv add package-name

# Install new dev dependencies
uv add --dev package-name

# Update all package dependencies to the latest compatible versions
uv sync --upgrade
```

### Debugging

- **Use logging**: hierarchical loggers under `LOGGER_NAMESPACE` (`couchbase.mcp`),
  or under a server's own namespace for server-specific modules
- **Check connection**: ensure your Couchbase cluster is reachable and credentials have the needed RBAC roles
- **Validate configuration**: make sure required environment variables / CLI flags are set
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
