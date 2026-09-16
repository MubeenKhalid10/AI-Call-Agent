"""The fleet's numbers, from PostgreSQL into this process's gauges. Phase 22.

Queue depth, who is alive and how many calls the deployment placed in the
last hour are not facts any one process has; they are in the rows. Phase 21
wrote the reads (`worker_summary`, `queue_depth`) and this phase adds one
(`throughput`). This module turns their answers into gauges, so a scrape of
*any* process — the dashboard, the API, a worker — answers for the whole
deployment, and a Prometheus with one target still sees the queue.

Duck-typed on purpose: the store is whatever object has those three reads,
which is the real `CampaignStore` in production and the checks' in-memory
one. Nothing here imports the campaign package, so the bot — which never
reads these tables — can import the rest of `src/monitoring/` without
pulling them in.

`GaugeRefresher` is the loop the long-lived servers run: every
`MONITORING_REFRESH_SECS`, refresh, and say once when the reads fail rather
than once a period. The scheduler refreshes on its own report tick instead;
it already has the store open and a clock of its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from ..reliability.observability import event
from .instruments import (
    CAMPAIGNS_ACTIVE,
    COLLECTOR_REFRESHES,
    QUEUE_DEPTH,
    THROUGHPUT,
    THROUGHPUT_COST,
    THROUGHPUT_WINDOW,
    WORKERS,
    WORKERS_IN_FLIGHT,
)

DEFAULT_WINDOW_SECS = 3600.0

QUEUE_BUCKETS = ("due_now", "scheduled", "callbacks_due", "reserved", "live", "backlog")
WORKER_STATES = ("running", "draining", "stale", "stopped")
THROUGHPUT_KINDS = ("placed", "finished", "answered", "failed")


def set_queue_gauges(depth: Any) -> None:
    """`aiva_queue_depth{bucket}` and `aiva_campaigns_active` from a `QueueDepth`."""
    for bucket in QUEUE_BUCKETS:
        QUEUE_DEPTH.set(int(getattr(depth, bucket, 0) or 0), bucket=bucket)
    CAMPAIGNS_ACTIVE.set(int(getattr(depth, "active_campaigns", 0) or 0))


def set_worker_gauges(summary: Any) -> None:
    """`aiva_workers{state}` and `aiva_workers_in_flight` from a `WorkerSummary`."""
    for state in WORKER_STATES:
        WORKERS.set(int(getattr(summary, state, 0) or 0), state=state)
    WORKERS_IN_FLIGHT.set(int(getattr(summary, "in_flight", 0) or 0))


def set_throughput_gauges(throughput: Any) -> None:
    """`aiva_throughput_calls{kind}`, the window and the cost from a `Throughput`."""
    for kind in THROUGHPUT_KINDS:
        THROUGHPUT.set(int(getattr(throughput, kind, 0) or 0), kind=kind)
    THROUGHPUT_WINDOW.set(float(getattr(throughput, "window_secs", DEFAULT_WINDOW_SECS) or 0))
    cost = getattr(throughput, "cost_usd", None)
    THROUGHPUT_COST.set(float(cost) if cost is not None else 0.0)


async def refresh_from_store(
    store: Any,
    *,
    stale_secs: float,
    max_attempts: int = 3,
    window_secs: float = DEFAULT_WINDOW_SECS,
) -> bool:
    """Read the three aggregates and set every gauge. Returns whether all three reads worked.

    A store that predates a table (no `scheduler_workers`, no `throughput`)
    sets what it can and reports False; the caller decides how loudly to
    say so. Nothing raises past here: a gauge that is stale is better than
    a scheduler tick that died refreshing it.
    """
    complete = True
    try:
        set_queue_gauges(await store.queue_depth(max_attempts=max_attempts))
    except Exception as exc:  # noqa: BLE001 - reported, never raised into the caller's loop
        logger.debug(f"COLLECT | queue_depth failed: {exc}")
        complete = False
    try:
        set_worker_gauges(await store.worker_summary(stale_after_secs=stale_secs))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"COLLECT | worker_summary failed: {exc}")
        complete = False
    reader = getattr(store, "throughput", None)
    if reader is None:
        complete = False
    else:
        try:
            set_throughput_gauges(await reader(window_secs=window_secs))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"COLLECT | throughput failed: {exc}")
            complete = False
    COLLECTOR_REFRESHES.inc(outcome="ok" if complete else "partial")
    return complete


class GaugeRefresher:
    """Refreshes the fleet gauges on a timer, for a server process."""

    def __init__(
        self,
        store_getter: Callable[[], Any],
        *,
        interval_secs: float = 30.0,
        stale_secs: float = 60.0,
        max_attempts: int = 3,
        window_secs: float = DEFAULT_WINDOW_SECS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Create the refresher.

        Args:
            store_getter: Returns the process's store, or raises while it is
                still starting — the same callable the readiness probe uses.
            interval_secs: How often to refresh. Every read is a few
                aggregate queries; thirty seconds is plenty for a dashboard
                and cheap for the database.
            stale_secs: `WORKER_STALE_SECS`, for the worker summary.
            max_attempts: `CAMPAIGN_MAX_ATTEMPTS`, for the queue's eligibility.
            window_secs: The throughput window.
            sleep: How to wait; the checks inject one.
        """
        self._store_getter = store_getter
        self._interval = max(1.0, interval_secs)
        self._stale = stale_secs
        self._max_attempts = max_attempts
        self._window = window_secs
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task[Any] | None = None
        self._stopping = False
        self._warned = False
        self.refreshes = 0

    async def refresh_once(self) -> bool:
        """One refresh. Says once, at warning level, when the reads are failing."""
        try:
            store = self._store_getter()
        except Exception as exc:  # noqa: BLE001 - not open yet
            logger.debug(f"COLLECT | no store yet: {exc}")
            return False
        complete = await refresh_from_store(
            store, stale_secs=self._stale, max_attempts=self._max_attempts, window_secs=self._window
        )
        self.refreshes += 1
        if not complete and not self._warned:
            self._warned = True
            logger.warning(
                event(
                    "collect.partial",
                    outcome="some fleet gauges could not be read; run `campaign.py init` if the scheduler tables are missing",
                )
            )
        elif complete:
            self._warned = False
        return complete

    def start(self) -> None:
        """Begin refreshing in the background. Idempotent."""
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="gauge-refresher")

    async def stop(self) -> None:
        """Stop the loop and wait for it."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - it is going away
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.refresh_once()
            except Exception:  # noqa: BLE001 - the loop outlives any one bug
                logger.exception("COLLECT | refresh crashed")
            await self._sleep(self._interval)


__all__ = [
    "DEFAULT_WINDOW_SECS",
    "QUEUE_BUCKETS",
    "THROUGHPUT_KINDS",
    "WORKER_STATES",
    "GaugeRefresher",
    "refresh_from_store",
    "set_queue_gauges",
    "set_throughput_gauges",
    "set_worker_gauges",
]
