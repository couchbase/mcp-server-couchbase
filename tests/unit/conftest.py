"""Shared fixtures for the unit suite.

The audit subsystem keeps three pieces of process-global state, and a test that
installs any of them without restoring it changes the result of a *later,
unrelated* test. That is the worst kind of suite failure: the output names a
file that is not the one at fault, and the failure moves when tests are
reordered or run in isolation.

The three:

* the ``SIGTERM`` disposition, which :func:`cb_mcp.audit.emitter.init_audit`
  replaces and ``shutdown_audit`` does not restore;
* ``emitter._previous_sigterm``, the handler the audit handler chains to, which
  is what makes a server under uvicorn hand over instead of closing the trail;
* ``emitter._active``, the installed logger, which ``get_audit_logger`` returns
  to every caller including tools asserting ``audit["active"] is False``.

This module snapshots all three before each test and puts them back after, so
no test can depend on, or damage, another's leftovers.
"""

from __future__ import annotations

import contextlib
import signal

import pytest

from cb_mcp.audit import emitter


@pytest.fixture(autouse=True)
def _restore_audit_global_state():
    """Snapshot and restore the audit subsystem's process-global state.

    Autouse on purpose. Opting in per test is what produced the leak: the tests
    that install this state are not the ones that notice it, so the fixture has
    to cover the tests that would otherwise be the victims.
    """
    previous_disposition = signal.getsignal(signal.SIGTERM)
    previous_chain = emitter._previous_sigterm
    previous_active = emitter._active

    yield

    emitter._active = previous_active
    emitter._previous_sigterm = previous_chain
    # Only settable from the main thread of the main interpreter, and a handler
    # read back as None cannot be reinstalled. Neither is worth failing a
    # passing test over.
    with contextlib.suppress(TypeError, ValueError, OSError):
        signal.signal(signal.SIGTERM, previous_disposition)
