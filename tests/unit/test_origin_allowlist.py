"""Origin allow-list (``--allowed-origins`` / ``CB_MCP_ALLOWED_ORIGINS``).

Black-box on purpose: they drive the real CLI and send requests to the real ASGI
app the server would serve, once per serving path (single process, and the
``--workers`` factory). The check is FastMCP's own Host/Origin guard, so these
pin what an operator actually gets from it:

* No list (unset, empty, blank, only commas, a lone ``*``): no check, everything
  is admitted, exactly as before.
* List set: a request whose ``Origin`` is not listed gets ``403`` (``null``
  included). A request with **no** ``Origin`` is admitted: FastMCP's guard only
  judges an ``Origin`` that is present, so non-browser clients keep working.
* On a server bound to loopback, ``Host`` is validated too (``421`` for a foreign
  host, which is what stops DNS rebinding), and an ``Origin`` that is itself a
  loopback address is admitted whatever the list says. Bound to anything else
  (``0.0.0.0``, as in a container) ``Host`` is not checked.
* The guard covers every route, ``/health`` included, when an ``Origin`` is sent.
* A malformed list fails startup instead of silently becoming "allow all", and the
  error says which entry is wrong and why, so the operator can fix it without
  reading the source.

Most tests run "exposed" (``--host 0.0.0.0``, requests to a public name), where
only the list decides; the loopback behaviour has its own class.
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import couchbase
import httpx
import pytest
from click.testing import CliRunner

import mcp_server
from cb_mcp.utils.cli_params import decode_worker_config
from cb_mcp.utils.constants import HEALTH_CHECK_PATH

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
ALLOWED = "https://app.example.com,http://localhost:3000"
#: A server reachable by name (``--host 0.0.0.0``, as in a container) and one
#: bound to loopback. They differ in which FastMCP checks apply.
EXPOSED = "http://mcp.mycompany.com:8000"
LOOPBACK = "http://127.0.0.1:8000"
EXPOSE_ALL = "0.0.0.0"  # noqa: S104 - a flag value, nothing binds


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """One-shot SDK logging, worker env handoff and startup pings stay out of the way."""
    monkeypatch.delenv(mcp_server.WORKER_CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv("CB_MCP_ALLOWED_ORIGINS", raising=False)
    with (
        patch.object(couchbase, "configure_logging"),
        patch("cb_mcp.core.app.send_install_ping"),
        patch("mcp_server.send_install_ping"),
    ):
        yield
    os.environ.pop(mcp_server.WORKER_CONFIG_ENV_VAR, None)


# --- Harness: get the ASGI app the server would really serve ------------------


def _single_process_app(args: list[str], env: dict[str, str] | None = None):
    """Run the CLI for one process, capturing the app handed to Uvicorn."""
    captured: dict = {}

    class FakeServer:
        def __init__(self, config):
            captured["app"] = config.app

        async def serve(self, *_args, **_kwargs):
            return None

    # FastMCP runs its own event loop, which cannot start inside the running
    # loop of an async test, so the CLI runs on a short-lived thread.
    with (
        patch("fastmcp.server.mixins.transport.uvicorn.Server", FakeServer),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        result = pool.submit(
            CliRunner().invoke, mcp_server.main, args, env=env
        ).result()
    assert result.exit_code == 0, result.output
    return captured["app"]


def _worker_app(args: list[str], env: dict[str, str] | None = None):
    """Run the CLI as a ``--workers`` supervisor, then build one worker's app."""
    with patch("mcp_server.uvicorn.run"):
        result = CliRunner().invoke(mcp_server.main, [*args, "--workers", "2"], env=env)
    assert result.exit_code == 0, result.output
    return mcp_server.create_app()


