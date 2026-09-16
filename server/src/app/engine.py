"""The campaign execution engine: the scheduler, inside the application. Phase 25.

Starting a campaign marks it `ACTIVE`; something has to notice and dial.
Since Phase 13 that has been `campaign.py run`, a process of its own, and it
still is — but the unified application now runs the same loop inside
itself, so `uv run app.py` executes campaigns without a second command.
Nothing new is scheduled here: `CampaignEngine` builds the worker exactly as
the CLI does (`src/campaigns/runtime.build_worker`), runs it as one asyncio
task for the life of the application, reports on it, and stops it cleanly.

**What the loop already guarantees, and this file only hosts:** the queue is
the `campaign_prospects` rows (persistent, in PostgreSQL, since Phase 5); a
reservation runs under a deployment-wide advisory lock so no contact is
handed out twice (Phase 21); concurrency is `MAX_CONCURRENT_CALLS` across
the fleet and, from this phase, a campaign's own `max_concurrent_calls`; a
paused or stopped campaign stops being drawn from while its calls in
progress are followed to their end; a restart adopts the calls a dead
process was following and reconciles the ambiguous ones with the carrier
(Phases 9, 21); every outcome is written to the rows before the next
contact is reserved.

**What it refuses to do:** dial without a carrier. With no outbound carrier
configured (`TELEPHONY_PROVIDER`, its credentials, `TELEPHONY_FROM_NUMBER`,
`TELEPHONY_PUBLIC_URL`) the engine reports *idle* and the application runs
without it, exactly as before this phase.

**Stopping.** Uvicorn's shutdown runs the application's lifespan exit, which
calls `stop()`: no new call is placed from that moment, the calls in
progress get `WORKER_SHUTDOWN_SECS` to end (30 s by default; a call has a
ten-minute ceiling, so this is not "until they end"), and whatever is still
up is handed over — the attempt rows keep their status, the worker's
heartbeat stops, and the next engine to start (this one, restarted, or a
`campaign.py run` beside it) adopts them within `WORKER_ADOPT_SECS`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ..campaigns.runtime import Scheduler, build_worker
from ..config import Config, ConfigError
from ..reliability.observability import event
from ..telephony import TelephonyProvider, make_provider

StoreFactory = Callable[[], Awaitable[Any]]
ProviderFactory = Callable[[], TelephonyProvider]

# The engine's states, as `status()` reports them.
OFF = "off"            # WORKER_EMBEDDED=false or --no-engine: another process dials
IDLE = "idle"          # no outbound carrier configured; nothing can be dialled
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"  # no new calls; calls in progress finishing or being handed over
STOPPED = "stopped"
FAILED = "failed"      # the loop raised; the reason is in `status()["reason"]`


class CampaignEngine:
    """The scheduler as a component of the application. One per process."""

    def __init__(
        self,
        config: Config,
        *,
        store_factory: StoreFactory,
        provider_factory: ProviderFactory | None = None,
        enabled: bool | None = None,
        worker_id: str | None = None,
        shutdown_secs: float | None = None,
    ) -> None:
        """Create the engine; nothing runs until `start`.

        Args:
            config: The deployment's configuration.
            store_factory: Opens the engine's own store (its own pool: the
                worker's transactions must not queue behind page requests).
            provider_factory: Builds the carrier. `make_provider` over
                `config.telephony` by default; the checks and the audit hand
                in a stand-in that never rings a phone.
            enabled: Run at all. None takes `WORKER_EMBEDDED`.
            worker_id: This process's name in the fleet (None: `WORKER_ID`,
                or one made up by the worker).
            shutdown_secs: None takes `WORKER_SHUTDOWN_SECS`.
        """
        self._config = config
        self._store_factory = store_factory
        self._provider_factory = provider_factory
        self._enabled = config.worker.embedded if enabled is None else enabled
        self._worker_id = worker_id
        self._shutdown_secs = config.worker.shutdown_secs if shutdown_secs is None else shutdown_secs
        self._state = OFF if not self._enabled else STOPPED
        self._reason: str | None = None if self._enabled else "WORKER_EMBEDDED is off; `campaign.py run` places the calls"
        self._store: Any = None
        self._provider: TelephonyProvider | None = None
        self._scheduler: Scheduler | None = None
        self._task: asyncio.Task[Any] | None = None
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._metrics: dict[str, Any] | None = None

    # --- Lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        """Open the store, build the carrier and the worker, and run the loop as a task."""
        if not self._enabled or self._task is not None:
            return
        self._state = STARTING
        try:
            if self._provider_factory is None:
                self._config.telephony.require_outbound()
        except ConfigError as exc:
            self._state = IDLE
            self._reason = (str(exc).splitlines() or ["no outbound carrier"])[0]
            logger.warning(event("engine.idle", outcome="nothing will be dialled by this process", reason=self._reason))
            return
        try:
            self._store = await self._store_factory()
            self._provider = (
                self._provider_factory()
                if self._provider_factory is not None
                else make_provider(self._config.telephony, timeout_secs=self._config.reliability.carrier_timeout_secs)
            )
            self._scheduler = build_worker(self._config, self._store, self._provider, worker_id=self._worker_id)
        except Exception as exc:  # noqa: BLE001 - the application must still serve its pages
            self._state = FAILED
            self._reason = (str(exc).splitlines() or [type(exc).__name__])[0]
            logger.exception(event("engine.start_failed", outcome="the application serves without a scheduler", error=self._reason))
            await self._release()
            return
        self._task = asyncio.create_task(self._run(), name="campaign-engine")

    async def _run(self) -> None:
        assert self._scheduler is not None
        worker = self._scheduler.worker
        self._state = RUNNING
        self._started_at = datetime.now(UTC)
        self._reason = None
        logger.info(
            event(
                "engine.started",
                worker=worker.worker_id,
                limits=self._scheduler.guards.describe(),
                provider=getattr(self._provider, "name", "?"),
                outcome="dialling every ACTIVE campaign",
            )
        )
        try:
            metrics = await worker.run()
            self._metrics = metrics.snapshot()
            self._state = STOPPED
        except asyncio.CancelledError:
            self._state = STOPPED
            raise
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed silently
            self._state = FAILED
            self._reason = (str(exc).splitlines() or [type(exc).__name__])[0]
            logger.exception(event("engine.failed", error=self._reason, outcome="no more calls from this process"))
        finally:
            self._stopped_at = datetime.now(UTC)

    async def stop(self) -> None:
        """No new calls; give the calls in progress a bounded time; hand over the rest."""
        task, scheduler = self._task, self._scheduler
        if task is None or scheduler is None:
            await self._release()
            return
        worker = scheduler.worker
        if not task.done():
            self._state = STOPPING
            worker.request_stop()
            logger.info(
                event(
                    "engine.stopping",
                    in_flight=len(worker.in_flight),
                    wait_secs=self._shutdown_secs,
                    outcome="no new calls; waiting for the calls in progress",
                )
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_secs)
        if not task.done():
            # Still up after the wait: hand the calls over rather than keep
            # the process alive. The rows keep their status; the next worker
            # adopts them (Phase 21).
            worker.request_stop(immediate=True)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        await self._release()
        if self._state not in (FAILED, OFF, IDLE):
            self._state = STOPPED
        logger.info(event("engine.stopped", outcome="the scheduler task has ended"))

    async def _release(self) -> None:
        provider, store = self._provider, self._store
        self._provider, self._store, self._scheduler = None, None, None
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
        if store is not None:
            with contextlib.suppress(Exception):
                await store.close()

    # --- Reporting --------------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._state == RUNNING

    @property
    def worker(self) -> Any:
        """The live worker, for a check that wants to tick it by hand; None otherwise."""
        return self._scheduler.worker if self._scheduler is not None else None

    def status(self) -> dict[str, Any]:
        """What a page or `/readyz` needs to know, as JSON-ready values."""
        scheduler = self._scheduler
        worker = scheduler.worker if scheduler is not None else None
        in_flight = [
            {"attempt_id": a.id, "campaign_id": a.campaign_id, "prospect_id": a.prospect_id, "status": a.status.value}
            for a in (worker.in_flight if worker is not None else [])
        ]
        return {
            "state": self._state,
            "enabled": self._enabled,
            "reason": self._reason,
            "worker_id": worker.worker_id if worker is not None else None,
            "provider": getattr(self._provider, "name", None),
            "limits": scheduler.guards.describe() if scheduler is not None else None,
            "in_flight": in_flight,
            "stopping": bool(worker.stopping) if worker is not None else False,
            "started_at": self._started_at.isoformat(timespec="seconds") if self._started_at else None,
            "stopped_at": self._stopped_at.isoformat(timespec="seconds") if self._stopped_at else None,
            "metrics": worker.metrics.snapshot() if worker is not None else self._metrics,
            "shutdown_secs": self._shutdown_secs,
        }


__all__ = ["FAILED", "IDLE", "OFF", "RUNNING", "STARTING", "STOPPED", "STOPPING", "CampaignEngine"]
