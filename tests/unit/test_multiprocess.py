"""Tests for the multi-worker helpers.

Two things here are worth pinning down. First, the option-combination rules:
``--workers`` above 1 only works on a transport whose requests can be spread
over processes, and only in stateless mode, because a session created in one
worker's memory is invisible to its siblings. Getting that wrong produces a
server that accepts a connection and then fails the client's next request, so
the rules are enforced up front and asserted here.

Second, the config handoff. Workers are spawned as fresh interpreters that
never parse the CLI, so the parent's resolved options travel as JSON. Two of
them are ``NamedTuple`` callback results, which ``json`` will silently flatten
into anonymous arrays — the round-trip test exists to catch that.
"""

from __future__ import annotations

import json

import pytest

from cb_mcp.utils.logging import ParsedLogLevel, ParsedLogSinks
from cb_mcp.utils.multiprocess import (
    WORKER_CONFIG_ENV_VAR,
    WorkerConfigError,
    decode_worker_config,
    encode_worker_config,
    export_worker_config,
    load_worker_config,
    resolve_worker_settings,
    uvicorn_log_level,
    worker_log_file,
)


def _params(**overrides):
    """A minimal params mapping shaped like ``click.Context.params``."""
    base = {
        "connection_string": "couchbase://localhost",
        "username": "tester",
        "password": "hunter2",
        "transport": "http",
        "host": "127.0.0.1",
        "port": 8000,
        "workers": 2,
        "stateless_http": True,
        "log_file": "mcp_server.log",
        "log_level": ParsedLogLevel("INFO", None),
        "log_sinks": ParsedLogSinks({"stderr", "file"}, ["bogus"]),
    }
    base.update(overrides)
    return base


class TestResolveWorkerSettings:
    def test_single_worker_defaults_to_stateful(self):
        """The default path must not change behaviour for existing deployments."""
        assert resolve_worker_settings(
            workers=1, stateless_http=None, transport="http"
        ) == (1, False)

    def test_multi_worker_defaults_to_stateless(self):
        assert resolve_worker_settings(
            workers=4, stateless_http=None, transport="http"
        ) == (4, True)

    def test_explicit_stateless_honored_for_single_worker(self):
        """Stateless mode is independently useful: it also drops per-session
        bookkeeping in a single process."""
        assert resolve_worker_settings(
            workers=1, stateless_http=True, transport="http"
        ) == (1, True)

    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_multi_worker_rejected_on_non_http_transports(self, transport):
        with pytest.raises(WorkerConfigError, match="requires --transport=http"):
            resolve_worker_settings(workers=2, stateless_http=None, transport=transport)

    def test_multi_worker_with_stateless_disabled_rejected(self):
        """Sessions are per-process, so this combination would break clients on
        their second request rather than at startup — reject it at startup."""
        with pytest.raises(WorkerConfigError, match="requires stateless HTTP"):
            resolve_worker_settings(workers=2, stateless_http=False, transport="http")

    def test_stateless_rejected_on_sse(self):
        with pytest.raises(WorkerConfigError, match="not supported on --transport=sse"):
            resolve_worker_settings(workers=1, stateless_http=True, transport="sse")

    def test_stdio_single_worker_unaffected(self):
        assert resolve_worker_settings(
            workers=1, stateless_http=None, transport="stdio"
        ) == (1, False)


class TestWorkerConfigRoundTrip:
    def test_round_trip_preserves_all_params(self):
        params = _params()
        assert decode_worker_config(encode_worker_config(params)) == params

    def test_parsed_log_params_survive_as_named_fields(self):
        """``ParsedLogLevel``/``ParsedLogSinks`` are NamedTuples, which json
        serialises as bare arrays. Encoding them as objects is what keeps the
        rejected-input tokens attached to the right field after the trip."""
        params = _params(
            log_level=ParsedLogLevel("INFO", "bogus-level"),
            log_sinks=ParsedLogSinks({"file"}, ["nope"]),
        )
        encoded = json.loads(encode_worker_config(params))
        assert encoded["log_level"] == {
            "level": "INFO",
            "invalid_token": "bogus-level",
        }
        assert encoded["log_sinks"] == {"sinks": ["file"], "invalid_tokens": ["nope"]}

        decoded = decode_worker_config(json.dumps(encoded))
        assert decoded["log_level"] == ParsedLogLevel("INFO", "bogus-level")
        assert decoded["log_sinks"] == ParsedLogSinks({"file"}, ["nope"])

    def test_export_then_load_via_environment(self, monkeypatch):
        monkeypatch.delenv(WORKER_CONFIG_ENV_VAR, raising=False)
        params = _params()
        export_worker_config(params)
        assert load_worker_config() == params

    def test_load_without_exported_config_raises(self, monkeypatch):
        """A worker with no configuration must fail loudly instead of starting
        up on defaults and connecting nowhere."""
        monkeypatch.delenv(WORKER_CONFIG_ENV_VAR, raising=False)
        with pytest.raises(RuntimeError, match=WORKER_CONFIG_ENV_VAR):
            load_worker_config()

    def test_non_serialisable_param_fails_in_the_parent(self):
        """Better a TypeError in the supervisor than a worker that spawns with
        half a configuration."""
        with pytest.raises(TypeError):
            encode_worker_config(_params(port=object()))


class TestWorkerLogFile:
    def test_pid_inserted_before_extension(self):
        assert worker_log_file("mcp_server.log", 4711) == "mcp_server.4711.log"

    def test_directories_and_dots_preserved(self):
        assert (
            worker_log_file("/var/log/cb/mcp.server.log", 12)
            == "/var/log/cb/mcp.server.12.log"
        )

    def test_extensionless_path_still_suffixed(self):
        assert worker_log_file("mcp_server", 9) == "mcp_server.9"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_unset_path_passed_through(self, empty):
        """No file sink configured means there is nothing to disambiguate."""
        assert worker_log_file(empty, 1) == empty


class TestUvicornLogLevel:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            ("OFF", "critical"),
            ("TRACE", "trace"),
            ("DEBUG", "debug"),
            ("INFO", "info"),
            ("WARNING", "warning"),
            ("ERROR", "error"),
            ("info", "info"),
        ],
    )
    def test_known_levels_mapped(self, level, expected):
        assert uvicorn_log_level(level) == expected

    def test_unknown_level_falls_back_to_info(self):
        assert uvicorn_log_level("verbose") == "info"
