"""Unit tests for discover_tool_input_values.

No cluster mocking anywhere: this tool never opens a connection, which is the point of it --
looking up a metric name has to work while the cluster is down.

Tests that exercise format behaviour (chapterless datasets, memory scaling) build their own
temporary datasets rather than leaning on the shipped one, so they stay meaningful if the
Couchbase metrics reference changes.
"""

import json
import resource
import sys
from collections.abc import Iterator

import pytest

from cb_mcp.tools.reference import discover_tool_input_values
from cb_mcp.utils import reference_data

METRICS_TOOL = "get_cluster_metrics"

# A metric that exists in the shipped dataset with a distinctive, stable name.
KNOWN_METRIC = "kv_audit_dropped_events"


@pytest.fixture(autouse=True)
def _clear_dataset_caches() -> Iterator[None]:
    """Datasets are lru_cached by path; tests that write temp datasets must not leak state."""
    reference_data._registry.cache_clear()
    reference_data.load_envelope.cache_clear()
    reference_data.chapters.cache_clear()
    yield
    reference_data._registry.cache_clear()
    reference_data.load_envelope.cache_clear()
    reference_data.chapters.cache_clear()


def _write_dataset(path, envelope, records) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope) + "\n")
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return str(path)


def _point_registry_at(monkeypatch, path: str, tool_name: str) -> None:
    """Register a temp dataset without touching the shipped reference_data directory."""
    monkeypatch.setattr(reference_data, "_dataset_paths", lambda: [path])
    reference_data._registry.cache_clear()
    assert reference_data.resolve_dataset(tool_name) == path


class TestToolNameResolution:
    def test_unknown_tool_name_lists_what_is_registered(self):
        result = discover_tool_input_values("no_such_tool")

        assert result["success"] is False
        assert METRICS_TOOL in result["available_tool_names"]

    @pytest.mark.parametrize(
        "alias",
        [
            "get_cluster_metrics",
            "Get Cluster Metrics",
            "get-cluster-metrics",
            "GET_CLUSTER_METRICS",
        ],
    )
    def test_tool_name_is_normalized(self, alias):
        assert discover_tool_input_values(alias)["success"] is True

    def test_dataset_id_also_resolves(self):
        assert discover_tool_input_values("couchbase_server_metrics")["success"] is True


class TestBrowseMode:
    def test_tool_name_alone_is_a_valid_call(self):
        """Every parameter except tool_name is optional."""
        result = discover_tool_input_values(METRICS_TOOL)

        assert result["success"] is True
        assert "results" not in result, "browse mode must not run a search"

    def test_default_call_lists_every_record(self):
        """No keywords means the caller gets the whole namespace in one response."""
        result = discover_tool_input_values(METRICS_TOOL)

        assert result["chapters"], "chapters tell the caller how the data is organised"
        assert result["record_count"] > 1000, (
            f"expected the full metrics dataset, got {result['record_count']} records"
        )
        assert "next_step" in result

    def test_max_results_does_not_truncate_the_listing(self):
        """max_results applies to search only; the default listing is never cut short."""
        capped = discover_tool_input_values(METRICS_TOOL, max_results=5)

        assert capped["record_count"] > 5

    def test_browse_chapter_values_carry_counts(self):
        chapters = discover_tool_input_values(METRICS_TOOL)["chapters"]

        assert "category" in chapters
        assert all(isinstance(count, int) for count in chapters["category"].values())

    def test_chapterless_dataset_still_guides_the_caller(self, tmp_path, monkeypatch):
        """A dataset may have no chapters; browse must not dead-end on an empty object."""
        path = _write_dataset(
            tmp_path / "flat.jsonl",
            {
                "schema_version": 1,
                "dataset_id": "flat_dataset",
                "title": "Flat",
                "tools": ["flat_tool"],
                "source_url": "https://example.invalid",
                "generated_at": "2026-09-10",
                "record_count": 2,
                "id_field": "code",
                "search_fields": [{"field": "code", "weight": 1.0}],
            },
            [{"code": "E001"}, {"code": "E002"}],
        )
        _point_registry_at(monkeypatch, path, "flat_tool")

        result = discover_tool_input_values("flat_tool")

        assert result["success"] is True
        assert result["chapters"] == {}
        assert result["records"] == [{"code": "E001"}, {"code": "E002"}]

    def test_dataset_over_the_size_cap_is_not_listed(self, tmp_path, monkeypatch):
        """Above MAX_LIST_BYTES the caller is pointed at search, never given a partial list.

        The shipped metrics dataset sits ~19 KB under the cap, so this branch is a near-term
        reality rather than a theoretical one and has to be covered.
        """
        filler = "x" * 500
        path = _write_dataset(
            tmp_path / "big.jsonl",
            {
                "schema_version": 1,
                "dataset_id": "big_dataset",
                "title": "Big",
                "tools": ["big_tool"],
                "source_url": "https://example.invalid",
                "generated_at": "2026-09-11",
                "record_count": 2000,
                "id_field": "name",
                "search_fields": [{"field": "name", "weight": 1.0}],
            },
            [{"name": f"metric_{i}", "description": filler} for i in range(2000)],
        )
        assert reference_data.dataset_size_bytes(path) > reference_data.MAX_LIST_RESPONSE_BYTES
        _point_registry_at(monkeypatch, path, "big_tool")

        result = discover_tool_input_values("big_tool")

        assert result["success"] is True
        assert "records" not in result, "an over-cap dataset must not be listed at all"
        assert "search_keywords" in result["next_step"]


