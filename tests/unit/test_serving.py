"""Unit tests for cb_mcp.core.serving: the pure serving-topology rules."""

from __future__ import annotations

import anyio
import pytest
from anyio.to_thread import current_default_thread_limiter

from cb_mcp.core.serving import (
    ServingConfig,
    ServingConfigError,
    apply_thread_pool_limit,
    resolve_serving,
    uvicorn_log_level,
    worker_log_file,
)


def _resolve(**overrides) -> ServingConfig:
    kwargs = {
        "server_id": "operational",
        "transport": "http",
        "workers": 1,
        "stateless_http": None,
        "thread_pool_size": None,
        "supports_multiple_workers": True,
        "confirmation_required": set(),
    }
    kwargs.update(overrides)
    return resolve_serving(**kwargs)


class TestStatelessDefault:
    def test_single_worker_keeps_sessions(self):
        assert _resolve().stateless_http is False

    def test_multiple_workers_default_to_stateless(self):
        assert _resolve(workers=4) == ServingConfig(4, True, None)

    def test_explicit_true_on_one_worker_is_honoured(self):
        assert _resolve(stateless_http=True).stateless_http is True

    def test_explicit_true_with_workers_is_honoured(self):
        assert _resolve(workers=2, stateless_http=True).stateless_http is True

    def test_thread_pool_size_passes_through(self):
        assert _resolve(thread_pool_size=80).thread_pool_size == 80


class TestRejectedCombinations:
    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_workers_need_streamable_http(self, transport):
        with pytest.raises(ServingConfigError, match="requires --transport=http"):
            _resolve(workers=2, transport=transport)

    def test_workers_with_explicit_stateful_is_an_error(self):
        with pytest.raises(ServingConfigError, match="requires stateless HTTP"):
            _resolve(workers=2, stateless_http=False)

    def test_workers_on_a_server_that_cannot_share_state(self):
        with pytest.raises(ServingConfigError, match="operational-insights"):
            _resolve(
                server_id="operational-insights",
                workers=2,
                supports_multiple_workers=False,
            )

    def test_single_worker_is_fine_for_a_server_that_cannot_share_state(self):
        config = _resolve(
            stateless_http=True,
            supports_multiple_workers=False,
        )
        assert config.stateless_http is True

    def test_stateless_on_sse_is_an_error(self):
        with pytest.raises(ServingConfigError, match="no stateless mode"):
            _resolve(transport="sse", stateless_http=True)

    @pytest.mark.parametrize(
        "overrides",
        [{"stateless_http": True}, {"workers": 3}],
        ids=["explicit-stateless", "implied-by-workers"],
    )
    def test_confirmation_tools_cannot_be_stateless(self, overrides):
        with pytest.raises(ServingConfigError, match="upsert_document_by_id"):
            _resolve(confirmation_required={"upsert_document_by_id"}, **overrides)

    def test_confirmation_tools_with_sessions_are_fine(self):
        config = _resolve(confirmation_required={"upsert_document_by_id"})
        assert config.stateless_http is False


class TestStdio:
    def test_stateless_on_stdio_is_ignored_with_a_warning(self, caplog):
        config = _resolve(transport="stdio", stateless_http=True)
        assert config.stateless_http is False
        assert "only honored" in caplog.text

    def test_ignored_stateless_does_not_reject_confirmation_tools(self):
        config = _resolve(
            transport="stdio",
            stateless_http=True,
            confirmation_required={"upsert_document_by_id"},
        )
        assert config.stateless_http is False


class TestWorkerLogFile:
    def test_inserts_host_and_pid_before_the_extension(self):
        assert (
            worker_log_file("logs/mcp_server.log", host="web-1", pid=4711)
            == "logs/mcp_server.web-1.4711.log"
        )

    def test_uses_the_short_hostname(self):
        assert (
            worker_log_file("mcp.log", host="web-1.prod.example.com", pid=7)
            == "mcp.web-1.7.log"
        )

    def test_sanitises_unsafe_characters(self):
        assert worker_log_file("mcp.log", host="a b/c", pid=7) == "mcp.a_b_c.7.log"

    def test_empty_host_still_yields_a_name(self):
        assert worker_log_file("mcp.log", host="", pid=7) == "mcp.host.7.log"

    def test_path_without_extension(self):
        assert worker_log_file("mcp", host="h", pid=7) == "mcp.h.7"


class TestThreadPoolLimit:
    def test_none_keeps_the_runtime_default(self):
        async def check():
            return apply_thread_pool_limit(None)

        assert anyio.run(check) == 40

    def test_sets_the_limit_for_this_run(self):
        async def check():
            applied = apply_thread_pool_limit(80)
            return applied, current_default_thread_limiter().total_tokens

        assert anyio.run(check) == (80, 80)


@pytest.mark.parametrize(
    ("ours", "uvicorn"),
    [("OFF", "critical"), ("debug", "debug"), ("INFO", "info"), ("bogus", "info")],
)
def test_uvicorn_log_level(ours, uvicorn):
    assert uvicorn_log_level(ours) == uvicorn
