"""Parsing and validation of the host allow-list string."""

from __future__ import annotations

import pytest

from cb_mcp.core.origin_guard import (
    HostListError,
    OriginListError,
    origin_guard_kwargs,
    parse_allowed_hosts,
)


class TestParse:
    @pytest.mark.parametrize("raw", [None, "", "   ", ",", " , ,", "*", " * "])
    def test_nothing_to_enforce(self, raw):
        assert parse_allowed_hosts(raw) == ()

    def test_lowercases_trims_and_dedupes_keeping_order(self):
        assert parse_allowed_hosts(" B.com , a.COM,b.com,,") == ("b.com", "a.com")

    def test_wildcards_and_ip_addresses_pass_through(self):
        assert parse_allowed_hosts("*.example.com,10.0.0.5") == (
            "*.example.com",
            "10.0.0.5",
        )

    def test_star_cannot_be_combined(self):
        with pytest.raises(HostListError, match="cannot be combined"):
            parse_allowed_hosts("*,a.com")


class TestRejects:
    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            ("https://a.com", "scheme"),
            ("a.com:8000", "port"),
            ("[::1]", "brackets"),
            ("::1", "port"),
            ("a.com/path", "path"),
            ("a.com?x=1", "query"),
            ("a.com#frag", "fragment"),
            ("user@a.com", "credentials"),
            ("a b.com", "whitespace"),
            ("bücher.example", "punycode"),
        ],
    )
    def test_bad_entry_is_named_with_a_reason(self, entry, reason):
        with pytest.raises(HostListError, match=reason) as exc:
            parse_allowed_hosts(entry)
        assert exc.value.problems[0][0] == entry
        assert "invalid host list" in str(exc.value)

    def test_every_bad_entry_is_reported(self):
        with pytest.raises(HostListError) as exc:
            parse_allowed_hosts("https://a.com,ok.com,b.com:1")
        assert [entry for entry, _ in exc.value.problems] == [
            "https://a.com",
            "b.com:1",
        ]

    def test_is_an_origin_list_error_so_one_handler_covers_both(self):
        assert issubclass(HostListError, OriginListError)


class TestGuardKwargs:
    def test_no_lists_means_no_kwargs(self):
        assert origin_guard_kwargs((), ()) == {}
        assert origin_guard_kwargs(()) == {}

    def test_hosts_only(self):
        assert origin_guard_kwargs((), ("a.com",)) == {
            "host_origin_protection": "auto",
            "allowed_hosts": ["a.com"],
        }

    def test_origins_only_is_unchanged(self):
        assert origin_guard_kwargs(("https://a.com",)) == {
            "host_origin_protection": "auto",
            "allowed_origins": ["https://a.com"],
        }

    def test_both(self):
        assert origin_guard_kwargs(("https://a.com",), ("a.com",)) == {
            "host_origin_protection": "auto",
            "allowed_origins": ["https://a.com"],
            "allowed_hosts": ["a.com"],
        }