@pytest.fixture(params=["single-process", "worker"])
def make_app(request):
    """Build the served app for either serving path from CLI args / env."""
    builder = _single_process_app if request.param == "single-process" else _worker_app

    def build(allowed: str | None, *extra: str, env: dict[str, str] | None = None):
        args = ["--transport", "http", *extra]
        if "--host" not in extra:
            args += ["--host", EXPOSE_ALL]
        if allowed is not None:
            args += ["--allowed-origins", allowed]
        return builder(args, env)

    return build


async def _send(
    app,
    method: str = "POST",
    path: str = "/mcp",
    *,
    origin: str | None = None,
    host: str | None = None,
    base: str = EXPOSED,
) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    # The lifespan never runs here, so an admitted request may fail inside the
    # MCP layer (500). Only "was it turned away at the door" is asserted.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base) as client:
        if method == "POST":
            return await client.post(path, json=INITIALIZE, headers=headers)
        return await client.request(method, path, headers=headers)


def _admitted(response: httpx.Response) -> bool:
    return response.status_code not in (403, 421)


# --- No list configured: behaviour is unchanged --------------------------------


@pytest.mark.asyncio
class TestNoAllowlist:
    @pytest.mark.parametrize(
        "allowed",
        [None, "", "   ", ",", " , , ", "*", " * "],
        ids=[
            "unset",
            "empty",
            "blank",
            "comma",
            "commas-and-blanks",
            "bare-star",
            "bare-star-padded",
        ],
    )
    async def test_everything_is_admitted(self, make_app, allowed):
        app = make_app(allowed)
        assert _admitted(await _send(app))  # no Origin at all
        assert _admitted(await _send(app, origin="https://evil.com"))
        assert _admitted(await _send(app, origin="null"))

    async def test_empty_env_var_means_no_check(self, make_app):
        app = make_app(None, env={"CB_MCP_ALLOWED_ORIGINS": ""})
        assert _admitted(await _send(app, origin="https://evil.com"))

    async def test_no_host_check_either_on_loopback(self, make_app):
        """Without a list FastMCP's guard is off entirely, as it always was."""
        app = make_app(None, "--host", "127.0.0.1")
        response = await _send(app, host="evil.com:8000", base=LOOPBACK)
        assert _admitted(response)


# --- List configured: who gets in ---------------------------------------------


@pytest.mark.asyncio
class TestAdmission:
    @pytest.mark.parametrize(
        "origin", ["https://app.example.com", "http://localhost:3000"]
    )
    async def test_every_listed_origin_is_admitted(self, make_app, origin):
        assert _admitted(await _send(make_app(ALLOWED), origin=origin))

    async def test_list_from_env_var(self, make_app):
        app = make_app(None, env={"CB_MCP_ALLOWED_ORIGINS": ALLOWED})
        assert _admitted(await _send(app, origin="https://app.example.com"))
        assert (await _send(app, origin="https://evil.com")).status_code == 403

    async def test_whitespace_around_entries_is_ignored(self, make_app):
        app = make_app("  https://a.example.com  ,\thttps://b.example.com ")
        assert _admitted(await _send(app, origin="https://a.example.com"))
        assert _admitted(await _send(app, origin="https://b.example.com"))

    async def test_empty_entries_between_commas_are_ignored(self, make_app):
        app = make_app("https://a.example.com,,https://b.example.com,")
        assert _admitted(await _send(app, origin="https://b.example.com"))
        assert (await _send(app, origin="https://c.example.com")).status_code == 403

    async def test_scheme_and_host_match_case_insensitively(self, make_app):
        app = make_app("https://app.example.com")
        assert _admitted(await _send(app, origin="HTTPS://App.Example.COM"))

    async def test_default_port_in_the_entry_matches_a_portless_origin(self, make_app):
        # Browsers omit :443 / :80 from Origin.
        app = make_app("https://app.example.com:443,http://other.example.com:80")
        assert _admitted(await _send(app, origin="https://app.example.com"))
        assert _admitted(await _send(app, origin="http://other.example.com"))

    async def test_explicit_non_default_port_must_match_exactly(self, make_app):
        app = make_app("https://app.example.com:8443")
        assert _admitted(await _send(app, origin="https://app.example.com:8443"))
        assert (await _send(app, origin="https://app.example.com")).status_code == 403

    async def test_other_schemes_are_matched_exactly(self, make_app):
        """Browser extensions and webviews send their own scheme as the origin."""
        app = make_app("chrome-extension://abcdef,vscode-webview://1a2b")
        assert _admitted(await _send(app, origin="chrome-extension://abcdef"))
        assert _admitted(await _send(app, origin="vscode-webview://1a2b"))
        assert (await _send(app, origin="chrome-extension://abcdeg")).status_code == 403
        assert (await _send(app, origin="https://abcdef")).status_code == 403

    async def test_duplicate_entries_are_harmless(self, make_app):
        app = make_app("https://a.example.com,https://A.example.com:443")
        assert _admitted(await _send(app, origin="https://a.example.com"))


