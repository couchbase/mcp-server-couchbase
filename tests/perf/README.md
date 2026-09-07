# In-process performance tests

Opt-in, not run in CI. They drive the real FastMCP server through
`fastmcp.Client`'s in-memory transport, so a call goes through the actual
dispatch chain (middleware → `tool._run` → `without_injected_parameters` →
thread pool) with no HTTP, subprocess, or load generator involved.

```sh
CB_MCP_PERF=1 pytest tests/perf -s            # report only (always passes)
CB_MCP_PERF=1 CB_MCP_PERF_ASSERT=1 pytest tests/perf -s   # also enforce thresholds
```

| File | Cluster | Measures |
|---|---|---|
| `test_dispatch_overhead.py` | stubbed | per-call framework cost at c1, per tool |
| `test_concurrency_scaling.py` | stubbed | p50 growth from c1 → c100 (event-loop contention) |
| `test_live_cluster.py` | real | same calls with Couchbase behind them; the delta vs. the stub tier is the real DB cost |

The stub tiers run anywhere in seconds. The live tier needs
`CB_CONNECTION_STRING` / `CB_USERNAME` / `CB_PASSWORD` / `CB_MCP_TEST_BUCKET`
(same as `tests/integration`) and writes `perf_unit::doc::*` keys.

## Environment

| Variable | Effect |
|---|---|
| `CB_MCP_PERF=1` | required; otherwise everything is skipped |
| `CB_MCP_PERF_ASSERT=1` | enforce thresholds (ratios, plus generous in-process caps) |
| `CB_MCP_PERF_ITERATIONS` | calls per worker, default 100 |

`DO_NOT_TRACK=1` is set by `conftest.py`: the telemetry wrapper stays
installed (realistic overhead) but sends nothing.

## Reading the numbers

- Thresholds are ratios where possible (`c100 p50 / c1 p50`), not absolute
  milliseconds as they travel across machines. Absolute caps are deliberately
  loose; they exist to catch catastrophic regressions, not noise.
- The `build_perf_server` helper mirrors `mcp_server.main`'s registration by
  hand. If registration changes there, update `_harness.py`.
