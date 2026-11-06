"""Unit tests for cb_mcp.core.serving: the pure serving-topology rules."""

from __future__ import annotations

import anyio
import pytest
from _tls_fixtures import KEY_PHRASE, write_self_signed
from anyio.to_thread import current_default_thread_limiter

from cb_mcp.core.serving import (
    ServingConfig,
    ServingConfigError,
    TlsConfig,
    apply_thread_pool_limit,
    resolve_serving,
    resolve_tls,
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


class TestInheritedRuntimeDefault:
    """FASTMCP_STATELESS_HTTP, as the host reads it from FastMCP's settings.

    It is the default when --stateless-http is unset, so deployments that
    relied on it keep working, and the checks see the mode that will run.
    """

    def test_is_honoured_when_the_flag_is_unset(self):
        assert _resolve(runtime_default_stateless=True).stateless_http is True

    def test_explicit_flag_overrides_it(self):
        config = _resolve(stateless_http=False, runtime_default_stateless=True)
        assert config.stateless_http is False

    def test_workers_still_imply_stateless_when_it_is_false(self):
        assert _resolve(workers=2).stateless_http is True

    def test_confirmation_tools_are_rejected_and_the_source_is_named(self):
        with pytest.raises(ServingConfigError, match="FASTMCP_STATELESS_HTTP"):
            _resolve(
                runtime_default_stateless=True,
                confirmation_required={"upsert_document_by_id"},
            )

    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_is_ignored_with_a_warning_where_it_cannot_apply(self, transport, caplog):
        config = _resolve(transport=transport, runtime_default_stateless=True)
        assert config.stateless_http is False
        assert "FASTMCP_STATELESS_HTTP" in caplog.text

    def test_explicit_flag_on_sse_is_still_an_error(self):
        with pytest.raises(ServingConfigError, match="no stateless mode"):
            _resolve(transport="sse", stateless_http=True)


def _tls(**overrides) -> TlsConfig | None:
    kwargs = {
        "transport": "http",
        "cert_path": None,
        "key_path": None,
        "key_password": None,
    }
    kwargs.update(overrides)
    return resolve_tls(**kwargs)


class TestResolveTls:
    def test_nothing_configured_serves_plain_http(self):
        assert _tls() is None

    @pytest.mark.parametrize("transport", ["http", "sse"])
    def test_cert_and_key_enable_tls_on_network_transports(self, tmp_path, transport):
        files = write_self_signed(tmp_path)
        tls = _tls(transport=transport, cert_path=files.cert, key_path=files.key)
        assert tls == TlsConfig(files.cert, files.key, None)
        assert tls.uvicorn_kwargs() == {
            "ssl_certfile": files.cert,
            "ssl_keyfile": files.key,
        }

    def test_encrypted_key_with_its_password(self, tmp_path):
        files = write_self_signed(tmp_path, password=KEY_PHRASE)
        tls = _tls(cert_path=files.cert, key_path=files.key, key_password=KEY_PHRASE)
        assert tls.uvicorn_kwargs()["ssl_keyfile_password"] == KEY_PHRASE

    def test_empty_password_is_treated_as_unset(self, tmp_path):
        files = write_self_signed(tmp_path)
        tls = _tls(cert_path=files.cert, key_path=files.key, key_password="")
        assert tls.key_password is None
        assert "ssl_keyfile_password" not in tls.uvicorn_kwargs()

    def test_repr_hides_the_password(self, tmp_path):
        files = write_self_signed(tmp_path, password=KEY_PHRASE)
        tls = _tls(cert_path=files.cert, key_path=files.key, key_password=KEY_PHRASE)
        assert KEY_PHRASE not in repr(tls)

    @pytest.mark.parametrize(
        ("given", "missing"),
        [("cert_path", "--tls-key-path"), ("key_path", "--tls-cert-path")],
    )
    def test_cert_and_key_are_required_together(self, tmp_path, given, missing):
        files = write_self_signed(tmp_path)
        path = files.cert if given == "cert_path" else files.key
        with pytest.raises(ServingConfigError, match=missing):
            _tls(**{given: path})

    def test_password_without_cert_is_an_error(self):
        with pytest.raises(ServingConfigError, match="--tls-key-password requires"):
            _tls(key_password=KEY_PHRASE)

    def test_ignored_with_a_warning_on_stdio(self, tmp_path, caplog):
        files = write_self_signed(tmp_path)
        assert _tls(transport="stdio", cert_path=files.cert, key_path=files.key) is None
        assert "ignoring them for transport=stdio" in caplog.text

    @pytest.mark.parametrize(
        "settings",
        [
            {"cert_path": "/absent/server.crt"},
            {"key_path": "/absent/server.key"},
            {"key_password": KEY_PHRASE},
        ],
        ids=["cert-only", "key-only", "password-only"],
    )
    def test_partial_settings_are_ignored_on_stdio(self, settings, caplog):
        """A stdio client inheriting part of the TLS env must still start."""
        assert _tls(transport="stdio", **settings) is None
        assert "ignoring them for transport=stdio" in caplog.text

    def test_nothing_configured_on_stdio_is_silent(self, caplog):
        assert _tls(transport="stdio") is None
        assert "TLS" not in caplog.text

    @pytest.mark.parametrize(
        ("missing", "flag"),
        [("cert", "--tls-cert-path"), ("key", "--tls-key-path")],
    )
    def test_missing_file_is_an_error(self, tmp_path, missing, flag):
        files = write_self_signed(tmp_path)
        paths = {"cert_path": files.cert, "key_path": files.key}
        paths[f"{missing}_path"] = str(tmp_path / "absent.pem")
        with pytest.raises(ServingConfigError, match=f"{flag} file not found"):
            _tls(**paths)

    def test_key_that_does_not_match_the_certificate(self, tmp_path):
        first = write_self_signed(tmp_path, name="first")
        second = write_self_signed(tmp_path, name="second")
        with pytest.raises(ServingConfigError, match="Could not load"):
            _tls(cert_path=first.cert, key_path=second.key)

    def test_wrong_password(self, tmp_path):
        files = write_self_signed(tmp_path, password=KEY_PHRASE)
        with pytest.raises(ServingConfigError, match="Could not load") as excinfo:
            _tls(cert_path=files.cert, key_path=files.key, key_password="wrong")
        assert "wrong" not in str(excinfo.value)

    def test_encrypted_key_without_password_fails_instead_of_prompting(self, tmp_path):
        files = write_self_signed(tmp_path, password=KEY_PHRASE)
        with pytest.raises(ServingConfigError, match="Could not load"):
            _tls(cert_path=files.cert, key_path=files.key)

    def test_not_pem_is_an_error(self, tmp_path):
        files = write_self_signed(tmp_path)
        bogus = tmp_path / "bogus.crt"
        bogus.write_text("not a certificate")
        with pytest.raises(ServingConfigError, match="Could not load"):
            _tls(cert_path=str(bogus), key_path=files.key)
