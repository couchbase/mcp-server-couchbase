"""Utilities for extracting and evaluating SQL++ EXPLAIN plans."""

from collections import Counter
from typing import Any


def extract_plan_from_explain_results(explain_results):
    if not explain_results:
        return None
    return explain_results[0].get("plan")


def _walk_plan(
    node: Any,
    operators: list[str],
    indexes_used: set[str],
    keyspaces: set[str],
) -> None:
    """Recursively walk an EXPLAIN plan node and collect details."""
    if isinstance(node, dict):
        operator = node.get("#operator")
        if isinstance(operator, str):
            operators.append(operator)

        index_name = node.get("index")
        if isinstance(index_name, str) and index_name:
            indexes_used.add(index_name)

        keyspace_name = node.get("keyspace")
        if isinstance(keyspace_name, str) and keyspace_name:
            keyspaces.add(keyspace_name)

        for value in node.values():
            _walk_plan(value, operators, indexes_used, keyspaces)
    elif isinstance(node, list):
        for item in node:
            _walk_plan(item, operators, indexes_used, keyspaces)


def evaluate_query_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate an EXPLAIN plan and return optimization findings."""
    if not plan:
        return {
            "summary": "No query plan found in EXPLAIN output.",
            "operators": [],
            "operator_counts": {},
            "indexes_used": [],
            "keyspaces": [],
            "findings": [],
        }

    operators: list[str] = []
    indexes_used: set[str] = set()
    keyspaces: set[str] = set()

    _walk_plan(plan, operators, indexes_used, keyspaces)

    operator_counts = dict(Counter(operators))
    findings: list[dict[str, str]] = []

    has_primary_scan = any(op.startswith("PrimaryScan") for op in operators)
    has_secondary_index_scan = any(op.startswith("IndexScan") for op in operators)
    has_fetch = any(op.startswith("Fetch") for op in operators)

    if has_primary_scan:
        findings.append(
            {
                "severity": "warning",
                "issue": "primary_index_scan",
                "message": (
                    "Primary index scan detected. Consider creating a targeted "
                    "secondary index for better selectivity."
                ),
            }
        )

    if has_fetch and has_secondary_index_scan:
        findings.append(
            {
                "severity": "warning",
                "issue": "non_covering_index",
                "message": (
                    "Fetch operator detected after secondary index scan. "
                    "A covering index may reduce document fetches."
                ),
            }
        )

    if not findings:
        findings.append(
            {
                "severity": "info",
                "issue": "no_obvious_issues",
                "message": ("No common anti-patterns detected from the query plan."),
            }
        )

    summary = (
        "Plan has optimization opportunities."
        if any(f["severity"] == "warning" for f in findings)
        else "Plan looks healthy for common query-plan checks."
    )

    return {
        "summary": summary,
        "operators": sorted(operator_counts),
        "operator_counts": operator_counts,
        "indexes_used": sorted(indexes_used),
        "keyspaces": sorted(keyspaces),
        "findings": findings,
    }
