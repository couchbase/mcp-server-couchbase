"""Unit tests for query plan evaluation helpers."""

from cb_mcp.tools.operational.query import evaluate_query_plan
from cb_mcp.utils.operational.query_utils import (
    analyze_parameter_binding,
    extract_plan_from_explain_results,
)


def test_evaluate_query_plan_detects_primary_scan() -> None:
    """Primary scans should be reported as optimization findings."""
    plan = {
        "#operator": "Sequence",
        "~children": [
            {
                "#operator": "PrimaryScan3",
                "index": "def_inventory_landmark_primary",
                "keyspace": "landmark",
            },
            {"#operator": "Fetch", "keyspace": "landmark"},
        ],
    }

    evaluation = evaluate_query_plan(plan)

    assert evaluation["summary"] == "Plan has optimization opportunities."
    issues = {finding["issue"] for finding in evaluation["findings"]}
    assert "primary_index_scan" in issues
    assert "PrimaryScan3" in evaluation["operators"]


def test_evaluate_query_plan_detects_no_obvious_issues() -> None:
    """Secondary-index-only plans without fetch should be marked healthy."""
    plan = {
        "#operator": "Sequence",
        "~children": [
            {
                "#operator": "IndexScan3",
                "index": "idx_users_status",
                "keyspace": "users",
            },
            {"#operator": "InitialProject"},
        ],
    }

    evaluation = evaluate_query_plan(plan)

    assert evaluation["summary"] == "Plan looks healthy for common query-plan checks."
    assert evaluation["indexes_used"] == ["idx_users_status"]
    assert evaluation["findings"][0]["issue"] == "no_obvious_issues"


def test_evaluate_query_plan_handles_missing_plan() -> None:
    """A missing plan should produce an empty, non-failing evaluation payload."""
    evaluation = evaluate_query_plan(None)

    assert evaluation["summary"] == "No query plan found in EXPLAIN output."
    assert evaluation["operators"] == []
    assert evaluation["findings"] == []


def test_extract_plan_from_explain_results_with_documented_shape() -> None:
    """EXPLAIN payload with top-level `plan` should be extracted."""
    plan = {"#operator": "Sequence", "~children": []}
    results = [{"plan": plan, "text": "EXPLAIN"}]

    assert extract_plan_from_explain_results(results) == plan


def test_extract_plan_from_explain_results_empty_list_returns_none() -> None:
    """Empty results list should return None rather than IndexError."""
    assert extract_plan_from_explain_results([]) is None


def test_extract_plan_from_explain_results_none_returns_none() -> None:
    """None input should return None defensively."""
    assert extract_plan_from_explain_results(None) is None


def test_evaluate_query_plan_detects_non_covering_index() -> None:
    """IndexScan + Fetch should be flagged as a non-covering index opportunity."""
    plan = {
        "#operator": "Sequence",
        "~children": [
            {
                "#operator": "IndexScan3",
                "index": "idx_users_status",
                "keyspace": "users",
            },
            {"#operator": "Fetch", "keyspace": "users"},
        ],
    }

    evaluation = evaluate_query_plan(plan)

    assert evaluation["summary"] == "Plan has optimization opportunities."
    issues = {finding["issue"] for finding in evaluation["findings"]}
    assert "non_covering_index" in issues
    # No primary scan present — only the non-covering finding should fire.
    assert "primary_index_scan" not in issues


def test_evaluate_query_plan_detects_non_covering_index_with_versioned_fetch() -> None:
    """A versioned Fetch operator (e.g. Fetch3) should still be detected."""
    plan = {
        "#operator": "Sequence",
        "~children": [
            {
                "#operator": "IndexScan3",
                "index": "idx_users_status",
                "keyspace": "users",
            },
            {"#operator": "Fetch3", "keyspace": "users"},
        ],
    }

    evaluation = evaluate_query_plan(plan)

    issues = {finding["issue"] for finding in evaluation["findings"]}
    assert "non_covering_index" in issues