@pytest.mark.asyncio
class TestRejection:
    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.com",
            "http://app.example.com",  # wrong scheme
            "https://app.example.com:8443",  # wrong port
            "https://sub.app.example.com",  # a subdomain is not the listed host
            "https://example.com",  # a parent domain is not the listed host
            "https://app.example.com.evil.com",  # listed host as a prefix
            "https://evilapp.example.com",  # listed host as a suffix
            "http://localhost:3001",  # wrong port on a listed host
            "http://127.0.0.1:3000",  # localhost and 127.0.0.1 differ
            "https://app.example.com/path",  # not a valid Origin value
            "null",  # sandboxed iframes, file:// pages
            "*",
        ],
    )
    async def test_unlisted_origin_is_rejected(self, make_app, origin):
        response = await _send(make_app(ALLOWED), origin=origin)
        assert response.status_code == 403

    @pytest.mark.parametrize("method", ["POST", "GET"])
    async def test_missing_origin_is_admitted_when_the_list_is_set(
        self, make_app, method
    ):
        """FastMCP's guard only judges an Origin that is present. Accepted.

        Non-browser clients (desktop apps, CLI agents) send none and keep
        working; the list is a guard against web pages, not authentication.
        """
        response = await _send(make_app(ALLOWED), method)
        assert _admitted(response)

    async def test_the_literal_null_origin_is_not_a_wildcard(self, make_app):
        assert (await _send(make_app(ALLOWED), origin="null")).status_code == 403

    async def test_rejected_before_reaching_the_mcp_layer(self, make_app):
        """The 403 comes from the door, not from a half-run MCP request."""
        response = await _send(make_app(ALLOWED), origin="https://evil.com")
        assert response.status_code == 403
        assert "jsonrpc" not in response.text


# --- Host: validated on loopback only ------------------------------------------


@pytest.mark.asyncio
class TestHostHandling:
    """FastMCP validates ``Host`` only when the server is bound to loopback."""

    async def test_exposed_server_accepts_any_host_so_docker_and_proxies_work(
        self, make_app
    ):
        for host in ("mcp.mycompany.com", "couchbase-mcp:8000", "10.0.3.7:8000"):
            response = await _send(
                make_app(ALLOWED), origin="https://app.example.com", host=host
            )
            assert _admitted(response), host

    async def test_exposed_server_does_not_judge_a_request_without_origin(
        self, make_app
    ):
        """The accepted gap: no Origin and no Host check, so nothing to refuse."""
        response = await _send(make_app(ALLOWED), host="evil.com:8000")
        assert _admitted(response)

    async def test_loopback_server_rejects_a_foreign_host(self, make_app):
        """DNS rebinding: the page reaches the server under the attacker's name."""
        app = make_app(ALLOWED, "--host", "127.0.0.1")
        for origin in (None, "http://evil.com:8000"):
            response = await _send(
                app, origin=origin, host="evil.com:8000", base=LOOPBACK
            )
            assert response.status_code == 421, origin

    async def test_loopback_server_still_rejects_unlisted_internet_origins(
        self, make_app
    ):
        app = make_app(ALLOWED, "--host", "127.0.0.1")
        response = await _send(app, origin="https://evil.com", base=LOOPBACK)
        assert response.status_code == 403

    async def test_loopback_server_admits_listed_origins(self, make_app):
        app = make_app(ALLOWED, "--host", "127.0.0.1")
        response = await _send(app, origin="https://app.example.com", base=LOOPBACK)
        assert _admitted(response)

    async def test_loopback_server_admits_any_loopback_origin(self, make_app):
        """FastMCP's same-machine fallback: a local page needs no list entry.

        The cost of that fallback, accepted: a hostile page served from another
        port on the same machine is treated as local.
        """
        app = make_app(ALLOWED, "--host", "127.0.0.1")
        response = await _send(app, origin="http://localhost:9999", base=LOOPBACK)
        assert _admitted(response)