class TestSearch:
    def test_exact_name_ranks_first(self):
        result = discover_tool_input_values(METRICS_TOOL, [KNOWN_METRIC])

        assert result["results"][0]["name"] == KNOWN_METRIC

    def test_keywords_are_a_list_of_separate_concepts(self):
        result = discover_tool_input_values(METRICS_TOOL, ["disk", "write", "queue"])

        assert result["matches"] > 0
        assert any("disk" in row["name"] for row in result["results"])

    def test_a_bare_string_is_accepted_rather_than_rejected(self):
        """A caller passing a string instead of a list is a slip, not worth a failed round trip."""
        as_string = discover_tool_input_values(METRICS_TOOL, "disk write queue")
        as_list = discover_tool_input_values(METRICS_TOOL, ["disk", "write", "queue"])

        assert as_string["success"] is True
        assert [r["name"] for r in as_string["results"]] == [
            r["name"] for r in as_list["results"]
        ]

    def test_results_carry_scores_and_are_ranked(self):
        results = discover_tool_input_values(METRICS_TOOL, ["disk", "queue"])["results"]

        scores = [row["score"] for row in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_response_still_includes_chapters(self):
        """So the caller can narrow on the next call without a separate browse."""
        result = discover_tool_input_values(METRICS_TOOL, ["disk"])

        assert result["chapters"]

    def test_nothing_is_filtered_out_by_default(self):
        """min_score defaults to 0: weak matches are returned and scored, not hidden."""
        result = discover_tool_input_values(
            METRICS_TOOL, ["zzzqqqxxx", "nothingmatchesthis"], max_results=3
        )

        assert result["success"] is True
        assert result["results"], (
            "a default search should still rank and return something"
        )
        assert all(row["score"] < 50 for row in result["results"]), (
            "nonsense keywords should come back with visibly low scores, not be suppressed"
        )

    def test_zero_matches_is_a_success_not_an_error(self):
        """Reachable when the caller raises min_score above anything the data scores."""
        result = discover_tool_input_values(
            METRICS_TOOL, ["zzzqqqxxx", "nothingmatchesthis"], min_score=90.0
        )

        assert result["success"] is True
        assert result["matches"] == 0
        assert result["results"] == []
        assert "next_step" in result

    def test_min_score_narrows_a_search(self):
        strict = discover_tool_input_values(
            METRICS_TOOL, ["fragmentation"], min_score=95.0
        )
        loose = discover_tool_input_values(
            METRICS_TOOL, ["fragmentation"], min_score=50.0
        )

        assert loose["matches"] > strict["matches"]


class TestMaxResults:
    @pytest.mark.parametrize("max_results", [1, 3, 50, 500])
    def test_max_results_is_honored_exactly_and_never_clamped(self, max_results):
        result = discover_tool_input_values(
            METRICS_TOOL, ["disk"], max_results=max_results
        )

        assert result["returned"] == min(max_results, result["matches"])

    def test_matches_reports_the_full_count_even_when_fewer_are_returned(self):
        result = discover_tool_input_values(METRICS_TOOL, ["disk"], max_results=2)

        assert result["returned"] == 2
        assert result["matches"] > 2


class TestChapterFilters:
    def test_filter_narrows_to_the_requested_chapter(self):
        result = discover_tool_input_values(
            METRICS_TOOL, ["memory", "usage"], {"category": "Query Service Metrics"}
        )

        assert result["matches"] > 0
        assert {row["category"] for row in result["results"]} == {
            "Query Service Metrics"
        }

    def test_filter_fixes_the_wrong_service_failure(self):
        """Identifiers use kv_/n1ql_/fts_ prefixes that keywords cannot reach; filters can."""
        unfiltered = discover_tool_input_values(
            METRICS_TOOL, ["memory", "usage"], max_results=10
        )
        filtered = discover_tool_input_values(
            METRICS_TOOL,
            ["memory", "usage"],
            {"category": "Query Service Metrics"},
            max_results=10,
        )

        assert {row["category"] for row in unfiltered["results"]} != {
            "Query Service Metrics"
        }
        assert {row["category"] for row in filtered["results"]} == {
            "Query Service Metrics"
        }

    def test_multiple_filters_are_combined_with_and(self):
        result = discover_tool_input_values(
            METRICS_TOOL,
            ["disk"],
            {"category": "Data Service Metrics", "metric_type": "gauge"},
            max_results=100,
        )

        for row in result["results"]:
            assert row["category"] == "Data Service Metrics"
            assert row["metric_type"] == "gauge"

    def test_unknown_chapter_field_names_the_valid_ones(self):
        result = discover_tool_input_values(
            METRICS_TOOL, ["disk"], {"not_a_chapter": "x"}
        )

        assert result["success"] is False
        assert "category" in result["valid_chapter_fields"]
        assert [entry["filter"] for entry in result["invalid_filters"]] == [
            "not_a_chapter"
        ]

    def test_unknown_chapter_value_lists_the_valid_values(self):
        result = discover_tool_input_values(
            METRICS_TOOL, ["disk"], {"category": "Nope Metrics"}
        )

        assert result["success"] is False
        assert "Data Service Metrics" in result["invalid_filters"][0]["valid_values"]

    def test_every_bad_filter_is_reported_in_one_response(self):
        """Reporting only the first would cost a round trip per bad filter."""
        result = discover_tool_input_values(
            METRICS_TOOL,
            ["disk"],
            {"not_a_chapter": "x", "category": "Nope Metrics", "metric_type": "gauge"},
        )

        assert result["success"] is False
        reported = {entry["filter"] for entry in result["invalid_filters"]}
        assert reported == {"not_a_chapter", "category"}, (
            "both bad filters must be reported, and the valid one must not be"
        )
        # Everything needed to fix both at once is in this single response.
        assert "category" in result["chapters"]
        assert "Data Service Metrics" in result["chapters"]["category"]

    def test_filters_are_validated_in_browse_mode_too(self):
        result = discover_tool_input_values(METRICS_TOOL, None, {"not_a_chapter": "x"})

        assert result["success"] is False


class TestStreamingInvariant:
    """The whole point of the JSONL format is that memory does not scale with dataset size."""

    def _peak_rss_mb(self) -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux reports kilobytes.
        return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024

    def test_memory_stays_flat_as_the_dataset_grows(self, tmp_path, monkeypatch):
        envelope = {
            "schema_version": 1,
            "dataset_id": "big_dataset",
            "title": "Big",
            "tools": ["big_tool"],
            "source_url": "https://example.invalid",
            "generated_at": "2026-09-10",
            "record_count": 0,
            "id_field": "name",
            "search_fields": [
                {"field": "name", "weight": 1.0, "split_underscores": True},
                {"field": "description", "weight": 0.85},
            ],
        }
        description = (
            "a description long enough to carry realistic weight per record " * 3
        )

        def build(path, count):
            envelope["record_count"] = count
            return _write_dataset(
                path,
                envelope,
                [
                    {"name": f"kv_ep_disk_queue_metric_{i}", "description": description}
                    for i in range(count)
                ],
            )

        small = build(tmp_path / "small.jsonl", 1_000)
        large = build(tmp_path / "large.jsonl", 20_000)

        _point_registry_at(monkeypatch, small, "big_tool")
        discover_tool_input_values("big_tool", ["disk", "queue"], max_results=25)
        after_small = self._peak_rss_mb()

        reference_data.load_envelope.cache_clear()
        reference_data.chapters.cache_clear()
        _point_registry_at(monkeypatch, large, "big_tool")
        result = discover_tool_input_values(
            "big_tool", ["disk", "queue"], max_results=25
        )
        after_large = self._peak_rss_mb()

        assert result["matches"] == 20_000, (
            "the large dataset really was searched end to end"
        )
        # 20x the records. Holding them would cost tens of MB; streaming costs ~nothing.
        assert after_large - after_small < 10.0, (
            f"peak RSS grew {after_large - after_small:.1f} MB when the dataset grew 20x -- "
            "something is materialising records instead of streaming them"
        )
