# Stage 1: Build the distribution wheel from source
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS wheel-builder

WORKDIR /src

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN uv build --wheel --out-dir /wheels

# Stage 2: Create venv, install pinned deps from lock file, then install pre-built wheel
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

COPY pyproject.toml README.md uv.lock ./

COPY --from=wheel-builder /wheels/ /wheels/

RUN uv venv /opt/venv && \
    UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-install-project && \
    uv pip install --python /opt/venv/bin/python --no-deps /wheels/couchbase_mcp_server-*.whl

# Runtime stage
FROM python:3.13-slim-trixie AS runtime

# Accept build arguments for labels
ARG GIT_COMMIT_HASH="unknown"
ARG BUILD_DATE="unknown"

# Add metadata labels
LABEL org.opencontainers.image.revision="${GIT_COMMIT_HASH}" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.title="MCP Server Couchbase" \
    org.opencontainers.image.description="Model Context Protocol server for Couchbase" \
    org.opencontainers.image.source="https://github.com/couchbase/mcp-server-couchbase"\
    io.modelcontextprotocol.server.name="io.github.couchbase/mcp-server-couchbase"

# Create non-root user
RUN useradd --system --uid 1001 mcpuser

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Set up Python environment
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Change ownership to non-root user
RUN chown -R mcpuser:mcpuser /app /opt/venv

# Switch to non-root user
USER 1001

# Environment variables with stdio defaults (override for network mode)
ENV CB_MCP_READ_ONLY_MODE="true" \
    CB_MCP_TRANSPORT="stdio" \
    CB_MCP_PORT="8000"

# Expose default port for HTTP/SSE mode
EXPOSE 8000

# Use the installed console script
ENTRYPOINT ["couchbase-mcp-server"]