# --- Wildcards: a '*' in the host stays inside the host ------------------------------


@pytest.mark.asyncio
class TestWildcards:
    PATTERN = "https://*.example.com"

    @pytest.mark.parametrize(
        "origin",
        [
            "https://a.example.com",
            "https://a.b.example.com",  # * spans dots
            "https://a.example.com",
        ],
    )
    async def test_matching_origins_are_admitted(self, make_app, origin):
        assert _admitted(await _send(make_app(self.PATTERN), origin=origin))

    @pytest.mark.parametrize(
        "origin",
        [
            "https://example.com",  # the bare parent does not match
            "http://a.example.com",  # scheme is part of the pattern
            "https://a.example.com.evil.com",  # pattern is anchored at the end
            "https://a.example.org",
            "https://evil.com",
        ],
    )
    async def test_non_matching_origins_are_rejected(self, make_app, origin):
        assert (await _send(make_app(self.PATTERN), origin=origin)).status_code == 403

    @pytest.mark.parametrize(
        "origin",
        [
            "https://app.example.com:8443",  # the entry names the default port
            "https://app.example.com/x",
            "https://app.example.com@evil.com",
        ],
    )
    async def test_trailing_wildcard_stays_inside_the_host(self, make_app, origin):
        app = make_app("https://app.example.*")
        assert _admitted(await _send(app, origin="https://app.example.com"))
        assert (await _send(app, origin=origin)).status_code == 403

    async def test_wildcard_and_exact_entries_combine(self, make_app):
        app = make_app("https://*.example.com,http://localhost:3000")
        assert _admitted(await _send(app, origin="http://localhost:3000"))
        assert _admitted(await _send(app, origin="https://x.example.com"))
        assert (await _send(app, origin="http://localhost:3001")).status_code == 403


# --- Health probes -----------------------------------------------------------------


@pytest.mark.asyncio
class TestHealthEndpoint:
    """Probes send no Origin, so the guard has nothing to refuse."""

    async def test_reachable_without_an_origin(self, make_app):
        response = await _send(make_app(ALLOWED), "GET", HEALTH_CHECK_PATH)
        assert response.status_code == 200

    @pytest.mark.parametrize("host", ["10.0.3.7:8000", "couchbase-mcp:8000"])
    async def test_reachable_by_pod_ip_or_service_name(self, make_app, host):
        response = await _send(make_app(ALLOWED), "GET", HEALTH_CHECK_PATH, host=host)
        assert response.status_code == 200

    async def test_a_bad_origin_is_refused_here_too(self, make_app):
        """The guard wraps every route, /health included. Accepted: probes and
        browsers do not mix, and a browser sending an Origin is what it judges."""
        response = await _send(
            make_app(ALLOWED), "GET", HEALTH_CHECK_PATH, origin="https://evil.com"
        )
        assert response.status_code == 403

    async def test_mcp_is_guarded_next_to_it(self, make_app):
        app = make_app(ALLOWED)
        assert (await _send(app, "GET", HEALTH_CHECK_PATH)).status_code == 200
        assert (await _send(app, origin="https://evil.com")).status_code == 403


# --- CLI wiring and startup validation ------------------------------------------


