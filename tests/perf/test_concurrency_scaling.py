"""How per-call latency grows with in-process concurrency, Couchbase SDK stubbed.

With the cluster free, every millisecond of growth from c1 to c100 is
dispatch/scheduling contention in the single event loop. Total calls
are held roughly constant across levels so runtime stays bounded.
"""

from __future__ import annotations

import pytest
from _harness import (
    ITERATIONS,
    StubClusterProvider,
    build_perf_server,
    check_ratio,
    format_table,
    run_load,
)

LEVELS = (1, 10, 50, 100)
ARGS = {
    "bucket_name": "b",
    "scope_name": "_default",
    "collection_name": "_default",
    "document_id": "k",
}

# c100 p50 may legitimately be far above c1 p50 (queueing); this cap only
# flags a catastrophic regression such as a lock serialising every call.
MAX_P50_GROWTH = 200.0


@pytest.fixture(scope="module")
def mcp():
    return build_perf_server(StubClusterProvider())


@pytest.mark.asyncio
async def test_latency_vs_concurrency(mcp):
    results = []
    for c in LEVELS:
        per_worker = max(20, ITERATIONS // c)
        results.append(
            await run_load(
                mcp,
                "get_document_by_id",
                ARGS,
                concurrency=c,
                iterations=per_worker,
                label=f"get_document_by_id (stub) c{c}",
            )
        )
    print("\n" + format_table(results))
    for r in results:
        assert r.errors == 0, f"{r.label}: {r.errors} errors"

    base, top = results[0], results[-1]
    check_ratio("p50 growth c1→c100", top.p50, base.p50, MAX_P50_GROWTH)
