"""Origin and Host allow-lists for the network transports, enforced by FastMCP.

Browsers stamp every cross-origin request with an ``Origin`` header that page
JavaScript cannot change, so an allow-list on it stops a malicious web page
from using a visitor's browser to reach this server. Non-browser clients send
any ``Origin`` they like, or none, so this is a guard against browsers, not a
client authentication mechanism; use OAuth for that.

The ``Host`` header is the other half of DNS-rebinding protection: a rebound
page reaches the server under the attacker's hostname, so listing the names the
server is meant to be reached by rejects it whether or not the browser sends
an ``Origin``.

The check itself is FastMCP's own Host/Origin guard
(``fastmcp.server.http.HostOriginGuardMiddleware``); this module validates the
operator's lists and hands them over (:func:`origin_guard_kwargs`). Validation
is ours because FastMCP does none: a malformed entry would simply never match.
An allow-list is a security setting, so a typo must never quietly turn into
"allow everything": a malformed list is an error that names every bad entry and
says why.

What FastMCP's guard does with the list (``host_origin_protection="auto"``):

* A request with **no** ``Origin`` is admitted.
* A request whose ``Origin`` matches an entry is admitted; any other ``Origin``
  gets ``403``. This covers every route, ``/health`` included.
* On a server bound to a loopback address, ``Host`` is validated too (``421``
  for a foreign host), and an ``Origin`` that is itself a loopback address is
  admitted whatever the list says. Bound to anything else (``0.0.0.0`` in a
  container), ``Host`` is not checked, so proxies and Docker names just work.

With a **host** list, ``Host`` is checked on every request whatever the bind
address: a name that is not listed gets ``421``. ``127.0.0.1``, ``localhost``,
``::1`` and the address the server is bound to are always admitted, and the port
in ``Host`` is ignored. Unlike an ``Origin``, a ``Host`` is always present, so
this also covers clients that send no ``Origin``. It also turns the ``Origin``
check on, even with no origin list.
"""

import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import SplitResult, urlsplit

from ..utils.constants import LOGGER_NAMESPACE

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.core.origin_guard")

#: Default ports browsers leave out of the ``Origin`` header. Other schemes
#: (``chrome-extension://``, ``vscode-webview://``, ...) have none, so a port on
#: them is always kept.
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: What the operator should write, shown whenever a list is rejected.
ORIGIN_FORMAT_HINT = (
    "Each entry must be an origin: scheme://host[:port], for example "
    "https://app.example.com, http://localhost:3000 or "
    "chrome-extension://<extension-id>. A '*' in the host "
    "(https://*.example.com) matches any text. Use '*' on its own to allow "
    "every origin."
)


#: The host-list counterpart of :data:`ORIGIN_FORMAT_HINT`.
HOST_FORMAT_HINT = (
    "Each entry must be a host name as it appears in the Host header, without "
    "scheme, port or path, for example mcp.example.com or 10.0.0.5. A '*' "
    "matches any text (*.example.com). Use '*' on its own to allow every host."
)


class OriginListError(ValueError):
    """The origin list has at least one unusable entry.

    ``problems`` pairs each offending entry, as the operator wrote it, with a
    plain-language reason. ``str()`` renders them all, one per line, so the
    operator can fix the whole list in one go.
    """

    kind = "origin"
    hint = ORIGIN_FORMAT_HINT

    def __init__(self, problems: Iterable[tuple[str, str]]) -> None:
        self.problems = list(problems)
        lines = [f"  - {entry!r}: {reason}" for entry, reason in self.problems]
        super().__init__(
            f"invalid {self.kind} list:\n" + "\n".join(lines) + "\n" + self.hint
        )


class HostListError(OriginListError):
    """The host list has at least one unusable entry; see :class:`OriginListError`."""

    kind = "host"
    hint = HOST_FORMAT_HINT


def parse_allowed_origins(raw: str | None) -> tuple[str, ...]:
    """Parse the comma-separated origin list into normalized entries.

    Returns ``()`` when there is nothing to enforce: ``None``, a blank value,
    only commas, or a bare ``*`` (every origin allowed, which is the same as
    no list). Otherwise each entry is lowercased and stripped of a default
    port, matching how browsers write ``Origin``; duplicates collapse and the
    first occurrence keeps its place. Entries may contain ``*`` in the host as
    a wildcard.

    Raises:
        OriginListError: listing every unusable entry, not just the first.
    """
    if raw is None:
        return ()
    entries = [entry.strip() for entry in raw.split(",")]
    entries = [entry for entry in entries if entry]

    if "*" in entries:
        if len(entries) > 1:
            raise OriginListError(
                [
                    (
                        "*",
                        "'*' allows every origin, so it cannot be combined with "
                        "other entries; remove either the '*' or the others",
                    )
                ]
            )
        return ()

    origins: list[str] = []
    problems: list[tuple[str, str]] = []
    for entry in entries:
        try:
            origin = _normalize(entry)
        except _BadEntryError as e:
            problems.append((entry, str(e)))
            continue
        if origin not in origins:
            origins.append(origin)
    if problems:
        raise OriginListError(problems)
    return tuple(origins)


