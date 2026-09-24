"""Couchbase Server protocol constants used by this server's helpers.

These are facts about the *product* — which TCP port a service answers REST
calls on — rather than anything an operator configures or another server
could reuse. They lived in ``cb_mcp.utils.constants`` when there was only
one server and "shared" and "Couchbase" meant the same thing; with a second
server they are just noise in a module every server imports.

Kept here rather than in ``servers/operational/constants.py`` so the
dependency only ever points ``tools/operational`` and ``utils/operational``
at ``utils/operational`` — never from a helper package back up into a
``servers/`` package.
"""

#: Couchbase Server REST API ports. TLS/plaintext port pairs differ per
#: service — these are used to build request URLs once TLS-vs-plaintext is
#: decided from the connection string's scheme.
MANAGEMENT_REST_PORT_TLS = 18091
MANAGEMENT_REST_PORT_PLAIN = 8091
INDEX_REST_PORT_TLS = 19102
INDEX_REST_PORT_PLAIN = 9102
