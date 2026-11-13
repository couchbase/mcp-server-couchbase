"""Utilities for extracting and evaluating SQL++ EXPLAIN plans."""

from collections import Counter
from typing import Any

from ..sqlpp import find_query_placeholders


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


def analyze_parameter_binding(
    statement: str,
    named_parameters: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare a statement's placeholders against the values supplied for them.

    Only names and counts are reported, never the bound values themselves.

    Positional placeholders (``$1``, ``?``) are always counted as unbound:
    the query tools bind ``$name`` placeholders only.

    ``unused_named`` reports values bound under a name the statement never
    references. The server ignores them, which leaves the real placeholder
    unbound.
    """
    placeholders = find_query_placeholders(statement)
    bound_named = set(named_parameters or {})
    unbound_named = sorted(placeholders.named - bound_named)

    return {
        "parameterized": bool(placeholders),
        "detected": {
            "named": sorted(placeholders.named),
            "positional": placeholders.positional,
        },
        "bound": {"named": sorted(bound_named)},
        "unbound": {
            "named": unbound_named,
            "positional": placeholders.positional,
        },
        "unused_named": sorted(bound_named - placeholders.named),
        "fully_bound": not unbound_named and placeholders.positional == 0,
    }


# Appended to a plan finding when the EXPLAIN ran with placeholders unbound.
_UNBOUND_CAVEAT = (
    " Parameter values were not supplied for this EXPLAIN, so the optimizer "
    "planned without them and may pick a different index at runtime. Re-run "
    "with named_parameters to confirm."
)


def evaluate_query_plan(
    plan: dict[str, Any] | None,
    *,
    parameters_unbound: bool = False,
) -> dict[str, Any]:
    """Evaluate an EXPLAIN plan and return optimization findings.

    ``parameters_unbound`` marks the evaluation as provisional: findings are
    tagged ``confidence: "possible"`` instead of ``"definite"`` and carry a
    caveat. Severity is unchanged.
    """
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

    for finding in findings:
        finding["confidence"] = "possible" if parameters_unbound else "definite"
        if parameters_unbound:
            finding["message"] += _UNBOUND_CAVEAT

    if any(f["severity"] == "warning" for f in findings):
        summary = (
            "Plan has possible optimization opportunities, but parameters were "
            "unbound so the plan may not match runtime."
            if parameters_unbound
            else "Plan has optimization opportunities."
        )
    else:
        summary = (
            "No common anti-patterns detected, but parameters were unbound so "
            "the plan may not match runtime."
            if parameters_unbound
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