def parse_allowed_hosts(raw: str | None) -> tuple[str, ...]:
    """Parse the comma-separated host list into normalized entries.

    Returns ``()`` when there is nothing to enforce: ``None``, a blank value,
    only commas, or a bare ``*`` (every host allowed, which is the same as no
    list). Otherwise each entry is lowercased; duplicates collapse and the first
    occurrence keeps its place. Entries may contain ``*`` as a wildcard.

    Raises:
        HostListError: listing every unusable entry, not just the first.
    """
    if raw is None:
        return ()
    entries = [entry.strip() for entry in raw.split(",")]
    entries = [entry for entry in entries if entry]

    if "*" in entries:
        if len(entries) > 1:
            raise HostListError(
                [
                    (
                        "*",
                        "'*' allows every host, so it cannot be combined with "
                        "other entries; remove either the '*' or the others",
                    )
                ]
            )
        return ()

    hosts: list[str] = []
    problems: list[tuple[str, str]] = []
    for entry in entries:
        try:
            host = _normalize_host_entry(entry)
        except _BadEntryError as e:
            problems.append((entry, str(e)))
            continue
        if host not in hosts:
            hosts.append(host)
    if problems:
        raise HostListError(problems)
    return tuple(hosts)


def _normalize_host_entry(entry: str) -> str:
    """Return the normalized host for ``entry``, or raise ``_BadEntryError``."""
    if any(char.isspace() for char in entry):
        raise _BadEntryError("contains whitespace; separate entries with commas")
    if "://" in entry:
        raise _BadEntryError(
            "contains a scheme; list only the host name (use --allowed-origins "
            "for origins)"
        )
    if "@" in entry:
        raise _BadEntryError("contains credentials (user@); a host has none")
    for char, what in (("/", "a path"), ("?", "a query string"), ("#", "a fragment")):
        if char in entry:
            raise _BadEntryError(f"contains {what} ({char}...); list only the host")
    # FastMCP drops a trailing ':port' before comparing, so an entry with one
    # would be read as something other than what the operator wrote; an IPv6
    # literal's colons and brackets are unmatchable for the same reason as in
    # an origin.
    if ":" in entry or "[" in entry or "]" in entry:
        raise _BadEntryError(
            "contains ':' or brackets; list the host without a port (the port in "
            "Host is ignored), and name an IPv6 address by a hostname that "
            "resolves to it"
        )
    host = entry.lower()
    _check_ascii_host(host)
    return host


class _BadEntryError(Exception):
    """One entry is unusable; the message is the reason shown to the operator."""


def _normalize(entry: str) -> str:
    """Return the normalized origin for ``entry``, or raise ``_BadEntryError``."""
    if entry.lower() == "null":
        raise _BadEntryError(
            "'null' is what sandboxed iframes and file:// pages send; it is not "
            "a real origin and cannot be allowed"
        )
    if any(char.isspace() for char in entry):
        raise _BadEntryError("contains whitespace; separate entries with commas")
    # urlsplit is lenient: it reads 'localhost:3000' as scheme 'localhost' and
    # 'app.example.com' as a bare path. Insisting on '://' keeps those from
    # looking like origins.
    if "://" not in entry:
        raise _BadEntryError("missing scheme; start with something like https://")
    try:
        parts = urlsplit(entry)
    except ValueError as e:
        raise _BadEntryError(f"is not a valid URL ({e})") from e

    _check_scheme(entry, parts)
    _check_nothing_after_the_authority(entry, parts)
    host = parts.hostname
    if not host:
        raise _BadEntryError("missing host")
    _check_ascii_host(host)
    _check_http_host_characters(parts.scheme, host)
    _check_not_ipv6(host)
    port = _port(parts)

    # urlsplit has already lowercased the scheme and the host.
    origin = f"{parts.scheme}://{host}"
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme):
        origin += f":{port}"
    return origin


def _check_ascii_host(host: str) -> None:
    """Reject internationalized hostnames instead of accepting an entry that can never match.

    Browsers send such a host in punycode (``xn--...``), so an entry written
    with the original letters would be accepted at startup and then never
    admit anyone. Failing loudly, with the form to write, beats converting:
    ``*`` is not valid in IDNA, so a conversion could not cover wildcards.
    """
    if host.isascii():
        return
    if "*" in host:
        fix = "write each non-ASCII label in its punycode (xn--) form"
    else:
        try:
            fix = f"list {host.encode('idna').decode('ascii')!r} instead"
        except UnicodeError:
            fix = "write it in its punycode (xn--) form"
    raise _BadEntryError(
        f"host {host!r} contains non-ASCII characters; browsers send "
        f"internationalized names in punycode, so {fix}"
    )


# Characters a browser rejects or rewrites in an http(s) host, so an entry
# containing one can never equal the Origin it sends.
_FORBIDDEN_HTTP_HOST_CHARS = frozenset('\\^%<>|"`{}')