def _invoke(args: list[str], env: dict[str, str] | None = None):
    """Run the CLI with every way of actually serving patched out."""
    with (
        patch("cb_mcp.core.app.FastMCP", side_effect=lambda *a, **k: MagicMock()),
        patch("mcp_server.run_app") as run_app,
        patch("mcp_server.uvicorn.run") as uvicorn_run,
    ):
        result = CliRunner().invoke(mcp_server.main, args, env=env)
    return result, run_app, uvicorn_run


SUBCOMMANDS = [
    pytest.param([], id="operational"),
    pytest.param(["operational-insights"], id="operational-insights"),
]


class TestCli:
    @pytest.mark.parametrize("argv", SUBCOMMANDS)
    def test_flag_is_listed_in_help_with_its_env_var(self, argv):
        result = CliRunner().invoke(mcp_server.main, [*argv, "--help"])
        assert result.exit_code == 0, result.output
        assert "--allowed-origins" in result.output
        assert "CB_MCP_ALLOWED_ORIGINS" in result.output

    @pytest.mark.parametrize("argv", SUBCOMMANDS)
    def test_accepted_by_every_server(self, argv):
        result, run_app, _ = _invoke(
            [*argv, "--transport", "http", "--allowed-origins", ALLOWED]
        )
        assert result.exit_code == 0, result.output
        run_app.assert_called_once()

    def test_the_normalized_list_reaches_the_server(self):
        result, run_app, _ = _invoke(
            [
                "--transport",
                "http",
                "--allowed-origins",
                " HTTPS://App.Example.com:443 , http://localhost:3000,",
            ]
        )
        assert result.exit_code == 0, result.output
        assert run_app.call_args.kwargs["allowed_origins"] == (
            "https://app.example.com",
            "http://localhost:3000",
        )

    @pytest.mark.parametrize("value", [None, "", "*"])
    def test_no_list_reaches_the_server_as_empty(self, value):
        args = ["--transport", "http"]
        if value is not None:
            args += ["--allowed-origins", value]
        result, run_app, _ = _invoke(args)
        assert result.exit_code == 0, result.output
        assert run_app.call_args.kwargs["allowed_origins"] == ()

    @pytest.mark.parametrize(
        "value",
        [
            "app.example.com",  # no scheme
            "my_app://example.com",  # '_' is not valid in a scheme
            "https://example.com/path",  # an origin has no path
            "https://example.com?x=1",
            "https://user@example.com",  # no credentials
            "https://example.com:notaport",
            "https://ok.example.com,not-an-origin",  # one bad entry fails the list
            "null",
        ],
    )
    def test_malformed_list_fails_startup_instead_of_allowing_everything(self, value):
        result, run_app, uvicorn_run = _invoke(
            ["--transport", "http", "--allowed-origins", value]
        )
        assert result.exit_code == 2, result.output
        assert value.split(",")[-1] in result.output
        run_app.assert_not_called()
        uvicorn_run.assert_not_called()

    def test_malformed_env_var_fails_startup_too(self):
        result, run_app, _ = _invoke(
            ["--transport", "http"], env={"CB_MCP_ALLOWED_ORIGINS": "nonsense"}
        )
        assert result.exit_code == 2, result.output
        run_app.assert_not_called()

    def test_malformed_list_fails_before_serving_with_workers(self):
        result, run_app, uvicorn_run = _invoke(
            ["--transport", "http", "--workers", "2", "--allowed-origins", "nonsense"]
        )
        assert result.exit_code == 2, result.output
        assert "nonsense" in result.output
        run_app.assert_not_called()
        uvicorn_run.assert_not_called()

    @pytest.mark.parametrize("value", ["", "  ", ",", " , ", "*"])
    def test_blank_list_or_bare_star_is_accepted_as_no_list(self, value):
        result, run_app, _ = _invoke(
            ["--transport", "http", "--allowed-origins", value]
        )
        assert result.exit_code == 0, result.output
        run_app.assert_called_once()

    def test_ignored_on_stdio_without_failing(self):
        """There is no Origin on stdio; the setting is inert, not an error."""
        result, run_app, _ = _invoke(
            ["--transport", "stdio", "--allowed-origins", ALLOWED]
        )
        assert result.exit_code == 0, result.output
        run_app.assert_called_once()

    def test_accepted_on_sse(self):
        result, run_app, _ = _invoke(
            ["--transport", "sse", "--allowed-origins", ALLOWED]
        )
        assert result.exit_code == 0, result.output
        run_app.assert_called_once()

    def test_reaches_every_worker_through_the_handoff(self):
        """Workers rebuild from the supervisor's params, so the list must survive."""
        result, _, _ = _invoke(
            ["--transport", "http", "--workers", "2", "--allowed-origins", ALLOWED]
        )
        assert result.exit_code == 0, result.output
        _, params = decode_worker_config(os.environ[mcp_server.WORKER_CONFIG_ENV_VAR])
        assert params["allowed_origins"] == ALLOWED


