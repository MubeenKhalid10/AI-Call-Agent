"""The scheduler: places campaign calls unattended. Phase 13.

Until this phase a campaign was a queue that a person emptied by hand, one
`campaign.py call` at a time. This module is the loop that was missing — and
almost nothing else, because Phases 5 to 12 built every part it needs:

* **who to call** — `service.next_call`, the queue's own reservation (one
  transaction, `FOR UPDATE SKIP LOCKED`, an idempotency key), which already
  applies the campaign's status, the prospect's do-not-call, the retry
  timing and the attempt limit in SQL;
* **whether to call now** — `CampaignGuards`: the calling window in the
  prospect's own timezone, the concurrency limit, pacing. Every refusal carries
  `retry_after_secs`, which is exactly what a loop needs to sleep instead of
  spin;
* **how to call** — `CampaignDialer.dial_next`, unchanged: re-check, stamp,
  ask the carrier once, hold an ambiguous answer as `UNRESOLVED`;
* **what happened** — `CampaignDialer.refresh`, which asks the carrier and
  writes the answer through `store.apply_call_event`, monotonic and idempotent,
  and `service.record_outcome`, which moves the membership on;
* **what a crash leaves behind** — `AttemptRecovery`, which resolves every
  attempt that was live when a process died, and never dials.

So the worker is a loop over those five verbs and a small amount of bookkeeping:
which calls it is following, when each campaign next has work, and counters for
the log. It holds no state the database does not, which is what makes a restart
safe — see below.

**It is a separate process from the bot, on purpose.** `bot.py` answers the
carrier's media stream and holds the conversation; its loop is
STT → LLM → TTS with a person waiting on every millisecond. Nothing here runs
inside that process: the worker places calls and polls the carrier from its own
`uv run campaign.py run`, and the two share nothing but the database rows and
the call id the carrier hands each of them. A slow carrier request, a recovery
pass or a long queue query cannot add a millisecond to a turn.

**What "reliable" means here, and how each part is met.**

| Requirement | Mechanism |
|---|---|
| Never two calls to one person | Unchanged from Phase 9: the reservation lock, the idempotency key, the live-attempt exclusion, the never-retried placement. The worker adds nothing and removes nothing |
| A crash loses no work and repeats none | The rows *are* the state. On start the worker runs recovery (resolving what an earlier run left live) and then *adopts* every attempt that still has a call in progress, following it to its end. A reservation that never dialled is released; a placement whose outcome is unknown stays blocked until the carrier says |
| Concurrency and pacing | `CampaignGuards` before every reservation, and `max_concurrent` counted again *inside* the reservation transaction (Phase 11). The worker follows at most that many calls and sleeps for the guard's `retry_after_secs` when refused |
| Callbacks at the promised time | Due `PENDING` callbacks are placed *before* the general queue through a targeted reservation (`reserve_membership`), because the queue orders never-called prospects first and a promise for ten o'clock must not wait behind them |
| A campaign ends by itself | When a campaign has nothing due, nothing scheduled, nothing live and no pending callback, its unusable memberships are closed and it is marked `COMPLETED` |
| Graceful shutdown | The first stop request stops *placing*; the calls already in progress are followed to their end (bounded by `drain_secs`). A second request stops now, and recovery picks up the rest on the next start |

**What it does not do.** No broker, no lock service, no webhook endpoint. One
process was the design point for Phase 13; the reservation was already safe
across processes, and Phase 21 built the rest.

**Several of these at once (Phase 21).** Every rule the loop used to keep in
memory now lives in PostgreSQL, so any number of `campaign.py run` processes
— on one machine or many — share one queue, one concurrency limit and one
pacing clock (`coordination.py` says how). What the worker itself gained:

* an **identity** (`worker_id`) written onto every attempt it reserves, so
  "the calls I am following" is a column and not a dict;
* a **heartbeat** every `heartbeat_secs`, with a status — `running`,
  `draining` once a stop is requested, `stopped` at the end — so a worker
  that died can be told from one that is busy: no beat for `stale_secs`
  means dead;
* an **adoption pass** every `adopt_secs` that claims the live attempts of
  dead workers (and of none) and releases the reservations they never
  placed. A live worker's attempts are never touched; an attempt another
  live worker claimed from *this* one is dropped from the follow set;
* a **hand-over** at shutdown: ownership is cleared from whatever is still
  in progress, so the next adoption pass anywhere picks it up at once
  instead of waiting for the row to go stale.

A database without the Phase 21 tables is served as before — one worker,
adopting everything live — with one warning naming `campaign.py init`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ..monitoring.collect import refresh_from_store
from ..monitoring.instruments import (
    CALL_OUTCOMES,
    CALLBACK_OPERATIONS,
    WORKER_HEARTBEATS,
    WORKER_IN_FLIGHT,
    WORKER_TICKS,
)
from ..reliability.guardrails import CampaignGuards
from ..reliability.observability import CallContext, call_context, event
from .coordination import (
    DEFAULT_ADOPT_SECS,
    DEFAULT_HEARTBEAT_SECS,
    DEFAULT_STALE_SECS,
    WORKER_DRAINING,
    WORKER_RUNNING,
    make_worker_id,
)
from .dialer import CampaignDialer, DialResult
from .models import (
    CallAttempt,
    CallAttemptStatus,
    CallbackStatus,
    Campaign,
    CampaignStatus,
    MembershipStatus,
    ScheduledCallback,
)
from .recovery import AttemptRecovery
from .service import CampaignService
from .store import CampaignStoreError, QueueOutlook

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]

#: The shortest the loop will sleep, so a guard that says "0.0s" cannot spin it.
_MIN_SLEEP_SECS = 0.05

#: A ceiling on reservations attempted in one tick, however many campaigns
#: there are. A tick that could place more than this is a tick that should be
#: two ticks; the bound is what stops a bug from emptying a queue in a loop.
_MAX_DIALS_PER_TICK = 25


@dataclass
class WorkerMetrics:
    """What the worker has done since it started. Phase 13.

    Counters, not a table: the rows in `call_attempts` are the durable record,
    and these exist so one `worker.metrics` log line says how a run is going.

    Attributes:
        queued: Reservations taken — an attempt row created for a call.
        started: Calls the carrier accepted. `queued - started` is the number
            refused between reserving and placing.
        completed: Calls followed to a final status that was not a failure —
            answered, no answer, busy, voicemail, or an outcome the
            conversation wrote. `outcomes` breaks it down.
        failed: Attempts that ended `FAILED` (refused by the carrier, released
            by a safety check, or closed by recovery) plus placements held as
            `UNRESOLVED`.
        skipped: Times the worker decided not to place a call it was asked
            for: the window was closed, the limit was reached, pacing, a
            campaign no longer active. `skips` says why.
        callbacks: Calls placed to keep a scheduled callback.
        recovered: Attempts a recovery pass resolved, released or closed.
        campaigns_completed: Campaigns this worker marked `COMPLETED`.
        ticks: Loop iterations.
        adopted: Phase 21. Live calls taken over from a worker that died,
            stopped, or never existed (a call placed by hand).
        released: Phase 21. Reservations of dead workers this one released
            back to the queue, never dialled.
        handed_over: Phase 21. Calls this worker stopped following because
            another live worker owns them now.
    """

    queued: int = 0
    started: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    callbacks: int = 0
    recovered: int = 0
    campaigns_completed: int = 0
    ticks: int = 0
    adopted: int = 0
    released: int = 0
    handed_over: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    skips: Counter[str] = field(default_factory=Counter)

    def note_skip(self, family: str) -> None:
        """Count one skip, under a short reason family."""
        self.skipped += 1
        self.skips[family] += 1

    def note_outcome(self, status: CallAttemptStatus) -> None:
        """Count one attempt reaching a final status."""
        self.outcomes[status.value] += 1
        if status is CallAttemptStatus.FAILED:
            self.failed += 1
        else:
            self.completed += 1

    def snapshot(self) -> dict[str, Any]:
        """The counters as plain data, for a log line or a JSON endpoint."""
        return {
            "queued": self.queued,
            "started": self.started,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "callbacks": self.callbacks,
            "recovered": self.recovered,
            "campaigns_completed": self.campaigns_completed,
            "ticks": self.ticks,
            "adopted": self.adopted,
            "released": self.released,
            "handed_over": self.handed_over,
            "outcomes": dict(sorted(self.outcomes.items())),
            "skips": dict(sorted(self.skips.items())),
        }

    def describe(self) -> str:
        """One line for the CLI."""
        parts = [
            f"{self.queued} queued",
            f"{self.started} started",
            f"{self.completed} completed",
            f"{self.failed} failed",
            f"{self.skipped} skipped",
        ]
        if self.callbacks:
            parts.append(f"{self.callbacks} callback(s)")
        if self.recovered:
            parts.append(f"{self.recovered} recovered")
        if self.adopted:
            parts.append(f"{self.adopted} adopted")
        if self.released:
            parts.append(f"{self.released} released")
        if self.handed_over:
            parts.append(f"{self.handed_over} handed over")
        if self.campaigns_completed:
            parts.append(f"{self.campaigns_completed} campaign(s) completed")
        if self.outcomes:
            parts.append(
                "outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(self.outcomes.items()))
            )
        return ", ".join(parts)


@dataclass
class TickReport:
    """What one pass of the loop did, and what it learned about when to run again.

    Attributes:
        placed: Calls placed this tick.
        finished: Calls that reached a final status this tick.
        retry_after_secs: The soonest a guard said to ask again, if one refused.
        next_due_at: The earliest moment a campaign has scheduled work, if any.
        due_now_blocked: Work is due but the queue handed out nothing — a
            prospect on a call in another campaign, or a live attempt holding
            the concurrency slot. Worth polling soon, not sleeping long.
        sleep_secs: What the worker will wait before the next tick.
    """

    placed: int = 0
    finished: int = 0
    retry_after_secs: float | None = None
    next_due_at: datetime | None = None
    due_now_blocked: bool = False
    sleep_secs: float = 0.0

    def note_retry(self, secs: float | None) -> None:
        """Remember the soonest retry hint."""
        if secs is None:
            return
        secs = max(0.0, secs)
        if self.retry_after_secs is None or secs < self.retry_after_secs:
            self.retry_after_secs = secs

    def note_due(self, at: datetime | None) -> None:
        """Remember the earliest scheduled moment."""
        if at is None:
            return
        if self.next_due_at is None or at < self.next_due_at:
            self.next_due_at = at


@dataclass
class _Tracked:
    """One call the worker is following to its end.

    Attributes:
        pushed: Phase 14. Whether the carrier has delivered at least one
            webhook event for this call. Once it has, the carrier is asked
            about the call only every `webhook_poll_secs` — the safety net —
            instead of every tick.
        last_polled_at: When the carrier was last asked about this call.
    """

    attempt: CallAttempt
    since: datetime
    last_status: CallAttemptStatus
    callback: bool = False
    adopted: bool = False
    pushed: bool = False
    last_polled_at: datetime | None = None


class CampaignWorker:
    """Places calls for active campaigns until told to stop. Phase 13.

    Construct it over the same `CampaignService`, `CampaignDialer` and
    `AttemptRecovery` that `campaign.py call` uses, then `await run()`. Every
    decision the worker makes is one of those objects' decisions; the worker
    only decides *when to ask*.
    """

    def __init__(
        self,
        service: CampaignService,
        dialer: CampaignDialer,
        *,
        recovery: AttemptRecovery | None = None,
        guards: CampaignGuards | None = None,
        max_concurrent: int | None = None,
        campaign_ids: list[int] | None = None,
        poll_secs: float = 2.0,
        idle_secs: float = 30.0,
        recovery_interval_secs: float = 300.0,
        recovery_min_age_secs: float = 120.0,
        drain_secs: float = 900.0,
        report_secs: float = 60.0,
        auto_complete: bool = True,
        callbacks_override_attempt_limit: bool = True,
        max_calls: int | None = None,
        once: bool = False,
        webhook_poll_secs: float = 0.0,
        worker_id: str | None = None,
        heartbeat_secs: float = DEFAULT_HEARTBEAT_SECS,
        stale_secs: float = DEFAULT_STALE_SECS,
        adopt_secs: float = DEFAULT_ADOPT_SECS,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        """Create the worker.

        Args:
            worker_id: Phase 21. This process's name in `scheduler_workers`
                and on every attempt it reserves. `None` makes one from the
                host name and pid; a configured name still gets a unique
                suffix, so two processes never share a heartbeat row.
            heartbeat_secs: Phase 21. How often the worker writes its beat.
            stale_secs: Phase 21. A worker whose last beat is older than this
                is dead: its live calls are adopted and its reservations
                released. Must exceed a few heartbeats, or a busy worker on a
                slow database is declared dead while it is talking.
            adopt_secs: Phase 21. How often the adoption pass runs.
            service: Campaign rules and persistence.
            dialer: The dialer, already holding the carrier and the guards.
            recovery: The recovery pass to run at start and periodically.
                `None` skips both — only for checks that inject their own.
            guards: The same guards the dialer holds, so the worker knows the
                concurrency limit and can sleep for a refusal's `retry_after`.
            max_concurrent: The concurrency limit when `guards` is not given.
                Defaults to one.
            campaign_ids: Serve only these campaigns. `None` serves every
                `ACTIVE` campaign, including ones activated after the worker
                started.
            poll_secs: How often a call in progress is checked with the
                carrier, and the shortest the loop sleeps when it has work.
            idle_secs: The longest the loop sleeps. It wakes at least this
                often to notice a campaign that was activated, a callback that
                fell due, or a pause.
            recovery_interval_secs: How often the recovery pass runs while the
                worker is up, for attempts it is not following itself.
            recovery_min_age_secs: How stale an attempt must be before recovery
                touches it. The same setting `campaign.py recover` uses.
            drain_secs: After a stop request, how long to keep following the
                calls in progress before giving up on them. They are left
                live, and the next start's recovery pass resolves them.
            report_secs: How often the `worker.metrics` line is logged.
            auto_complete: Mark a campaign `COMPLETED` when it has nothing left
                it could ever dial.
            callbacks_override_attempt_limit: Place a scheduled callback even
                when its membership has used every attempt. The default,
                because a callback is the prospect's own request; see the
                handoff for the decision.
            max_calls: Stop after placing this many calls (and following them
                to their end). `None` runs until stopped.
            once: Run the start-up recovery and exactly one tick, then return
                without following anything. For a cron-style invocation.
            webhook_poll_secs: Phase 14. Once the carrier has pushed an event
                for a call, how often the carrier is still asked about it.
                The webhook is then the source of the call's status and the
                poll is the fallback under it; the tick still runs every
                `poll_secs`, reading the row, so a pushed final status is
                noticed as fast as a polled one. 0 polls every tick as before,
                which is also what happens until the first event arrives —
                so a receiver that is down costs nothing but the old rate.
            clock: Where "now" comes from. Injected by the checks.
            sleep: How to wait. Injected by the checks; the default is an
                `asyncio` wait that a stop request interrupts.
        """
        self._service = service
        self._dialer = dialer
        self._recovery = recovery
        self._guards = guards
        self._max_concurrent = max(
            1, guards.max_concurrent if guards is not None else (max_concurrent or 1)
        )
        self._campaign_ids = set(campaign_ids) if campaign_ids else None
        self._poll_secs = max(_MIN_SLEEP_SECS, poll_secs)
        self._idle_secs = max(self._poll_secs, idle_secs)
        self._recovery_interval = max(0.0, recovery_interval_secs)
        self._recovery_min_age = max(0.0, recovery_min_age_secs)
        self._drain_secs = max(0.0, drain_secs)
        self._report_secs = max(1.0, report_secs)
        self._auto_complete = auto_complete
        self._override_limit = callbacks_override_attempt_limit
        self._max_calls = max_calls
        self._once = once
        self._webhook_poll_secs = max(0.0, webhook_poll_secs)
        self._ledger_missing = False
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep

        # Phase 21: identity, heartbeat and adoption timing.
        self.worker_id = make_worker_id(worker_id)
        self._heartbeat_secs = max(1.0, heartbeat_secs)
        self._stale_secs = max(self._heartbeat_secs * 2, stale_secs)
        self._adopt_secs = max(1.0, adopt_secs)
        self._next_heartbeat_at: datetime | None = None
        self._next_adopt_at: datetime | None = None
        self._coordination_missing = False
        self._registered = False

        self.metrics = WorkerMetrics()
        self._in_flight: dict[int, _Tracked] = {}
        self._cursor = 0
        self._stopping = False
        self._force_stop = False
        self._stop_requested_at: datetime | None = None
        self._stop_seen = False
        self._wake = asyncio.Event()
        self._next_recovery_at: datetime | None = None
        self._next_report_at: datetime | None = None
        self._warned_callbacks: set[int] = set()
        self._noted_empty: set[int] = set()
        self._nothing_to_serve = False

    # --- Public surface -----------------------------------------------------

    @property
    def in_flight(self) -> list[CallAttempt]:
        """The calls the worker is following right now."""
        return [tracked.attempt for tracked in self._in_flight.values()]

    @property
    def stopping(self) -> bool:
        """Whether a stop has been requested."""
        return self._stopping

    def now(self) -> datetime:
        """The current moment, from the injected clock."""
        return self._clock()

    def request_stop(self, *, immediate: bool = False) -> None:
        """Ask the worker to stop.

        The first request stops it *placing* calls; the ones in progress are
        followed to their end, up to `drain_secs`. `immediate=True` — or a
        second request — returns from `run` as soon as the current tick ends,
        leaving live attempts for recovery. Safe to call from a signal handler
        via `loop.call_soon_threadsafe`.
        """
        if self._stopping or immediate:
            self._force_stop = True
        if not self._stopping:
            self._stopping = True
            self._stop_requested_at = self.now()
            self._next_heartbeat_at = None  # Say `draining` on the next tick, not the next beat.
            logger.warning(
                event(
                    "worker.stopping",
                    outcome="no new calls will be placed",
                    in_flight=len(self._in_flight),
                    drain_secs=self._drain_secs,
                )
            )
        elif self._force_stop:
            logger.warning(
                event(
                    "worker.stopping_now",
                    outcome="leaving calls in progress for recovery",
                    in_flight=len(self._in_flight),
                )
            )
        self._wake.set()

    async def run(self) -> WorkerMetrics:
        """Start, loop until stopped, and finish. Never raises for a bad tick.

        Returns:
            The counters, for the CLI's closing line.
        """
        await self.start()
        try:
            while True:
                try:
                    report = await self.tick()
                except Exception:  # noqa: BLE001 - the loop outlives any one bug
                    logger.exception(event("worker.tick_failed", outcome="sleeping, then retrying"))
                    report = TickReport(sleep_secs=self._idle_secs)
                if self._done(report):
                    break
                await self._pause(report.sleep_secs)
        finally:
            await self.finish()
        return self.metrics

    async def start(self) -> None:
        """The start-up pass: recover what an earlier run left, adopt what is still live."""
        limits = self._guards.describe() if self._guards is not None else "no guards (checks only)"
        logger.info(
            event(
                "worker.started",
                campaigns="all active" if self._campaign_ids is None else sorted(self._campaign_ids),
                limits=limits,
                poll_secs=self._poll_secs,
                idle_secs=self._idle_secs,
                max_calls=self._max_calls,
            )
        )
        now = self.now()
        self._next_report_at = now + timedelta(seconds=self._report_secs)
        await self._register()
        await self._recover("startup")
        await self._adopt_live_attempts()

    async def tick(self) -> TickReport:
        """One pass: follow calls in progress, then place what is due.

        Returns:
            What happened, and how long to sleep before the next pass.
        """
        self.metrics.ticks += 1
        WORKER_TICKS.inc()
        report = TickReport()

        await self._follow_in_flight(report)
        WORKER_IN_FLIGHT.set(len(self._in_flight))

        now = self.now()
        if self._next_heartbeat_at is None or now >= self._next_heartbeat_at:
            await self._heartbeat()
        if not self._stopping and self._next_recovery_at is not None and now >= self._next_recovery_at:
            await self._recover("periodic")
            await self._adopt_live_attempts()
        elif not self._stopping and self._next_adopt_at is not None and now >= self._next_adopt_at:
            await self._adopt_live_attempts()

        if not self._stopping and not self._reached_max_calls():
            await self._place_callbacks(report)
            await self._place_queued(report)
        elif not self._stopping:
            # The call cap is reached: nothing more is placed, but a campaign
            # whose last call just ended should still be closed.
            await self._assess_all(report)

        if self._next_report_at is not None and now >= self._next_report_at:
            self._log_metrics()
            await self.refresh_fleet_gauges()
            self._next_report_at = now + timedelta(seconds=self._report_secs)

        report.sleep_secs = self._sleep_for(report)
        return report

    async def finish(self) -> None:
        """The closing line. Calls still in progress are handed over, not abandoned silently.

        Phase 21: ownership is cleared from every attempt still being
        followed, so the next adoption pass on any live worker picks them up
        at once instead of after `stale_secs`; then the heartbeat row says
        `stopped`. Without another worker, `campaign.py recover` (or the
        next start) resolves them as before.
        """
        if self._in_flight:
            handed = await self._hand_over()
            logger.warning(
                event(
                    "worker.left_live",
                    outcome=(
                        "another running worker will adopt them; otherwise run "
                        "`campaign.py recover` once they have ended"
                        if handed
                        else "run `campaign.py recover` once they have ended"
                    ),
                    attempts=sorted(self._in_flight),
                )
            )
        await self._deregister()
        self._log_metrics()
        logger.info(event("worker.stopped", outcome=self.metrics.describe()))

    # --- The fleet (Phase 21) ------------------------------------------------

    async def _register(self) -> None:
        """Write this worker's row. A database without the table is served the old way."""
        try:
            await self._service.store.register_worker(
                self.worker_id,
                hostname=socket.gethostname(),
                pid=os.getpid(),
                campaign_ids=sorted(self._campaign_ids) if self._campaign_ids is not None else None,
            )
        except CampaignStoreError as exc:
            self._note_coordination_error("worker.register_failed", exc)
            return
        self._registered = True
        now = self.now()
        self._next_heartbeat_at = now + timedelta(seconds=self._heartbeat_secs)
        self._next_adopt_at = now + timedelta(seconds=self._adopt_secs)
        logger.info(
            event(
                "worker.registered",
                worker=self.worker_id,
                heartbeat_secs=self._heartbeat_secs,
                stale_secs=self._stale_secs,
                adopt_secs=self._adopt_secs,
            )
        )

    async def _heartbeat(self) -> None:
        """Say that this worker is alive, what it is doing and how it is doing."""
        self._next_heartbeat_at = self.now() + timedelta(seconds=self._heartbeat_secs)
        if not self._registered:
            return
        status = WORKER_DRAINING if self._stopping else WORKER_RUNNING
        try:
            await self._service.store.heartbeat_worker(
                self.worker_id,
                status=status,
                in_flight=len(self._in_flight),
                metrics=self.metrics.snapshot(),
            )
        except CampaignStoreError as exc:
            # The database went away, or the row was pruned. Keep going: the
            # next beat is the retry, and stale detection is the safety net.
            WORKER_HEARTBEATS.inc(outcome="failed")
            logger.warning(event("worker.heartbeat_failed", error=str(exc)))
            return
        WORKER_HEARTBEATS.inc(outcome="ok")

    async def _hand_over(self) -> bool:
        """Clear ownership of every followed attempt, so a live worker adopts it now."""
        if not self._registered:
            return False
        handed = False
        for attempt_id in list(self._in_flight):
            try:
                if await self._service.store.set_attempt_worker(attempt_id, None):
                    handed = True
            except CampaignStoreError as exc:
                logger.warning(event("worker.handover_failed", error=str(exc)))
                return handed
        return handed

    async def _deregister(self) -> None:
        """Mark the heartbeat row stopped. Silent when there is no row."""
        if not self._registered:
            return
        try:
            await self._service.store.mark_worker_stopped(self.worker_id, metrics=self.metrics.snapshot())
        except CampaignStoreError as exc:
            logger.warning(event("worker.deregister_failed", error=str(exc)))

    def _note_coordination_error(self, name: str, exc: CampaignStoreError) -> None:
        """A missing Phase 21 table is said once; anything else is a warning each time."""
        if "does not exist" in str(exc):
            if not self._coordination_missing:
                self._coordination_missing = True
                logger.warning(
                    event(
                        "worker.coordination_unavailable",
                        error=(str(exc).splitlines() or [type(exc).__name__])[0],
                        outcome="serving as a single worker; run `campaign.py init`",
                    )
                )
            return
        logger.warning(event(name, error=str(exc)))

    # --- Following calls ----------------------------------------------------

    async def _follow_in_flight(self, report: TickReport) -> None:
        """Ask about every call being followed, and write down what the carrier says."""
        for attempt_id in list(self._in_flight):
            tracked = self._in_flight[attempt_id]
            with call_context(self._context(tracked.attempt)):
                try:
                    await self._follow_one(attempt_id, tracked, report)
                except CampaignStoreError as exc:
                    # Keep following: the row is still there, the database is
                    # what went away. The next tick asks again.
                    logger.warning(event("worker.follow_failed", error=str(exc)))
                    report.note_retry(self._poll_secs)

    async def _follow_one(self, attempt_id: int, tracked: _Tracked, report: TickReport) -> None:
        """Bring one followed attempt up to date. The bot may have finished it first."""
        current = await self._service.store.get_attempt(attempt_id)
        if current is None:
            logger.warning(event("worker.attempt_vanished", outcome="no longer following"))
            del self._in_flight[attempt_id]
            return
        if current.status.is_final:
            self._finish_attempt(tracked, current, report)
            return
        if (
            self._registered
            and current.worker_id is not None
            and current.worker_id != self.worker_id
        ):
            # Phase 21: another worker claimed it — which only happens when
            # this one was taken for dead (a beat that did not land in time).
            # It is theirs now; following it too would count it twice.
            del self._in_flight[attempt_id]
            self.metrics.handed_over += 1
            logger.warning(
                event(
                    "worker.handed_over",
                    call=current.telephony_call_id,
                    outcome=f"now owned by worker {current.worker_id}; no longer following",
                )
            )
            return

        if not current.telephony_call_id:
            # Nothing to ask the carrier about: a placement that never
            # reported. Recovery owns it, once it is old enough.
            if current.status is not tracked.last_status:
                self._note_transition(tracked, current)
            return

        if not await self._poll_due(tracked, current):
            # Phase 14: the carrier is pushing this call's events and was
            # asked recently. The row is what the webhook wrote; nothing to
            # fetch. A transition the receiver wrote is still logged here.
            if current.status is not tracked.last_status:
                self._note_transition(tracked, current)
            return

        tracked.last_polled_at = self.now()
        refreshed = await self._dialer.refresh(current)
        latest = refreshed or current
        if latest.status is not tracked.last_status:
            self._note_transition(tracked, latest)
        if latest.status.is_final:
            self._finish_attempt(tracked, latest, report)

    async def _poll_due(self, tracked: _Tracked, current: CallAttempt) -> bool:
        """Whether to ask the carrier about this call on this tick. Phase 14.

        Always, until the carrier has pushed an event for the call: a
        receiver that is unreachable, unmounted or refusing must cost nothing
        but the old request rate. Once one has arrived, only when the last
        poll is `webhook_poll_secs` old — the safety net under the webhooks,
        for an event the carrier never sent.
        """
        if self._webhook_poll_secs <= 0 or self._ledger_missing:
            return True
        if not tracked.pushed:
            pushed_at = await self._last_webhook_at(current.telephony_call_id or "")
            if pushed_at is None:
                return True
            tracked.pushed = True
            logger.info(
                event(
                    "call.pushed",
                    call=current.telephony_call_id,
                    outcome=f"the carrier is delivering events; polling every "
                    f"{self._webhook_poll_secs:g}s as a fallback",
                )
            )
        if tracked.last_polled_at is None:
            return True
        return (self.now() - tracked.last_polled_at).total_seconds() >= self._webhook_poll_secs

    async def _last_webhook_at(self, call_id: str) -> datetime | None:
        """When the carrier last pushed an event for a call, or None. Never raises."""
        try:
            return await self._service.store.last_webhook_at(call_id)
        except CampaignStoreError as exc:
            if "does not exist" in str(exc):
                # A database that predates the ledger. Said once; polling
                # carries on at the full rate for the rest of the run.
                if not self._ledger_missing:
                    self._ledger_missing = True
                    logger.warning(
                        event(
                            "worker.webhooks_unavailable",
                            error=(str(exc).splitlines() or [type(exc).__name__])[0],
                            outcome="polling every tick; run `campaign.py init`",
                        )
                    )
                return None
            logger.warning(event("worker.webhook_check_failed", error=str(exc)))
            return None

    def _note_transition(self, tracked: _Tracked, latest: CallAttempt) -> None:
        """Log a status change on a followed call, and remember it."""
        logger.info(
            event(
                "call.status",
                call=latest.telephony_call_id,
                outcome=f"{tracked.last_status.value} -> {latest.status.value}",
                elapsed_secs=round((self.now() - tracked.since).total_seconds(), 1),
            )
        )
        tracked.last_status = latest.status
        tracked.attempt = latest

    def _finish_attempt(self, tracked: _Tracked, final: CallAttempt, report: TickReport) -> None:
        """Stop following a call that has ended, and count its outcome."""
        del self._in_flight[final.id]
        self.metrics.note_outcome(final.status)
        CALL_OUTCOMES.inc(campaign=final.campaign_id, status=final.status.value)
        WORKER_IN_FLIGHT.set(len(self._in_flight))
        report.finished += 1
        name = "call.failed" if final.status is CallAttemptStatus.FAILED else "call.completed"
        logger.info(
            event(
                name,
                call=final.telephony_call_id,
                outcome=final.status.value,
                duration_secs=final.duration_seconds,
                elapsed_secs=round((self.now() - tracked.since).total_seconds(), 1),
                error=final.failure_reason if final.status is CallAttemptStatus.FAILED else None,
                callback=tracked.callback or None,
                adopted=tracked.adopted or None,
                pushed=tracked.pushed or None,
            )
        )

    def _track(self, attempt: CallAttempt, *, callback: bool = False, adopted: bool = False) -> None:
        """Start following an attempt. Following the same one twice is a no-op."""
        if attempt.id in self._in_flight:
            return
        self._in_flight[attempt.id] = _Tracked(
            attempt=attempt,
            since=self.now(),
            last_status=attempt.status,
            callback=callback,
            adopted=adopted,
        )

    async def _adopt_live_attempts(self) -> None:
        """Follow every live call whose worker is dead, stopped, or was never a worker.

        After a restart the calls an earlier worker placed may still be up —
        the bot is a separate process and is still talking. Recovery leaves
        those alone (the carrier says they are live); adopting them is what
        gets their outcome written when they end, instead of waiting for the
        next recovery pass to notice.

        Phase 21: with several workers up, "nobody watching" is a question
        for the database, not a local dict. The store claims — under one
        lock, so two live workers never both claim the same call — every
        live attempt owned by a stale or stopped worker, or by no one, and
        stamps this worker on it. Reservations a dead worker took and never
        placed go back to the queue. A live worker's attempts are not
        touched. Without the Phase 21 tables the old rule holds: adopt
        everything live, because there is nobody else.
        """
        self._next_adopt_at = self.now() + timedelta(seconds=self._adopt_secs)
        if self._registered:
            try:
                claimed = await self._service.store.claim_abandoned_attempts(
                    self.worker_id, stale_after_secs=self._stale_secs, limit=200
                )
                released = await self._service.store.release_abandoned_reservations(
                    stale_after_secs=self._stale_secs, limit=200
                )
            except CampaignStoreError as exc:
                self._note_coordination_error("worker.adopt_failed", exc)
                return
            if released:
                self.metrics.released += released
                logger.info(
                    event(
                        "worker.released",
                        outcome=f"{released} reservation(s) of dead workers returned to the queue",
                    )
                )
            for attempt in claimed:
                self._adopt(attempt, "a call of a worker that stopped or died; following it to its end")
            return

        try:
            live = await self._service.store.list_live_attempts(older_than_secs=0.0, limit=200)
        except CampaignStoreError as exc:
            logger.warning(event("worker.adopt_failed", error=str(exc)))
            return
        for attempt in live:
            if attempt.telephony_call_id:
                self._adopt(attempt, "a call placed before this worker started; following it to its end")

    def _adopt(self, attempt: CallAttempt, why: str) -> None:
        """Start following a call somebody else placed. Following it already is a no-op."""
        if attempt.id in self._in_flight:
            return
        with call_context(self._context(attempt)):
            logger.info(
                event(
                    "worker.adopted",
                    call=attempt.telephony_call_id,
                    outcome=attempt.status.value,
                    error=why,
                )
            )
        self.metrics.adopted += 1
        self._track(attempt, adopted=True)

    # --- Placing calls ------------------------------------------------------

    def _capacity(self) -> int:
        """How many more calls this worker may follow at once."""
        return max(0, self._max_concurrent - len(self._in_flight))

    def _reached_max_calls(self) -> bool:
        return self._max_calls is not None and self.metrics.started >= self._max_calls

    async def _place_callbacks(self, report: TickReport) -> None:
        """Place every callback that has fallen due, ahead of the queue."""
        if not self._capacity():
            return
        now = self.now()
        try:
            due = await self._service.store.list_callbacks(
                status=CallbackStatus.PENDING, due_before=now, limit=50
            )
        except CampaignStoreError as exc:
            logger.warning(event("worker.callbacks_unavailable", error=str(exc)))
            return

        for callback in due:
            if not self._capacity() or self._stopping or self._reached_max_calls():
                return
            if self._campaign_ids is not None and callback.campaign_id not in self._campaign_ids:
                continue
            stop = await self._place_callback(callback, report)
            if stop:
                return

    async def _place_callback(self, callback: ScheduledCallback, report: TickReport) -> bool:
        """Place one due callback. Returns True when the tick should stop placing."""
        context = CallContext(
            campaign_id=callback.campaign_id,
            prospect_id=callback.prospect_id,
            extra={"callback": callback.id},
        )
        with call_context(context):
            if callback.campaign_id is None or callback.campaign_prospect_id is None:
                self._warn_callback(
                    callback,
                    "it has no campaign membership to dial through; place it by hand",
                )
                return False

            campaign = await self._service.store.get_campaign(callback.campaign_id)
            if campaign is None or not campaign.status.is_dialable:
                self._warn_callback(
                    callback,
                    f"its campaign is {campaign.status.value if campaign else 'gone'}, not ACTIVE",
                )
                self.metrics.note_skip("campaign_inactive")
                return False

            prospect = await self._service.store.get_prospect(callback.prospect_id)
            if prospect is None:
                return False
            if not prospect.is_callable:
                await self._cancel_callback(
                    callback, "the prospect is do-not-call or has no usable number"
                )
                return False

            membership = await self._service.store.get_membership(callback.campaign_prospect_id)
            if membership is None:
                self._warn_callback(callback, "its membership no longer exists")
                return False
            if membership.status is MembershipStatus.IN_PROGRESS:
                return False  # Already on a call; the callback is fulfilled by it.
            if membership.status is MembershipStatus.SKIPPED:
                await self._cancel_callback(callback, "the membership was skipped")
                return False

            # The membership was closed when the call that scheduled the
            # callback ended (we reached them), or a retry was pushed past the
            # promised time. Either way the promise is the schedule now.
            needs_reopen = membership.status.is_closed or (
                membership.next_attempt_at is not None
                and membership.next_attempt_at > callback.scheduled_for
            )
            if needs_reopen:
                await self._service.store.reopen_membership(
                    membership.id, next_attempt_at=callback.scheduled_for
                )
                logger.info(
                    event(
                        "callback.reopened",
                        outcome=f"membership {membership.id} queued for the callback",
                        scheduled_for=callback.scheduled_for.isoformat(timespec="minutes"),
                    )
                )

            logger.info(
                event(
                    "callback.due",
                    scheduled_for=callback.scheduled_for.isoformat(timespec="minutes"),
                    outcome="placing",
                )
            )
            result = await self._dialer.dial_membership(
                membership.id, ignore_attempt_limit=self._override_limit, worker_id=self.worker_id
            )
            self._handle_dial_result(result, report, callback=callback)
            if result.queued is None:
                if not result.blocked:
                    # Not eligible this instant — on a call in another
                    # campaign, say. It stays pending; next tick asks again.
                    self._warn_callback(callback, "not eligible right now; will try again")
                CALLBACK_OPERATIONS.inc(operation="place", outcome="blocked" if result.blocked else "not_eligible")
                return bool(result.blocked)
            if result.placed or result.deferred:
                CALLBACK_OPERATIONS.inc(operation="place", outcome="placed" if result.placed else "deferred")
                return False
            CALLBACK_OPERATIONS.inc(operation="place", outcome="unresolved" if result.ambiguous else "failed")
            # One honest try per callback. A carrier refusal would repeat
            # every tick and write a failed attempt each time; an ambiguous
            # placement may have rung the phone, and a second attempt to keep
            # the promise would be the duplicate call this system exists to
            # prevent. Either way the callback is withdrawn with the reason
            # in the log, and the attempt row says what happened.
            reason = (
                "the placement never reported an outcome; the attempt is held as UNRESOLVED"
                if result.ambiguous
                else f"the call could not be placed: {result.error}"
            )
            await self._cancel_callback(callback, reason)
            return bool(result.ambiguous)

    def _warn_callback(self, callback: ScheduledCallback, reason: str) -> None:
        """Say once why a due callback is not being placed."""
        if callback.id in self._warned_callbacks:
            return
        self._warned_callbacks.add(callback.id)
        CALLBACK_OPERATIONS.inc(operation="place", outcome="unserviceable")
        logger.warning(event("callback.unserviceable", error=reason, outcome="left pending"))

    async def _cancel_callback(self, callback: ScheduledCallback, reason: str) -> None:
        """Withdraw a callback that cannot be kept, saying why."""
        try:
            await self._service.store.set_callbacks_status(
                callback.prospect_id, CallbackStatus.CANCELLED
            )
        except CampaignStoreError as exc:
            CALLBACK_OPERATIONS.inc(operation="cancel", outcome="failed")
            logger.warning(event("callback.cancel_failed", error=str(exc)))
            return
        CALLBACK_OPERATIONS.inc(operation="cancel", outcome="cancelled")
        logger.warning(event("callback.cancelled", error=reason))

    async def _place_queued(self, report: TickReport) -> None:
        """Draw work from every active campaign in turn, while there is capacity."""
        campaigns = await self._active_campaigns()
        if not campaigns:
            return

        exhausted: set[int] = set()
        dials = 0
        while (
            self._capacity()
            and not self._stopping
            and not self._reached_max_calls()
            and dials < _MAX_DIALS_PER_TICK
        ):
            remaining = [c for c in campaigns if c.id not in exhausted]
            if not remaining:
                break
            campaign = remaining[self._cursor % len(remaining)]
            self._cursor += 1
            dials += 1

            with call_context(campaign_id=campaign.id):
                try:
                    result = await self._dialer.dial_next(campaign.id, worker_id=self.worker_id)
                except CampaignStoreError as exc:
                    logger.warning(event("worker.dial_failed", error=str(exc)))
                    report.note_retry(self._poll_secs * 5)
                    return

                if result.blocked and result.queued is None:
                    self._handle_dial_result(result, report)
                    return  # The guards are global: nothing else will pass either.

                if result.queued is None:
                    exhausted.add(campaign.id)
                    await self._assess(campaign, report)
                    continue

                self._handle_dial_result(result, report)
                if result.ambiguous:
                    return  # The slot is held; let recovery resolve it before more.
                if result.deferred and _skip_family(result.refusal) == "pacing":
                    # Phase 21: the pacing slot is shared across the fleet.
                    # Nothing else will pass until it frees; asking again this
                    # tick would only push every due prospect back by the wait.
                    return

    async def _assess_all(self, report: TickReport) -> None:
        """Ask every served campaign what it has left, placing nothing."""
        for campaign in await self._active_campaigns():
            with call_context(campaign_id=campaign.id):
                await self._assess(campaign, report)

    async def _active_campaigns(self) -> list[Campaign]:
        """The campaigns to draw from, newest first as the store lists them.

        Serving named campaigns and finding none of them active is how a run
        like `campaign.py run "Q1"` learns it is finished; `_done` reads
        `_nothing_to_serve`. Serving every active campaign, an empty list
        means "wait for one to be started", so the flag is left alone.
        """
        try:
            campaigns = await self._service.store.list_campaigns(status=CampaignStatus.ACTIVE, limit=100)
        except CampaignStoreError as exc:
            logger.warning(event("worker.campaigns_unavailable", error=str(exc)))
            return []
        if self._campaign_ids is not None:
            campaigns = [c for c in campaigns if c.id in self._campaign_ids]
            self._nothing_to_serve = not campaigns
        return campaigns

    def _handle_dial_result(
        self, result: DialResult, report: TickReport, *, callback: ScheduledCallback | None = None
    ) -> None:
        """Count and log what one dial did, and start following it if it placed a call."""
        if result.blocked and result.queued is None:
            self.metrics.note_skip(_skip_family(result.refusal))
            report.note_retry(result.blocked_by.retry_after_secs if result.blocked_by else None)
            logger.info(event("call.skipped", error=result.refusal))
            return
        if result.queued is None:
            return

        self.metrics.queued += 1
        queued = result.queued
        with call_context(self._context(queued.attempt)):
            if result.ambiguous:
                self.metrics.failed += 1
                self.metrics.outcomes[CallAttemptStatus.UNRESOLVED.value] += 1
                # Ask the carrier as soon as the row is old enough to be
                # touched, instead of waiting for the next periodic pass.
                self._schedule_recovery(self._recovery_min_age + 1.0)
                logger.error(
                    event(
                        "call.unresolved",
                        error=result.error,
                        outcome="held; recovery will ask the carrier what exists",
                    )
                )
                return
            if result.deferred:
                self.metrics.note_skip(_skip_family(result.refusal))
                report.note_retry(result.blocked_by.retry_after_secs if result.blocked_by else None)
                logger.info(event("call.deferred", error=result.error))
                return
            if not result.placed:
                self.metrics.failed += 1
                self.metrics.outcomes[CallAttemptStatus.FAILED.value] += 1
                self.metrics.note_skip("not_callable" if result.blocked_by is None else _skip_family(result.refusal))
                logger.warning(event("call.failed", error=result.error, outcome="released"))
                return

            self.metrics.started += 1
            report.placed += 1
            if callback is not None:
                self.metrics.callbacks += 1
            attempt = result.attempt or queued.attempt
            self._track(attempt, callback=callback is not None)
            logger.info(
                event(
                    "call.started",
                    call=attempt.telephony_call_id,
                    outcome=attempt.status.value,
                    callback=callback.id if callback else None,
                    in_flight=len(self._in_flight),
                )
                + f" | {queued.prospect.full_name}"
            )

    async def _assess(self, campaign: Campaign, report: TickReport) -> None:
        """The queue handed out nothing for a campaign. Learn why, and what to wait for."""
        try:
            outlook = await self._service.store.queue_outlook(
                campaign.id, max_attempts=self._service.max_attempts
            )
        except CampaignStoreError as exc:
            logger.warning(event("worker.outlook_failed", error=str(exc)))
            report.note_retry(self._poll_secs * 5)
            return

        report.note_due(outlook.next_due_at)
        report.note_due(outlook.next_callback_at)
        if outlook.due_now:
            report.due_now_blocked = True

        if outlook.total == 0:
            if campaign.id not in self._noted_empty:
                self._noted_empty.add(campaign.id)
                logger.info(
                    event("campaign.empty", outcome="active but has no prospects; waiting")
                )
            return
        self._noted_empty.discard(campaign.id)

        if outlook.has_live_work or not self._auto_complete:
            return
        await self._complete(campaign, outlook)

    async def _complete(self, campaign: Campaign, outlook: QueueOutlook) -> None:
        """Close what can never be dialled, then mark the campaign finished."""
        skipped = exhausted = 0
        if outlook.undialable:
            skipped, exhausted = await self._service.store.sweep_memberships(
                campaign.id, max_attempts=self._service.max_attempts
            )
        # Re-read before writing: a pause, or a fresh import, may have landed
        # between the queue saying "nothing" and now.
        fresh = await self._service.store.get_campaign(campaign.id)
        if fresh is None or not fresh.status.is_dialable:
            return
        after = await self._service.store.queue_outlook(
            campaign.id, max_attempts=self._service.max_attempts
        )
        if not after.is_finished or after.pending:
            return
        counts = await self._service.store.campaign_counts(campaign.id)
        await self._service.set_status(campaign.id, CampaignStatus.COMPLETED)
        self.metrics.campaigns_completed += 1
        logger.info(
            event(
                "campaign.completed",
                outcome=f"{counts.completed} reached, {counts.exhausted} exhausted, "
                f"{counts.skipped} skipped of {counts.total}",
                swept=f"{skipped} skipped, {exhausted} exhausted" if skipped or exhausted else None,
            )
            + f" | {campaign.name!r}"
        )

    # --- Recovery -----------------------------------------------------------

    async def _recover(self, reason: str) -> None:
        """Run the recovery pass, and schedule the next one."""
        self._next_recovery_at = (
            self.now() + timedelta(seconds=self._recovery_interval)
            if self._recovery_interval > 0
            else None
        )
        if self._recovery is None:
            return
        report = await self._recovery.run()
        resolved = report.resolved + report.released + report.failed
        self.metrics.recovered += resolved
        if report.total:
            logger.info(event("worker.recovery", outcome=report.describe(), reason=reason))

    def _schedule_recovery(self, in_secs: float) -> None:
        """Bring the next recovery pass forward."""
        at = self.now() + timedelta(seconds=max(0.0, in_secs))
        if self._next_recovery_at is None or at < self._next_recovery_at:
            self._next_recovery_at = at

    # --- The loop's timing --------------------------------------------------

    def _sleep_for(self, report: TickReport) -> float:
        """How long to wait before the next tick, from what this one learned."""
        wait = self._idle_secs
        now = self.now()
        if self._in_flight or report.placed or self._stopping:
            wait = min(wait, self._poll_secs)
        if report.due_now_blocked:
            wait = min(wait, self._poll_secs)
        if report.retry_after_secs is not None:
            wait = min(wait, report.retry_after_secs)
        if report.next_due_at is not None:
            wait = min(wait, (report.next_due_at - now).total_seconds())
        if self._next_recovery_at is not None:
            wait = min(wait, (self._next_recovery_at - now).total_seconds())
        if self._registered:
            # Phase 21: a beat that lands late is a worker declared dead.
            if self._next_heartbeat_at is not None:
                wait = min(wait, (self._next_heartbeat_at - now).total_seconds())
            if self._next_adopt_at is not None and not self._stopping:
                wait = min(wait, (self._next_adopt_at - now).total_seconds())
        return max(_MIN_SLEEP_SECS, wait)

    def _done(self, report: TickReport) -> bool:
        """Whether `run` should return after this tick."""
        if self._force_stop:
            return True
        if self._stopping:
            if not self._in_flight:
                return True
            if self._stop_requested_at is not None and self._drain_secs >= 0:
                waited = (self.now() - self._stop_requested_at).total_seconds()
                if waited >= self._drain_secs:
                    logger.warning(
                        event(
                            "worker.drain_timeout",
                            outcome="stopping with calls in progress",
                            in_flight=len(self._in_flight),
                            drain_secs=self._drain_secs,
                        )
                    )
                    return True
            return False
        if self._once:
            return True
        if self._reached_max_calls() and not self._in_flight:
            return True
        if self._nothing_to_serve and not self._in_flight:
            logger.info(
                event("worker.nothing_to_serve", outcome="every named campaign is finished or paused")
            )
            return True
        return False

    async def _pause(self, secs: float) -> None:
        """Wait, unless a stop request has arrived that the loop has not yet acted on."""
        if self._stopping and not self._stop_seen:
            self._stop_seen = True
            return
        if self._sleep is not None:
            await self._sleep(secs)
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=secs)
        except TimeoutError:
            pass

    def _log_metrics(self) -> None:
        logger.info(event("worker.metrics", in_flight=len(self._in_flight), **self.metrics.snapshot()))

    async def refresh_fleet_gauges(self) -> bool:
        """Read queue depth, worker health and throughput into this process's gauges. Phase 22.

        On the report tick, so `/metrics` on a worker answers for the whole
        deployment at the same cadence the `worker.metrics` line is written.
        Never raises; a store without the tables leaves the gauges as they were.
        """
        return await refresh_from_store(
            self._service.store,
            stale_secs=self._stale_secs,
            max_attempts=getattr(self._service, "max_attempts", 3) or 3,
        )

    def _context(self, attempt: CallAttempt) -> CallContext:
        return CallContext(
            campaign_id=attempt.campaign_id,
            prospect_id=attempt.prospect_id,
            attempt_id=attempt.id,
            call_id=attempt.telephony_call_id,
            provider=attempt.telephony_provider,
            # Phase 22: the correlation id from the row, and this process's name.
            trace_id=attempt.trace_id,
            extra={"worker": self.worker_id},
        )


def _skip_family(reason: str) -> str:
    """A guard's sentence, as a short key for the skip counter."""
    lowered = reason.lower()
    if "calling hours" in lowered:
        return "window"
    if "concurrency" in lowered:
        return "concurrency"
    if lowered.startswith("pacing"):
        return "pacing"
    if "cannot count live calls" in lowered:
        return "database"
    return "other"


def install_signal_handlers(worker: CampaignWorker) -> None:
    """Make Ctrl+C (and SIGTERM where it exists) a graceful stop.

    Once stops placing calls and drains; twice stops now. Uses the event loop's
    own handlers where the platform supports them and falls back to
    `signal.signal` on Windows, where the loop does not.
    """
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        worker.request_stop()

    installed = False
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _stop)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            continue
    if installed:
        return

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        loop.call_soon_threadsafe(_stop)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            continue


__all__ = ["CampaignWorker", "TickReport", "WorkerMetrics", "install_signal_handlers"]