def _check_http_host_characters(scheme: str, host: str) -> None:
    """Reject http(s) hosts a browser could never send as written.

    ``\\`` starts a path, ``%`` is decoded (``%61`` becomes ``a``) and the
    rest make the host invalid, so such an entry would be accepted at startup
    and then never admit anyone.
    """
    if scheme not in ("http", "https"):
        return
    bad = sorted(_FORBIDDEN_HTTP_HOST_CHARS.intersection(host))
    if bad:
        raise _BadEntryError(
            f"host contains {' '.join(map(repr, bad))}, which browsers do not "
            "allow in a host name (or decode, for '%'); write the plain host"
        )


def _check_not_ipv6(host: str) -> None:
    """Reject IPv6 literals instead of accepting an entry that can never match.

    FastMCP compares origins with ``fnmatch``, which reads the ``[...]`` around
    an IPv6 address as a character class, so ``http://[::1]:3000`` never
    matches the ``Origin`` a browser sends for it.
    """
    if ":" in host:
        raise _BadEntryError(
            f"IPv6 address [{host}] cannot be matched: the origin check reads "
            "the brackets as a pattern; list a hostname that resolves to it instead"
        )


def _check_scheme(entry: str, parts: SplitResult) -> None:
    if parts.scheme:
        return
    # urlsplit found no valid scheme in front of '://': say whether there was
    # none at all or one with characters a scheme cannot have.
    offered = entry.partition("://")[0]
    if not offered:
        raise _BadEntryError("missing scheme; start with something like https://")
    if "*" in offered:
        raise _BadEntryError("a wildcard '*' is only supported in the host")
    raise _BadEntryError(
        f"scheme {offered.lower()!r} is not valid; it must start with a letter "
        "and contain only letters, digits, '+', '-' or '.'"
    )


def _check_nothing_after_the_authority(entry: str, parts: SplitResult) -> None:
    # '?' and '#' are checked on the text because urlsplit reports an empty
    # query or fragment (a lone '?') the same as none.
    if "?" in entry:
        raise _BadEntryError("contains a query string (?...); an origin has none")
    if "#" in entry:
        raise _BadEntryError("contains a fragment (#...); an origin has none")
    if parts.path == "/":
        raise _BadEntryError("has a trailing slash; remove the '/'")
    if parts.path:
        raise _BadEntryError(
            f"contains a path ({parts.path}); an origin is only scheme://host[:port]"
        )
    if "@" in parts.netloc:
        raise _BadEntryError("contains credentials (user@); an origin has none")


def _port(parts: SplitResult) -> int | None:
    try:
        port = parts.port
    except ValueError:
        # Raised for a non-number and for anything above 65535; the port text
        # is whatever follows the last colon of the authority.
        port_text = parts.netloc.rpartition(":")[2]
        if "*" in port_text:
            raise _BadEntryError(
                "a wildcard '*' is only supported in the host, not the port"
            ) from None
        # str.isdigit() also accepts '²' and full-width digits, which int()
        # cannot or should not read, so require plain ASCII digits.
        if not (port_text.isascii() and port_text.isdigit()):
            raise _BadEntryError(f"port {port_text!r} is not a number") from None
        raise _BadEntryError(f"port {port_text} is out of range (1-65535)") from None
    if port is None and parts.netloc.endswith(":"):
        raise _BadEntryError("port '' is not a number")
    if port == 0:
        raise _BadEntryError("port 0 is out of range (1-65535)")
    return port


def origin_guard_kwargs(
    allowed_origins: Iterable[str], allowed_hosts: Iterable[str] = ()
) -> dict[str, Any]:
    """The FastMCP arguments that enforce the allow-lists; empty when there are none.

    Splat into ``FastMCP.run`` / ``FastMCP.http_app``. ``"auto"`` rather than
    ``True`` on purpose: ``True`` validates ``Host`` on every request against
    loopback names only, which answers ``421`` to any container, proxy or
    public hostname. ``"auto"`` leaves ``Host`` alone unless the server is
    bound to loopback or a host list is given. It has to be passed with the
    lists: FastMCP's default (``host_origin_protection=False``) installs no
    guard at all, so ``allowed_origins`` or ``allowed_hosts`` on their own would
    do nothing. With neither list we pass nothing and FastMCP's default applies.
    """
    origins = tuple(allowed_origins)
    hosts = tuple(allowed_hosts)
    if not origins and not hosts:
        return {}
    kwargs: dict[str, Any] = {"host_origin_protection": "auto"}
    if origins:
        logger.info(
            "Origin allow-list active; requests with an Origin header not matching "
            "one of these are rejected: %s",
            ", ".join(origins),
        )
        kwargs["allowed_origins"] = list(origins)
    if hosts:
        logger.info(
            "Host allow-list active; requests whose Host header matches none of "
            "these (or a loopback name or the bound address) are rejected: %s",
            ", ".join(hosts),
        )
        kwargs["allowed_hosts"] = list(hosts)
    return kwargs
