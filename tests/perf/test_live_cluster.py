"""Same in-process dispatch, real Couchbase server behind it.

Compared against test_dispatch_overhead.py (stub cluster), the difference
is the genuine end-to-end cost per call. Report-only: cluster latency varies
too much across environments to assert on.

Requires CB_CONNECTION_STRING / CB_USERNAME / CB_PASSWORD and
CB_MCP_TEST_BUCKET (same variables as the integration suite). Writes to
``perf_unit::doc::<n>`` keys in that bucket. The SQL++ case uses USE KEYS so
no index is needed.
"""

from __future__ import annotations

import os

import pytest
from _test_env import (
    REQUIRED_ENV_VARS,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)

from providers.static import StaticClusterProvider

from ._harness import build_perf_server, format_table, run_load

LEVELS = (1, 10)
N_KEYS = 50


def _key(i: int) -> str:
    return f"perf_unit::doc::{i % N_KEYS}"


@pytest.fixture(scope="module")
def keyspace():
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        pytest.skip(f"live perf tests need {', '.join(missing)}")
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


@pytest.fixture(scope="module")
def mcp():
    settings = {
        "connection_string": os.getenv("CB_CONNECTION_STRING"),
        "username": os.getenv("CB_USERNAME"),
        "password": os.getenv("CB_PASSWORD"),
        "ca_cert_path": os.getenv("CB_CA_CERT_PATH"),
        "client_cert_path": os.getenv("CB_CLIENT_CERT_PATH"),
        "client_key_path": os.getenv("CB_CLIENT_KEY_PATH"),
    }
    return build_perf_server(StaticClusterProvider(settings=settings))


@pytest.mark.asyncio
async def test_kv_and_query_against_cluster(mcp, keyspace):
    # The args callable gets the *worker* index, so seed with one worker per
    # key — every key a later worker will ask for then exists.
    await run_load(
        mcp,
        "upsert_document_by_id",
        lambda i: {
            **keyspace,
            "document_id": _key(i),
            "document_content": {"kind": "perf-unit", "n": i},
        },
        concurrency=N_KEYS,
        iterations=1,
        label="seed",
    )

    results = []
    for c in LEVELS:
        results.append(
            await run_load(
                mcp,
                "get_document_by_id",
                lambda i: {**keyspace, "document_id": _key(i)},
                concurrency=c,
                label=f"get_document_by_id c{c}",
            )
        )
        results.append(
            await run_load(
                mcp,
                "upsert_document_by_id",
                lambda i: {
                    **keyspace,
                    "document_id": _key(i),
                    "document_content": {"kind": "perf-unit", "n": i},
                },
                concurrency=c,
                label=f"upsert_document_by_id c{c}",
            )
        )
        results.append(
            await run_load(
                mcp,
                "run_sql_plus_plus_query",
                lambda i: {
                    "bucket_name": keyspace["bucket_name"],
                    "scope_name": keyspace["scope_name"],
                    "query": f"SELECT * FROM `{keyspace['collection_name']}` USE KEYS $k",
                    "named_parameters": {"k": _key(i)},
                },
                concurrency=c,
                label=f"run_sql_plus_plus_query c{c}",
            )
        )

    print("\n" + format_table(results))
    for r in results:
        assert r.errors == 0, f"{r.label}: {r.errors} errors"
