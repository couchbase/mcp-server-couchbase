"""SQL++ helpers specific to the Operational Insights server.

Only ``keyspace`` lives here now. The quoting primitives it is built from —
``safe_ident``, ``safe_field_path``, ``quote_literal`` — moved to
``cb_mcp.utils.sqlpp``: they encode SQL++ grammar rules that both servers
share, and ``safe_ident`` was duplicated verbatim between this module and
``tools/operational/query.py``. Import them from there directly rather than
through this module.
"""

from ..sqlpp import safe_ident


def keyspace(database_name: str, scope_name: str, collection_name: str) -> str:
    """Build a fully backtick-quoted three-part keyspace reference.

    Stays here rather than moving to the shared module because it is not the
    only shape a keyspace takes: the operational server's ``format_keyspace``
    builds an *unquoted* ``bucket.scope.collection`` for log messages. One
    name covering both would be safe for one caller and injectable for the
    other.

    Output must stay byte-identical to the historical
    ``f"{safe_ident(a)}.{safe_ident(b)}.{safe_ident(c)}"`` expression:
    callers (``create_index``) echo this value back to callers, and tests
    assert on the exact string.
    """
    return f"{safe_ident(database_name)}.{safe_ident(scope_name)}.{safe_ident(collection_name)}"