# --- Startup errors explain themselves -------------------------------------------

# (entry, regex the explanation for that entry must match). The wording is not
# pinned, only that each kind of mistake is named in plain terms.
BAD_ENTRIES = [
    pytest.param("app.example.com", r"scheme", id="no-scheme"),
    pytest.param("*.example.com", r"scheme", id="wildcard-without-scheme"),
    pytest.param("my_app://example.com", r"scheme.*not valid", id="invalid-scheme"),
    pytest.param("https://example.com/path", r"path", id="path"),
    pytest.param("https://example.com/", r"path|slash", id="trailing-slash"),
    pytest.param("https://example.com?x=1", r"query", id="query"),
    pytest.param("https://example.com#top", r"fragment", id="fragment"),
    pytest.param("https://user@example.com", r"credential|user", id="credentials"),
    pytest.param("https://example.com:notaport", r"port", id="port-not-a-number"),
    pytest.param("https://example.com:99999", r"port", id="port-out-of-range"),
    pytest.param("https://", r"host", id="no-host"),
    pytest.param("null", r"null.*(iframe|sandbox|file)", id="null"),
    pytest.param("https://bücher.example", r"non-ASCII.*xn--bcher-kva", id="idn-host"),
    pytest.param("http://[::1]:3000", r"IPv6.*cannot be matched", id="ipv6"),
]


def _bad_list(value: str, env: dict[str, str] | None = None):
    result, run_app, uvicorn_run = _invoke(
        ["--transport", "http", "--allowed-origins", value], env=env
    )
    assert result.exit_code == 2, result.output
    run_app.assert_not_called()
    uvicorn_run.assert_not_called()
    return result.output


