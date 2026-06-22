# Contributing to Couchbase MCP Server

Thank you for your interest in contributing to the Couchbase MCP Server! This guide will help you set up your development environment and understand our development workflow.

## 🚀 Development Setup

### Prerequisites

- **Python 3.10+**: Required for the project
- **[uv](https://docs.astral.sh/uv/)**: Fast Python package installer and dependency manager
- **Git**: For version control
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

### Manual Linting

```bash
# Check code quality (no changes made)
./scripts/lint.sh
# or: uv run ruff check src/

# Auto-fix issues
./scripts/lint_fix.sh
# or: uv run ruff check src/ --fix && uv run ruff format src/
```

### Automatic Linting

- **Pre-commit hooks**: Ruff runs automatically on every `git commit`
- **VS Code**: Auto-format on save using [Ruff extension](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff)

### Linting Rules

Our Ruff configuration includes:

- **Code style**: PEP 8 compliance with 88-character line limit
- **Import organization**: Automatic import sorting and cleanup
- **Code quality**: Detection of unused variables, simplification opportunities
- **Modern Python**: Encourages modern Python patterns with `pyupgrade`

## 📋 Adding New Features

### Before You Start

1. **Check existing issues** to see if someone is already working on it
2. **Open an issue** to discuss larger changes
3. **Review the codebase** to understand existing patterns

### Implementation Guidelines

1. **Follow existing patterns**: Look at similar tools for guidance
2. **Use the utility modules**: Leverage existing connection and context management
3. **Add proper logging**: Use the hierarchical logging system
4. **Handle errors gracefully**: Provide helpful error messages
5. **Consider read-only mode**: If your tool modifies data, respect `read_only_mode` settings
6. **Update documentation**: Update README.md, DOCKER.md, and the docs site (in the [couchbase/mcp-server-couchbase-docs](https://github.com/couchbase/mcp-server-couchbase-docs) repo, under `versioned_docs/version-<X>/`) if adding user-facing features

## 🏗️ Project Structure

```
mcp-server-couchbase/
├── src/
│   ├── mcp_server.py                              # MCP server entry point (uv run src/mcp_server.py)
│   ├── providers/                                 # Standalone-host ClusterProvider implementations
│   │   ├── __init__.py
│   │   └── static.py                              # StaticClusterProvider used by mcp_server.py
│   └── cb_mcp/                                    # Reusable Python package shared with other implementations
│       ├── __init__.py                            # Package marker
│       ├── tool_registration.py                   # Shared tool preparation for MCP server: parse, filter, wrap with confirmation
│       ├── core/                                  # Host-agnostic contracts (ClusterProvider, ...)
│       │   ├── __init__.py
│       │   └── contracts.py
│       ├── certs/                                 # SSL/TLS certificates
│       │   ├── __init__.py                        # Package marker
│       │   └── capella_root_ca.pem                # Capella root CA certificate (for Capella connections)
│       ├── tools/                                 # MCP tool implementations
│       │   ├── __init__.py                        # Tool exports and ALL_TOOLS list
│       │   ├── server.py                          # Server status and connection tools
│       │   ├── kv.py                              # Key-value operations (CRUD)
│       │   ├── query.py                           # SQL++ Query based tools
│       │   └── index.py                           # Index operations and recommendations
│       └── utils/                                 # Utility modules
│           ├── __init__.py                        # Utility exports
│           ├── constants.py                       # Project constants
│           ├── config.py                          # Configuration management
│           ├── connection.py                      # Couchbase connection handling
│           ├── context.py                         # Application context management
│           ├── elicitation.py                     # Confirmation/elicitation support
│           ├── index_utils.py                     # Index-related helper functions
│           └── query_utils.py                     # Query-related helper functions
├── scripts/                                       # Development scripts
│   ├── lint.sh                                    # Manual linting script
│   ├── lint_fix.sh                                # Auto-fix linting issues
│   ├── setup_test_data.py                         # Setup script for integration tests
│   └── update_version.sh                          # Script to bump package version
├── tests/                                         # Test suite
│   ├── conftest.py                                # Shared test fixtures
│   ├── test_confirmation_tools.py                 # Tests for confirmation/elicitation
│   ├── test_index_tools.py                        # Tests for index tools
│   ├── test_is_explain_statement.py               # Tests for EXPLAIN detection
│   ├── test_kv_tools.py                           # Tests for KV operations
│   ├── test_mcp_integration.py                    # MCP integration tests
│   ├── test_parse_tool_names.py                   # Tests for tool name parsing
│   ├── test_performance_tools.py                  # Tests for performance analysis tools
│   ├── test_query_plan_evaluation.py              # Tests for query plan evaluation
│   ├── test_query_tools.py                        # Tests for query tools
│   ├── test_read_only_mode.py                     # Tests for read-only mode
│   ├── test_server_configuration_status_tool.py   # Tests for server configuration status
│   ├── test_server_tools.py                       # Tests for server tools
│   ├── test_tool_registration.py                  # Tests for tool registration
│   └── test_utils.py                              # Tests for utility functions
├── .pre-commit-config.yaml                        # Pre-commit hook configuration
├── build.sh                                       # Docker image build script
├── Dockerfile                                     # Docker container definition
├── DOCKER.md                                      # Docker usage documentation
├── glama.json                                     # Glama MCP catalog metadata
├── LICENSE                                        # Apache 2.0 license
├── pyproject.toml                                 # Project dependencies and Ruff config
├── RELEASE.md                                     # Release process documentation
├── server.json                                    # MCP Registry configuration
├── CONTRIBUTING.md                                # Contribution Guide
└── README.md                                      # Usage
```

## 🛠️ Development Workflow

### Making Changes

1. **Create a branch** for your feature/fix:

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the existing patterns

3. **Test your changes**:

   ```bash
   # Run linting
   ./scripts/lint.sh

   # Test the MCP server
   uv run src/mcp_server.py --help
   ```

4. **Commit your changes**:

   ```bash
   git add .
   git commit -m "feat: add your feature description"
   ```

   The pre-commit hooks will automatically run and fix any formatting issues.

### Adding New Tools

When adding new MCP tools:

1. **Create the tool function** in the appropriate module under `src/cb_mcp/tools/` (`server.py`, `kv.py`, `query.py`, or `index.py`)
2. **Export the tool** in `src/cb_mcp/tools/__init__.py` and add it to `__all__`
3. **Add to the correct tool list** in `src/cb_mcp/tools/__init__.py`: `READ_ONLY_TOOLS` if it only reads data, or `KV_WRITE_TOOLS` if it modifies data (so it's excluded under read-only mode)
4. **Add an entry to `TOOL_ANNOTATIONS`** in the same file with the appropriate hints (`readOnlyHint`, `idempotentHint`, `destructiveHint`)
5. **Write tests** for the new tool in the `tests/` directory
6. **Test the tool** with an MCP client

### Code Style Guidelines

- **Line length**: 88 characters (enforced by Ruff)
- **Import organization**: Use isort-style grouping (standard library, third-party, local)
- **Type hints**: Use modern Python type hints where helpful
- **Docstrings**: Add docstrings for public functions and classes
- **Error handling**: Include appropriate exception handling with logging

## 🧪 Testing

### Manual Testing

Currently, testing is done manually with MCP clients:

1. **Set up environment variables** for your Couchbase cluster
2. **Run the server** with an MCP client like Claude Desktop
3. **Test tool functionality** through the client interface

### Automated Tests

Ensure all existing tests pass so your changes don't break anything. The project has a comprehensive test suite in the `tests/` directory:

```bash
# Run all tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_query_tools.py

# Run tests with verbose output
uv run pytest -v
```

### Setting Up Test Data

For integration tests that need a running Couchbase cluster:

```bash
# Set required environment variables
export CB_CONNECTION_STRING="couchbase://localhost"
export CB_USERNAME="username"
export CB_PASSWORD="password"
export CB_MCP_TEST_BUCKET="travel-sample"

# Run the setup script to create indexes and populate test data
uv run scripts/setup_test_data.py
```

### Test Categories

- **Unit tests**: Test individual functions and utilities (e.g., `test_utils.py`, `test_parse_tool_names.py`)
- **Tool tests**: Test each tool category (e.g., `test_kv_tools.py`, `test_query_tools.py`, `test_index_tools.py`)
- **Feature tests**: Test specific features like read-only mode (`test_read_only_mode.py`), confirmation tools (`test_confirmation_tools.py`), and query plan evaluation (`test_query_plan_evaluation.py`)
- **Integration tests**: Test MCP server integration (`test_mcp_integration.py`)

### Writing Tests

When adding new features or tools, add corresponding tests:

1. **Create a test file** in `tests/` following the `test_*.py` naming convention
2. **Use shared fixtures** from `conftest.py`
3. **Test both success and error paths**
4. **Test read-only mode interactions** if your tool performs write operations

## 🤝 Submitting Changes

1. **Run final checks**:

   ```bash
   # Ensure all linting passes
   ./scripts/lint.sh

   # Test with pre-commit
   uv run pre-commit run --all-files
   ```

2. **Push your branch** and create a pull request (PR)

   If you are working on a forked version of the repo, follow [these instructions](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork) to create the PR.

3. **Describe your changes** in the PR description:
   - What does this change do?
   - Why is this change needed?
   - How have you tested it?

## 💡 Tips for Contributors

### Common Development Tasks

```bash
# Install new dependencies
uv add package-name

# Install new dev dependencies
uv add --dev package-name

# Update all package dependencies to the latest compatible versions
uv sync --upgrade

# Update specific package to the latest compatible version
uv sync --upgrade-package package-name

# Run the server for testing
uv run src/mcp_server.py --connection-string "..." --username "..." --password "..."

# Run with write operations enabled
uv run src/mcp_server.py --connection-string "..." --username "..." --password "..." --read-only-mode false

# Run with confirmation required for specific tools
uv run src/mcp_server.py --connection-string "..." --username "..." --password "..." --confirmation-required-tools "delete_document_by_id,replace_document_by_id"

# Run with specific tools disabled
uv run src/mcp_server.py --connection-string "..." --username "..." --password "..." --disabled-tools "upsert_document_by_id,delete_document_by_id"
```

### Debugging

- **Use logging**: The project uses hierarchical logging with the pattern `logger = logging.getLogger(f"{MCP_SERVER_NAME}.module.name")`
- **Check connection**: Ensure your Couchbase cluster is accessible
- **Validate configuration**: Make sure all required environment variables are set

## 📖 Additional Resources

- **[Model Context Protocol Documentation](https://modelcontextprotocol.io/)**
- **[Couchbase Python SDK Documentation](https://docs.couchbase.com/python-sdk/current/hello-world/start-using-sdk.html)**
- **[SQL++ Query Language](https://www.couchbase.com/sqlplusplus/)**
- **[Ruff Documentation](https://docs.astral.sh/ruff/)**

## 🆘 Getting Help

- **Open an issue** for bugs or feature requests
- **Check existing issues** for similar problems
- **Review the code** for examples and patterns

Thank you for contributing to the Couchbase MCP Server! 🚀
