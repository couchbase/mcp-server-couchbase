"""Concurrency limit for tool execution within one server process.

FastMCP runs synchronous tool functions on AnyIO's shared thread pool, whose
``CapacityLimiter`` therefore sets how many tool calls can be *executing* at
once in a process. Requests beyond that limit are still accepted and still hold
an asyncio task; they simply wait for a slot. AnyIO's default is 40.

What raising the limit does and does not buy is worth stating plainly, because
the two are easy to confuse:

* It **does** raise the ceiling on in-flight tool calls. That matters when calls
  spend their time blocked on the cluster rather than on CPU — a high-latency
  link, or the long timeouts in the ``wan_development`` config profile — and when
  slow tools would otherwise occupy every slot and delay fast ones behind them.
* It **does not** raise CPU-bound throughput. One process executes Python
  bytecode on about one core regardless of thread count, so for CPU-bound calls
  a larger pool only moves the queue from the limiter to the GIL. Benchmarking
  this server's read-heavy mix against a local cluster at 40 versus 160 threads
  moved throughput 3.2% while worsening the latency tail, which is what that
  relocation looks like. Use ``--workers`` to add CPU capacity.
"""

import logging

from anyio.to_thread import current_default_thread_limiter

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.concurrency")


def apply_thread_pool_limit(size: int | None) -> int:
    """Set the per-process cap on concurrent tool calls; return what took effect.

    ``size`` of ``None`` leaves the runtime's own default in place rather than
    restating it here, so an unset option is provably a no-op and the default
    tracks AnyIO rather than a number copied into this repo.

    Must be called from inside the running event loop: the limiter is stored in
    a run-scoped variable, so there is nothing to read or set before the loop
    exists, and each worker process applies it to its own pool.

    Returns the effective limit — the value just set, or the runtime default
    when ``size`` is ``None`` — so callers can report the real number instead of
    the configured one.
    """
    limiter = current_default_thread_limiter()
    if size is not None:
        limiter.total_tokens = size
    effective = int(limiter.total_tokens)
    logger.debug("Tool-call concurrency limit: %d (configured: %s)", effective, size)
    return effective
