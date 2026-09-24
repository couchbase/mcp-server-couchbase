"""Helpers specific to the Operational Insights server.

These sit apart from the shared helpers in :mod:`cb_mcp.utils` because they
speak the ``couchbase_operational_insights`` SDK. Keeping them out of the
shared namespace is what lets the operational server keep working without
importing an SDK it does not use.

Importing :mod:`.sdk_logging` here, first, is deliberate: Python always runs
a package's ``__init__.py`` in full before running any of its submodules, so
this import guarantees ``sdk_logging`` takes its "handlers on the stdlib root
before the SDK is touched" snapshot before any sibling submodule (which does
import the SDK) gets a chance to run. See that module's docstring.
"""

from . import sdk_logging as _sdk_logging  # noqa: F401
