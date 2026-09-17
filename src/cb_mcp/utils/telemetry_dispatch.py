"""Deliver usage-telemetry events without starting a thread per tool call.

``reo_census.ReoEventLogger.log_event`` starts a new daemon thread for every
event, and each of those threads opens its own connection before posting a
single event. That is fine for the occasional install ping it was designed
for. A served MCP endpoint fires one event per tool call, so the thread and
connection churn scales with the request rate and competes with the request
path for the same CPU.

This module keeps the events and changes only how they are delivered:

* callers enqueue onto a bounded queue, and never block and never spawn;
* a small number of long-lived sender threads drain it;
* a circuit breaker stops hammering an endpoint that is failing;
* when the queue is full the oldest event is dropped, so a slow endpoint
  costs telemetry fidelity rather than server throughput or memory.

Each event is still handed to ``reo-census`` itself, one event per request,
with ``blocking=True`` so the send happens on the sender thread instead of a
new one. The wire format, the endpoint resolution, the opt-out check and the
payload validation are therefore unchanged: this module decides *when and on
which thread* an event is sent, never *what* is sent or *where*.

Two behaviours differ from the shipped path and are deliberate:

* **Events can be dropped.** A full queue drops the oldest event, and while
  the circuit breaker is open events are discarded rather than held. The
  shipped path instead retried every event, at the cost of an ever-growing
  number of live threads. Drops are counted in ``stats`` and the first one is
  logged.
* **Delivery is asynchronous.** ``log_event`` used to return once the event
  was handed to its own thread; ``submit`` returns once it is queued. Anything
  that needs to observe delivery must call ``flush`` first.

How many senders a deployment needs: the rule
---------------------------------------------

A sender delivers one event, waits for the response, then takes the next. So
sustainable delivery is **senders divided by the per-event round trip**, and an
event that takes longer than ``senders / event_rate`` to deliver means the
queue fills and events are dropped.

The per-event round trip is not one network round trip. Because each event
opens its own connection, it costs a TCP handshake, a TLS handshake and then
the request, which is roughly three and a half network round trips to an HTTPS
collector. A collector a few milliseconds away therefore needs very few
senders; one tens of milliseconds away needs an order of magnitude more.

**The default of 2 is sized for a collector on the same host or the same rack.
It is not enough for a distant one**, and the symptom is quiet: throughput
looks healthy precisely because events are being dropped rather than sent.
``CB_MCP_TELEMETRY_SENDERS`` raises it, ``CB_MCP_TELEMETRY_SAMPLE`` lowers what
has to be delivered, and ``get_server_configuration_status`` reports what is
actually getting through. Choose the value against the real endpoint.

A larger queue does **not** fix this. The queue absorbs bursts; it cannot
change the steady-state rate, so raising it only delays the first drop.

Configuration (all optional):

===============================  =========================================
``CB_MCP_TELEMETRY_MODE``        ``dispatch`` (default) or ``legacy`` to
                                 restore the thread-per-call behaviour
``CB_MCP_TELEMETRY_SENDERS``     sender threads, default 2. See the rule above
``CB_MCP_TELEMETRY_QUEUE``       max queued events, default 10000. Burst
                                 absorption only
``CB_MCP_TELEMETRY_SAMPLE``      fraction of tool-call events kept, default 1.0
===============================  =========================================

Two things would remove the per-event cost rather than resize around it, and
both belong in ``reo-census`` where every product using it would benefit:
reusing one connection per sender instead of opening one per event, and
coalescing several events into a single request. The second needs the collector
to accept an array of events.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.telemetry_dispatch")

DEFAULT_QUEUE_MAX = 10_000
DEFAULT_SENDERS = 2
DEFAULT_SAMPLE = 1.0
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 60.0
# How long a sender waits for work before looping to re-check the stop flag.
POLL_INTERVAL_S = 0.25
# How long shutdown waits for the queue to drain before giving up on it.
SHUTDOWN_FLUSH_S = 2.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def dispatch_enabled() -> bool:
    """False restores reo-census's own thread-per-call delivery."""
    return os.environ.get("CB_MCP_TELEMETRY_MODE", "dispatch").lower() != "legacy"


