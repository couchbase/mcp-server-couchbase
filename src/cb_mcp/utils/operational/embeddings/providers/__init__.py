"""Concrete embedding-provider implementations.

One module per provider so each request/response quirk (auth header shape,
body shape, response-vector extraction) stays a small, independently
reviewable diff. Imported lazily by ``registry.resolve_embedding_provider`` —
nothing here is imported at package-import time.
"""
