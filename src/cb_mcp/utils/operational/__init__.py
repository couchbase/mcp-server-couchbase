"""Helpers specific to the operational Couchbase server.

These sit apart from the shared helpers in :mod:`cb_mcp.utils` because they
speak the operational ``couchbase`` SDK or its REST endpoints. Keeping them out
of the shared namespace is what lets another server reuse the shared half
without importing an SDK it does not use.
"""