class EventDispatcher:
    """Queue telemetry events and deliver them from long-lived senders.

    ``send_one`` is called on a sender thread and must return truthy when the
    event was delivered. It is expected to be a blocking ``log_event``.
    """

    def __init__(
        self,
        send_one: Callable[[dict[str, Any]], bool],
        *,
        queue_max: int | None = None,
        senders: int | None = None,
        sample: float | None = None,
    ) -> None:
        self._send_one = send_one
        if sample is None:
            sample = _env_float("CB_MCP_TELEMETRY_SAMPLE", DEFAULT_SAMPLE)
        self._sample = sample
        if queue_max is None:
            queue_max = _env_int("CB_MCP_TELEMETRY_QUEUE", DEFAULT_QUEUE_MAX)
        if senders is None:
            senders = _env_int("CB_MCP_TELEMETRY_SENDERS", DEFAULT_SENDERS)

        # queue.Queue treats maxsize <= 0 as unbounded, which would defeat the
        # point of the bound, so a nonsensical setting falls back to the cap.
        self._queue: queue.Queue = queue.Queue(
            maxsize=queue_max if queue_max > 0 else DEFAULT_QUEUE_MAX
        )
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        self._warned_dropping = False
        # Approximate: incremented without a lock from the request threads and
        # the senders, so a concurrent update can be lost. Good enough to tell
        # "nothing is being delivered" from "everything is", which is what
        # these are for; do not treat them as an audit trail.
        self.stats = {
            "enqueued": 0,
            "dropped_queue_full": 0,
            "sampled_out": 0,
            "delivered": 0,
            "failed": 0,
        }
        self._threads = [
            threading.Thread(
                target=self._run, name=f"cb-mcp-telemetry-{i}", daemon=True
            )
            for i in range(max(1, senders))
        ]
        for t in self._threads:
            t.start()
        atexit.register(self._at_exit)

    # ---- producer side: called on the request path, must stay cheap --------
    def submit(self, event: dict[str, Any]) -> bool:
        """Queue an event. Returns False when it was sampled out or dropped."""
        if self._sample < 1.0 and random.random() >= self._sample:  # noqa: S311
            self.stats["sampled_out"] += 1
            return False
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Prefer the newest data: drop the oldest queued event instead of
            # blocking the caller or growing without bound.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass
            self.stats["dropped_queue_full"] += 1
            self._warn_dropping()
            return False
        self.stats["enqueued"] += 1
        return True

    def _warn_dropping(self) -> None:
        """Say once that telemetry is being lost, so it is not lost silently."""
        if self._warned_dropping:
            return
        with self._lock:
            if self._warned_dropping:
                return
            self._warned_dropping = True
        logger.warning(
            "Telemetry queue is full; events are being dropped. Events are "
            "arriving faster than %d sender(s) can deliver them to the "
            "collector. Raise CB_MCP_TELEMETRY_SENDERS, or lower "
            "CB_MCP_TELEMETRY_SAMPLE to send fewer. Raising "
            "CB_MCP_TELEMETRY_QUEUE will not help: the queue absorbs bursts "
            "and cannot change the steady-state rate. Tool calls are "
            "unaffected; get_server_configuration_status reports the counters.",
            len(self._threads),
        )

    # ---- consumer side ----------------------------------------------------
    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                event = self._queue.get(timeout=POLL_INTERVAL_S)
            except queue.Empty:
                continue
            if time.monotonic() < self._breaker_until:
                # Endpoint is failing: discard rather than queue up work that
                # would only fail again and delay the events behind it.
                self.stats["failed"] += 1
                continue
            self._deliver(event)

    def _deliver(self, event: dict[str, Any]) -> None:
        try:
            ok = bool(self._send_one(event))
        except Exception:  # telemetry must never raise into the server
            logger.debug("telemetry send failed", exc_info=True)
            ok = False
        if ok:
            self.stats["delivered"] += 1
            with self._lock:
                self._consecutive_failures = 0
            return
        self.stats["failed"] += 1
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures < BREAKER_FAILURES:
                return
            self._breaker_until = time.monotonic() + BREAKER_COOLDOWN_S
        logger.debug(
            "telemetry endpoint failing; dropping events for %ss", BREAKER_COOLDOWN_S
        )

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until the queue drains, for tests and for shutdown.

        Returns True if everything queued reached a sender before the timeout.
        Delivery itself stays best-effort: a sender may still fail the send.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                # Give a sender a moment to finish the event it holds.
                time.sleep(0.01)
                if self._queue.empty():
                    return True
            time.sleep(0.005)
        return self._queue.empty()

    def _at_exit(self) -> None:
        """Give queued events a short chance to go out on a clean shutdown.

        The senders are daemon threads, so without this anything still queued
        when the process ends is simply lost.
        """
        self.flush(SHUTDOWN_FLUSH_S)
        self.close()

    def close(self) -> None:
        """Stop the senders. Idempotent; queued events are not delivered."""
        self._stopping.set()
        atexit.unregister(self._at_exit)
