"""Unit tests for index management tools.

Mocks the cluster's execute_query() so these tests can verify CREATE INDEX
statement construction and the write-tool error envelope without a live
Enterprise Analytics cluster.

Identifiers are backtick-quoted, but input *validation* is still deliberately
absent from index.py, so there are no tests here for malformed input -- bad
input either raises out of the formatter into the error envelope, or is
forwarded to EA to reject.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ea_mcp.tools.index import create_index, safe_field_path


class TestSafeFieldPath:
    def test_quotes_single_segment(self) -> None:
        assert safe_field_path("title") == "`title`"

    def test_quotes_each_segment_of_a_dotted_path(self) -> None:
        """Dots stay separators; quoting the whole string would be a different field."""
        assert safe_field_path("ratings.Lyrics") == "`ratings`.`Lyrics`"

    def test_doubles_embedded_backticks(self) -> None:
        assert safe_field_path("weird`name") == "`weird``name`"

    def test_escapes_a_breakout_attempt(self) -> None:
        """A field path can no longer close its identifier and alter the statement."""
        assert safe_field_path("a`) EXCLUDE UNKNOWN KEY --") == (
            "`a``) EXCLUDE UNKNOWN KEY --`"
        )


def _make_ctx_with_cluster() -> tuple[SimpleNamespace, MagicMock]:
    """Build a Context plus its underlying cluster mock."""
    cluster = MagicMock()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(cluster=cluster)
        )
    )
    return ctx, cluster


def _create(ctx, cluster, **overrides):
    """Call create_index with sensible defaults, patching the connection."""
    kwargs = {
        "database_name": "music",
        "scope_name": "myPlaylist",
        "collection_name": "countrySongs",
        "index_name": "song_title_idx",
        "fields": [{"name": "title", "type": "string"}],
    }
    kwargs.update(overrides)
    with patch("ea_mcp.tools.index.get_cluster_connection", return_value=cluster):
        return create_index(ctx, **kwargs)


class TestCreateIndex:
    def test_builds_expected_statement(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster)

        statement = cluster.execute_query.call_args[0][0]
        assert statement == (
            "CREATE INDEX `song_title_idx` "
            "ON `music`.`myPlaylist`.`countrySongs` (`title`: string);"
        )
        assert result["success"] is True
        assert result["index_name"] == "song_title_idx"
        assert result["keyspace"] == "`music`.`myPlaylist`.`countrySongs`"

    def test_composite_index_joins_fields(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(
            ctx,
            cluster,
            fields=[
                {"name": "artist", "type": "string"},
                {"name": "ratings.Lyrics", "type": "bigint"},
            ],
        )

        statement = cluster.execute_query.call_args[0][0]
        assert "(`artist`: string, `ratings`.`Lyrics`: bigint)" in statement

    def test_omitted_type_emits_bare_field(self) -> None:
        """A field with no "type" is legal for standard indexes.

        Verified against a live cluster:
        CREATE INDEX name_idx3 ON `travel-sample`.inventory.airline (iata)
        succeeds, despite the published EBNF implying a type is mandatory.
        """
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster, fields=[{"name": "iata"}])

        assert cluster.execute_query.call_args[0][0].endswith("(`iata`);")
        assert result["success"] is True

    def test_mixes_typed_and_untyped_fields(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(
            ctx,
            cluster,
            fields=[{"name": "artist"}, {"name": "release_date", "type": "date"}],
        )

        statement = cluster.execute_query.call_args[0][0]
        assert "(`artist`, `release_date`: date)" in statement

    def test_if_not_exists_clause(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(ctx, cluster, if_not_exists=True)

        statement = cluster.execute_query.call_args[0][0]
        assert statement.startswith("CREATE INDEX `song_title_idx` IF NOT EXISTS ON ")

    def test_exclude_unknown_key_clause(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(ctx, cluster, exclude_unknown_key=True)

        statement = cluster.execute_query.call_args[0][0]
        assert statement.endswith("(`title`: string) EXCLUDE UNKNOWN KEY;")

    def test_escapes_backticks_in_keyspace_and_index_name(self) -> None:
        """A backtick in a name must not be able to close the identifier early."""
        ctx, cluster = _make_ctx_with_cluster()

        _create(ctx, cluster, database_name="db`.`evil", index_name="idx`x")

        statement = cluster.execute_query.call_args[0][0]
        assert "`db``.``evil`.`myPlaylist`.`countrySongs`" in statement
        assert "CREATE INDEX `idx``x`" in statement

    def test_quotes_names_needing_escaping(self) -> None:
        """Hyphenated names like travel-sample only work because they are quoted."""
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(
            ctx,
            cluster,
            database_name="travel-sample",
            scope_name="inventory",
            collection_name="airline",
        )

        assert result["keyspace"] == "`travel-sample`.`inventory`.`airline`"

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("index already exists")

        result = _create(ctx, cluster)

        assert result["success"] is False
        assert "index already exists" in result["error"]
        assert result["index_name"] == "song_title_idx"

    def test_forwards_unrecognised_type_to_server(self) -> None:
        """An unknown type is a SQL++ syntax error, so EA reports it, not us."""
        ctx, cluster = _make_ctx_with_cluster()

        _create(ctx, cluster, fields=[{"name": "title", "type": "float"}])

        assert "`title`: float" in cluster.execute_query.call_args[0][0]


class TestCastDefault:
    """The optional CAST (DEFAULT NULL ...) clause, used for TAV indexes."""

    def test_bare_cast_default_null(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster, cast_default_null=True)

        assert result["statement"].endswith("(`title`: string) CAST (DEFAULT NULL);")

    def test_matches_the_documented_example(self) -> None:
        """Docs: CREATE INDEX idx2 ON staff(hiredate:DATE) CAST(DEFAULT NULL DATE "MM/DD/YYYY")"""
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(
            ctx,
            cluster,
            index_name="idx2",
            fields=[{"name": "hiredate", "type": "date"}],
            cast_formats={"date": "MM/DD/YYYY"},
        )

        assert result["statement"].endswith(
            '(`hiredate`: date) CAST (DEFAULT NULL DATE "MM/DD/YYYY");'
        )

    def test_formats_imply_the_clause(self) -> None:
        """cast_formats alone is enough -- no need to also set cast_default_null."""
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster, cast_formats={"date": "MM/DD/YYYY"})

        assert "CAST (DEFAULT NULL DATE" in result["statement"]

    def test_emits_formats_in_grammar_order(self) -> None:
        """DateTimeFormatSpec fixes the order as DATE, TIME, DATETIME."""
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(
            ctx,
            cluster,
            cast_formats={
                "datetime": "YYYY-MM-DD hh:mm:ss",
                "time": "hh:mm",
                "date": "MM/DD/YYYY",
            },
        )

        assert result["statement"].endswith(
            'CAST (DEFAULT NULL DATE "MM/DD/YYYY" TIME "hh:mm" '
            'DATETIME "YYYY-MM-DD hh:mm:ss");'
        )

    def test_escapes_quotes_in_a_format_string(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster, cast_formats={"date": 'MM"DD'})

        assert r'DATE "MM\"DD"' in result["statement"]

    def test_escapes_a_backslash_before_a_quote(self) -> None:
        """A backslash must not be able to disarm the quote escaping.

        Escaping quotes without also escaping backslashes would let the
        caller's trailing backslash pair with the one we add, leaving their
        quote free to close the literal early.
        """
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster, cast_formats={"date": 'a\\"b'})

        assert r'DATE "a\\\"b"' in result["statement"]

    def test_cast_follows_exclude_unknown_key(self) -> None:
        """Clause order is fixed by the grammar: IndexUnknown, then IndexCastDefault.

        EA rejects this particular combination ("CAST modifier is only allowed
        for B-Tree indexes") for array indexes, but the ordering still has to
        be right for the plain-index case.
        """
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(ctx, cluster, exclude_unknown_key=True, cast_default_null=True)

        assert result["statement"].endswith("EXCLUDE UNKNOWN KEY CAST (DEFAULT NULL);")


class TestCreateArrayIndex:
    """Array (UNNEST) index elements, verified against a live cluster."""

    def test_array_of_primitives(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(
            ctx,
            cluster,
            fields=[{"unnest": "public_likes", "type": "string"}],
            exclude_unknown_key=True,
        )

        statement = cluster.execute_query.call_args[0][0]
        assert statement.endswith(
            "(UNNEST `public_likes`: string) EXCLUDE UNKNOWN KEY;"
        )

    def test_array_of_objects_with_select(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(
            ctx,
            cluster,
            fields=[
                {
                    "unnest": "reviews",
                    "select": [
                        {"name": "ratings.Lyrics", "type": "bigint"},
                        {"name": "ratings.Instrumentals", "type": "bigint"},
                    ],
                }
            ],
        )

        statement = cluster.execute_query.call_args[0][0]
        assert (
            "(UNNEST `reviews` SELECT `ratings`.`Lyrics`: bigint, "
            "`ratings`.`Instrumentals`: bigint)" in statement
        )

    def test_mixed_plain_and_array_elements(self) -> None:
        """The docs' composite example: a bare field plus an array element."""
        ctx, cluster = _make_ctx_with_cluster()

        _create(
            ctx,
            cluster,
            fields=[
                {"name": "artist"},
                {
                    "unnest": "reviews",
                    "select": [{"name": "ratings.Lyrics", "type": "bigint"}],
                },
            ],
        )

        statement = cluster.execute_query.call_args[0][0]
        assert (
            "(`artist`, UNNEST `reviews` SELECT `ratings`.`Lyrics`: bigint)"
            in statement
        )

    def test_nested_unnest_paths(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        _create(ctx, cluster, fields=[{"unnest": ["a", "b"], "type": "string"}])

        assert (
            "(UNNEST `a` UNNEST `b`: string)" in cluster.execute_query.call_args[0][0]
        )

    def test_does_not_force_exclude_unknown_key(self) -> None:
        """An array index without the clause is forwarded, not silently fixed.

        EA rejects it itself with a clear "Array indexes must specify EXCLUDE
        UNKNOWN KEY." (verified in
        ea_scripts/08_array_exclude_unknown_key_test.py), so the tool forwards
        the caller's statement rather than rewriting it.
        """
        ctx, cluster = _make_ctx_with_cluster()

        result = _create(
            ctx,
            cluster,
            fields=[{"unnest": "likes", "type": "string"}],
            exclude_unknown_key=False,
        )

        assert "UNKNOWN KEY" not in result["statement"]