class TestErrorReporting:
    @pytest.mark.parametrize(("entry", "reason"), BAD_ENTRIES)
    def test_names_the_bad_entry_and_says_why(self, entry, reason):
        output = _bad_list(entry)
        assert entry in output
        assert re.search(reason, output, re.IGNORECASE), output

    def test_names_the_flag_and_the_env_var(self):
        """Docker users set the env var and never see the flag."""
        output = _bad_list("nonsense")
        assert "--allowed-origins" in output
        assert "CB_MCP_ALLOWED_ORIGINS" in output

    def test_names_the_env_var_when_it_came_from_the_environment(self):
        result, run_app, _ = _invoke(
            ["--transport", "http"], env={"CB_MCP_ALLOWED_ORIGINS": "nonsense"}
        )
        assert result.exit_code == 2, result.output
        assert "nonsense" in result.output
        assert "CB_MCP_ALLOWED_ORIGINS" in result.output
        run_app.assert_not_called()

    def test_every_bad_entry_is_reported_in_one_go(self):
        """No fix-one-restart-find-the-next loop."""
        output = _bad_list(
            "app.example.com,https://ok.example.com,my_app://x.example.com,"
            "https://y.example.com/path"
        )
        for entry in (
            "app.example.com",
            "my_app://x.example.com",
            "https://y.example.com/path",
        ):
            assert entry in output
        assert re.search(r"scheme", output, re.IGNORECASE)
        assert re.search(r"path", output, re.IGNORECASE)

    def test_each_entry_gets_its_own_reason(self):
        output = _bad_list("app.example.com,https://y.example.com/path")
        lines = [line for line in output.splitlines() if "app.example.com" in line]
        assert any(re.search(r"scheme", line, re.IGNORECASE) for line in lines), output
        lines = [line for line in output.splitlines() if "/path" in line]
        assert any(re.search(r"path", line, re.IGNORECASE) for line in lines), output

    def test_a_valid_entry_is_not_blamed(self):
        output = _bad_list("https://ok.example.com,app.example.com")
        # The bad entry must be reported first, or the check below proves nothing.
        assert "app.example.com" in output
        blamed = [
            line
            for line in output.splitlines()
            if "ok.example.com" in line
            and re.search(r"invalid|scheme|path|port", line, re.IGNORECASE)
        ]
        assert not blamed, output

    def test_shows_what_a_valid_entry_looks_like(self):
        output = _bad_list("app.example.com")
        assert "scheme://host" in output
        assert re.search(
            r"https?://[\w.-]+(:\d+)?\b", output.split("scheme://host")[-1]
        )

    def test_mentions_that_no_path_is_allowed(self):
        """The most common slip is copying a URL instead of an origin."""
        output = _bad_list("https://app.example.com/dashboard")
        assert re.search(r"path", output, re.IGNORECASE)
        assert "https://app.example.com" in output

    @pytest.mark.parametrize(
        "value", ["*,https://a.example.com", "https://a.example.com,*"]
    )
    def test_bare_star_cannot_be_mixed_with_other_entries(self, value):
        output = _bad_list(value)
        assert "'*'" in output
        assert re.search(r"cannot be combined", output, re.IGNORECASE)

    def test_is_a_usage_error_not_a_traceback(self):
        output = _bad_list("nonsense")
        assert "nonsense" in output  # a real report, not just any failure
        assert "Traceback" not in output
        assert "Error" in output

    def test_whitespace_in_the_list_is_not_reported_as_an_error(self):
        result, run_app, _ = _invoke(
            ["--transport", "http", "--allowed-origins", " https://a.example.com , "]
        )
        assert result.exit_code == 0, result.output
        run_app.assert_called_once()

    @pytest.mark.parametrize("argv", SUBCOMMANDS)
    def test_same_report_from_every_server(self, argv):
        result, _, _ = _invoke(
            [*argv, "--transport", "http", "--allowed-origins", "app.example.com"]
        )
        assert result.exit_code == 2, result.output
        assert "app.example.com" in result.output
        assert re.search(r"scheme", result.output, re.IGNORECASE)

    def test_same_report_when_started_with_workers(self):
        """The supervisor validates before spawning, so the operator sees it once."""
        result, run_app, uvicorn_run = _invoke(
            [
                "--transport",
                "http",
                "--workers",
                "2",
                "--allowed-origins",
                "app.example.com",
            ]
        )
        assert result.exit_code == 2, result.output
        assert re.search(r"scheme", result.output, re.IGNORECASE)
        assert result.output.count("app.example.com") <= 3
        run_app.assert_not_called()
        uvicorn_run.assert_not_called()


@pytest.mark.asyncio
class TestSseTransport:
    """SSE is a network transport too: the guard applies to it."""

    async def test_unlisted_origin_is_rejected(self):
        app = _single_process_app(
            ["--transport", "sse", "--host", EXPOSE_ALL, "--allowed-origins", ALLOWED]
        )
        # A wrongly admitted SSE request would stream forever; fail fast instead.
        evil = await asyncio.wait_for(
            _send(app, "GET", "/sse", origin="https://evil.com"), timeout=5
        )
        assert evil.status_code == 403
