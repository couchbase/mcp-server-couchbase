"""Unit tests for get_index_advisor_recommendations SQL++ injection hardening.

These verify that the user-supplied query is handed to ADVISOR as a *bound
named parameter* and is never concatenated into the statement string. A
regression to f-string concatenation (the original injection vector) would
fail these tests without needing a live Couchbase cluster.
"""

from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.index import get_index_advisor_recommendations

# Queries with single quotes, doubled quotes, and an injection-style payload —
# exactly the inputs that broke the old string-concatenation implementation.
QUOTE_HEAVY_QUERIES = [
    pytest.param("SELECT * FROM airline WHERE country = 'France'", id="single_quote"),
    pytest.param("SELECT * FROM airline WHERE name = 'O''Hare'", id="doubled_quote"),
    pytest.param("'; DROP SCOPE inventory; --", id="injection_payload"),
    pytest.param("SELECT * FROM a WHERE x = '\\' OR ''='", id="quote_breakout_attempt"),
]


@pytest.mark.parametrize("user_query", QUOTE_HEAVY_QUERIES)
def test_advisor_binds_user_query_as_named_parameter(user_query: str) -> None:
    """The user query must reach the SDK only as a bound parameter."""
    ctx = MagicMock()
    fake_results = [
        {"advisor_result": {"recommended_indexes": [{"index": "CREATE INDEX ..."}]}}
    ]

    with patch(
        "cb_mcp.tools.index.run_sql_plus_plus_query",
        return_value=fake_results,
    ) as mock_run:
        result = get_index_advisor_recommendations(
            ctx, "travel-sample", "inventory", user_query
        )

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args

    # The statement passes the user query via an ADVISOR($placeholder) bind; the
    # raw user query is NOT interpolated into it. This is the core of the
    # injection hardening — kept placeholder-name-agnostic so renaming the bind
    # variable doesn't require touching this assertion.
    statement = args[3]
    assert user_query not in statement

    # The user query is passed through as a bound named parameter only, and the
    # placeholder used in the statement must match that parameter's name.
    named_parameters = kwargs["named_parameters"]
    assert len(named_parameters) == 1
    ((param_name, param_value),) = named_parameters.items()
    assert param_value == user_query
    assert statement == f"SELECT ADVISOR(${param_name}) AS advisor_result"

    # The reserved SDK name 'query' collides with N1QLQuery's positional arg and
    # crashes against a live cluster — it must never be used as the bind name.
    assert param_name != "query"

    # Response shape is still assembled from the advisor result.
    assert "recommended_indexes" in result
