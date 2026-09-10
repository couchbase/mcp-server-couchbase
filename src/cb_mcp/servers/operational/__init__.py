"""The operational Couchbase cluster server.

Deliberately inert: importing any submodule runs this file first, and this
server's tool modules import :mod:`.constants` for their logger namespace. If
this module imported the spec — which imports the tools — that would be a
cycle. Import the spec from :mod:`cb_mcp.servers.operational.spec` instead.
"""