class TestAnalyzeParameterBinding:
    """Placeholder-versus-supplied-value comparison for EXPLAIN."""

    def test_non_parameterized_query_is_fully_bound(self) -> None:
        report = analyze_parameter_binding("SELECT * FROM t WHERE a = 1", None)

        assert report["parameterized"] is False
        assert report["fully_bound"] is True
        assert report["detected"] == {"named": [], "positional": 0}
        assert report["unbound"] == {"named": [], "positional": 0}

    def test_named_placeholders_without_values_are_unbound(self) -> None:
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = $country AND b = $city", None
        )

        assert report["parameterized"] is True
        assert report["fully_bound"] is False
        assert report["detected"]["named"] == ["city", "country"]
        assert report["unbound"]["named"] == ["city", "country"]

    def test_named_placeholders_with_values_are_bound(self) -> None:
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = $country",
            {"country": "France"},
        )

        assert report["fully_bound"] is True
        assert report["bound"]["named"] == ["country"]
        assert report["unbound"]["named"] == []

    def test_partially_bound_named_placeholders(self) -> None:
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = $country AND b = $city",
            {"country": "France"},
        )

        assert report["fully_bound"] is False
        assert report["unbound"]["named"] == ["city"]

    def test_values_are_never_echoed_back(self) -> None:
        """Only names and counts may cross into the response payload."""
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = $email",
            {"email": "someone@example.com"},
        )

        assert "someone@example.com" not in repr(report)

    def test_positional_placeholders_are_always_unbound(self) -> None:
        """Nothing binds ``$1``/``?``, so every one detected stays unbound."""
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = ? AND b = ?", None
        )

        assert report["detected"]["positional"] == 2
        assert report["unbound"]["positional"] == 2
        assert report["fully_bound"] is False

    def test_named_values_do_not_bind_positional_placeholders(self) -> None:
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = $country AND b = ?",
            {"country": "France"},
        )

        assert report["unbound"]["named"] == []
        assert report["unbound"]["positional"] == 1
        assert report["fully_bound"] is False

    def test_unreferenced_named_value_is_reported(self) -> None:
        """A typo'd name leaves the real placeholder unbound — flag it."""
        report = analyze_parameter_binding(
            "SELECT * FROM t WHERE a = $country",
            {"contry": "France"},
        )

        assert report["unused_named"] == ["contry"]
        assert report["unbound"]["named"] == ["country"]
        assert report["fully_bound"] is False

    def test_empty_named_mapping_is_treated_as_no_values(self) -> None:
        report = analyze_parameter_binding("SELECT * FROM t WHERE a = $country", {})

        assert report["unbound"]["named"] == ["country"]


PRIMARY_SCAN_PLAN = {
    "#operator": "Sequence",
    "~children": [
        {
            "#operator": "PrimaryScan3",
            "index": "def_primary",
            "keyspace": "hotel",
        },
        {"#operator": "Fetch", "keyspace": "hotel"},
    ],
}

HEALTHY_PLAN = {
    "#operator": "Sequence",
    "~children": [
        {
            "#operator": "IndexScan3",
            "index": "idx_users_status",
            "keyspace": "users",
        },
        {"#operator": "InitialProject"},
    ],
}


class TestEvaluateQueryPlanWithUnboundParameters:
    """Findings are provisional when the plan was built without values."""

    def test_bound_plan_findings_are_definite(self) -> None:
        evaluation = evaluate_query_plan(PRIMARY_SCAN_PLAN)

        (finding,) = [
            f for f in evaluation["findings"] if f["issue"] == "primary_index_scan"
        ]
        assert finding["confidence"] == "definite"
        assert finding["severity"] == "warning"
        assert "Parameter values were not supplied" not in finding["message"]
        assert evaluation["summary"] == "Plan has optimization opportunities."

    def test_unbound_plan_downgrades_primary_scan_to_possible(self) -> None:
        evaluation = evaluate_query_plan(PRIMARY_SCAN_PLAN, parameters_unbound=True)

        (finding,) = [
            f for f in evaluation["findings"] if f["issue"] == "primary_index_scan"
        ]
        assert finding["confidence"] == "possible"
        # Severity is kept: a primary scan is still worth looking at.
        assert finding["severity"] == "warning"
        assert "Parameter values were not supplied" in finding["message"]
        assert "may not match runtime" in evaluation["summary"]

    def test_unbound_plan_also_qualifies_a_healthy_verdict(self) -> None:
        """ "No issues found" is just as unreliable without values."""
        evaluation = evaluate_query_plan(HEALTHY_PLAN, parameters_unbound=True)

        (finding,) = evaluation["findings"]
        assert finding["issue"] == "no_obvious_issues"
        assert finding["confidence"] == "possible"
        assert "may not match runtime" in evaluation["summary"]

    def test_bound_healthy_plan_keeps_its_original_summary(self) -> None:
        evaluation = evaluate_query_plan(HEALTHY_PLAN)

        assert (
            evaluation["summary"] == "Plan looks healthy for common query-plan checks."
        )
        assert evaluation["findings"][0]["confidence"] == "definite"

    def test_missing_plan_is_unaffected_by_the_flag(self) -> None:
        evaluation = evaluate_query_plan(None, parameters_unbound=True)

        assert evaluation["summary"] == "No query plan found in EXPLAIN output."
        assert evaluation["findings"] == []
