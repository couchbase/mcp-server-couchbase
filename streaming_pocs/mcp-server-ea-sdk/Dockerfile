FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# HTTP transport for load-balanced / multi-replica deployment.
ENV EA_MCP_TRANSPORT=http \
    EA_MCP_HOST=0.0.0.0 \
    EA_MCP_PORT=8000

EXPOSE 8000
ENTRYPOINT ["enterprise-analytics-streaming-mcp-server"]
