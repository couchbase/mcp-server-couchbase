"""Parsing and validation of the origin allow-list string."""

from __future__ import annotations

import re

import pytest

from cb_mcp.core.origin_guard import OriginListError, parse_allowed_origins


class TestNothingToEnforce:
    @pytest.mark.parametrize("raw", [None, "", "   ", ",", " , ,"])
    def test_blank_is_no_list(self, raw):
        assert parse_allowed_origins(raw) == ()

    @pytest.mark.parametrize("raw", ["*", " * ", "*,", ",*"])
    def test_bare_star_allows_everything_so_there_is_nothing_to_enforce(self, raw):
        assert parse_allowed_origins(raw) == ()


class TestNormalization:
    def test_splits_trims_and_keeps_order(self):
        assert parse_allowed_origins(
            " https://b.example.com , http://localhost:3000,https://a.example.com"
        ) == (
            "https://b.example.com",
            "http://localhost:3000",
            "https://a.example.com",
        )

    def test_lowercases_scheme_and_host(self):
        assert parse_allowed_origins("HTTPS://App.Example.COM") == (
            "https://app.example.com",
        )

    def test_drops_default_ports_only(self):
        assert parse_allowed_origins(
            "https://a.com:443,http://b.com:80,https://c.com:80,http://d.com:443"
        ) == (
            "https://a.com",
            "http://b.com",
            "https://c.com:80",
            "http://d.com:443",
        )

    def test_keeps_non_default_ports(self):
        assert parse_allowed_origins("https://a.com:8443") == ("https://a.com:8443",)

    def test_leading_zeros_in_a_port_are_normalized(self):
        assert parse_allowed_origins("http://a.com:08080") == ("http://a.com:8080",)

    def test_duplicates_collapse_after_normalization(self):
        assert parse_allowed_origins("https://a.com,HTTPS://A.com:443") == (
            "https://a.com",
        )

    def test_blank_entries_between_commas_are_ignored(self):
        assert parse_allowed_origins("https://a.com,, ,https://b.com,") == (
            "https://a.com",
            "https://b.com",
        )


class TestOtherSchemes:
    """Any valid URL scheme is accepted, not just http and https."""

    @pytest.mark.parametrize(
        "origin",
        [
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            "vscode-webview://1a2b3c4d",
            "app://myapp",
            "ftp://example.com",
            "x.y+z-1://host",
        ],
    )
    def test_accepted_as_written(self, origin):
        assert parse_allowed_origins(origin) == (origin,)

    def test_scheme_is_lowercased(self):
        assert parse_allowed_origins("Chrome-Extension://AbC") == (
            "chrome-extension://abc",
        )

    def test_a_port_is_kept_because_other_schemes_have_no_default(self):
        assert parse_allowed_origins("app://host:443,app://host:80") == (
            "app://host:443",
            "app://host:80",
        )

    def test_mixes_with_http_entries(self):
        assert parse_allowed_origins(
            "https://app.example.com,chrome-extension://abc"
        ) == ("https://app.example.com", "chrome-extension://abc")


class TestWildcards:
    def test_wildcard_in_the_host_is_kept_verbatim(self):
        assert parse_allowed_origins("https://*.example.com") == (
            "https://*.example.com",
        )

    def test_wildcard_and_exact_entries_mix(self):
        assert parse_allowed_origins("https://*.example.com,http://localhost:3000") == (
            "https://*.example.com",
            "http://localhost:3000",
        )

    def test_wildcard_host_with_a_port(self):
        assert parse_allowed_origins("https://*.example.com:8443") == (
            "https://*.example.com:8443",
        )

    def test_wildcard_in_the_port_is_rejected(self):
        with pytest.raises(OriginListError, match="only supported in the host"):
            parse_allowed_origins("https://example.com:*")

    def test_bare_star_with_other_entries_is_rejected(self):
        with pytest.raises(OriginListError) as exc:
            parse_allowed_origins("*,https://a.example.com")
        assert exc.value.problems[0][0] == "*"
        assert "cannot be combined" in str(exc.value)

    def test_bare_star_with_other_entries_is_rejected_in_any_order(self):
        with pytest.raises(OriginListError, match="cannot be combined"):
            parse_allowed_origins("https://a.example.com,*")


# (entry, regex the reason must match)
BAD = [
    ("app.example.com", r"missing scheme"),
    ("*.example.com", r"missing scheme"),
    ("localhost:3000", r"missing scheme"),
    ("my_app://example.com", r"scheme 'my_app' is not valid"),
    ("1app://example.com", r"scheme '1app' is not valid"),
    ("://example.com", r"missing scheme"),
    ("*://example.com", r"wildcard.*host"),
    ("https://example.com/path", r"path \(/path\)"),
    ("https://example.com/", r"trailing slash"),
    ("https://example.com?x=1", r"query"),
    ("https://example.com#top", r"fragment"),
    ("https://example.com?", r"query"),
    ("https://example.com#", r"fragment"),
    ("https://[::1", r"not a valid URL"),
    ("https://user@example.com", r"credentials"),
    ("https://example.com:notaport", r"port 'notaport' is not a number"),
    ("https://example.com:99999", r"port 99999 is out of range"),
    ("https://example.com:0", r"port 0 is out of range"),
    ("https://example.com:", r"port '' is not a number"),
    ("https://", r"missing host"),
    ("https://:3000", r"missing host"),
    ("null", r"null.*(iframe|sandbox|file)"),
    ("https://bücher.example", r"non-ASCII.*xn--bcher-kva\.example"),
    ("http://[::1]:3000", r"IPv6 address \[::1\] cannot be matched"),
    ("https://[2001:db8::1]", r"IPv6 address .* cannot be matched"),
    ("https://*.bücher.example", r"non-ASCII.*punycode"),
    ("NULL", r"null.*(iframe|sandbox|file)"),
    ("https://a .com", r"whitespace"),
]


class TestRejection:
    @pytest.mark.parametrize(("entry", "reason"), BAD, ids=[b[0] for b in BAD])
    def test_each_mistake_is_named(self, entry, reason):
        with pytest.raises(OriginListError) as exc:
            parse_allowed_origins(entry)
        assert [e for e, _ in exc.value.problems] == [entry.strip()]
        assert re.search(reason, exc.value.problems[0][1], re.IGNORECASE), (
            exc.value.problems
        )

    def test_every_bad_entry_is_reported_together(self):
        with pytest.raises(OriginListError) as exc:
            parse_allowed_origins(
                "app.example.com,https://ok.example.com,my_app://x.example.com,"
                "https://y.example.com/path"
            )
        assert [e for e, _ in exc.value.problems] == [
            "app.example.com",
            "my_app://x.example.com",
            "https://y.example.com/path",
        ]

    def test_a_valid_entry_is_never_in_the_problems(self):
        with pytest.raises(OriginListError) as exc:
            parse_allowed_origins("https://ok.example.com,nonsense")
        assert all("ok.example.com" not in entry for entry, _ in exc.value.problems)

    def test_the_message_lists_each_entry_with_its_reason_and_the_format(self):
        with pytest.raises(OriginListError) as exc:
            parse_allowed_origins("app.example.com,https://y.example.com/path")
        text = str(exc.value)
        assert "'app.example.com': missing scheme" in text
        assert "'https://y.example.com/path': contains a path" in text
        assert "scheme://host[:port]" in text
        assert "https://app.example.com" in text

    def test_is_a_value_error(self):
        assert issubclass(OriginListError, ValueError)
