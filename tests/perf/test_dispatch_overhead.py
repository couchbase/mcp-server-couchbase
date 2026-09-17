"""Per-call framework overhead at c1, with Couchbase SDK stubbed out.

What's left when the cluster costs nothing is the FastMCP/mcp dispatch
path itself: middleware, argument validation, thread-pool hop, result
serialisation. This is the baseline every other perf test is relative to.
"""

from __future__ import annotations

import pytest

from ._harness import (
    StubClusterProvider,
    build_perf_server,
    check_ceiling,
    format_table,
    run_load,
)

KEYSPACE = {"bucket_name": "b", "scope_name": "_default", "collection_name": "_default"}

# Generous in-process caps; only enforced with CB_MCP_PERF_ASSERT=1. Meant to
# catch a dispatch path that became catastrophically slower, not machine noise.
MAX_P50_MS = 50.0


@pytest.fixture(scope="module")
def mcp():
    return build_perf_server(StubClusterProvider())


@pytest.mark.asyncio
async def test_c1_overhead_per_tool(mcp):
    results = [
        await run_load(
            mcp,
            "get_server_configuration_status",
            {},
            concurrency=1,
            label="config_status (no cluster)",
        ),
        await run_load(
            mcp,
            "get_document_by_id",
            {**KEYSPACE, "document_id": "k"},
            concurrency=1,
            label="get_document_by_id (stub)",
        ),
        await run_load(
            mcp,
            "upsert_document_by_id",
            {**KEYSPACE, "document_id": "k", "document_content": {"a": 1}},
            concurrency=1,
            label="upsert_document_by_id (stub)",
        ),
        await run_load(
            mcp,
            "run_sql_plus_plus_query",
            {
                "bucket_name": "b",
                "scope_name": "_default",
                "query": "SELECT * FROM `_default` USE KEYS $k",
                "named_parameters": {"k": "k"},
            },
            concurrency=1,
            label="run_sql_plus_plus_query (stub)",
        ),
    ]
    print("\n" + format_table(results))
    for r in results:
        assert r.errors == 0, f"{r.label}: {r.errors} errors"
        check_ceiling(f"{r.label} p50", r.p50, MAX_P50_MS)
