"""Phase 21 checks: several workers over one queue.

What is checked, and how:

* **In memory, deterministically** — `test_worker.py`'s world (a fake clock,
  a scripted carrier, the real `CampaignWorker`, `CampaignDialer` and
  `CampaignService`) with the Phase 21 coordination methods added to its
  `MemoryStore`. Two or three workers are ticked by hand over the same rows,
  so the interleavings that a fleet produces by accident are produced here on
  purpose: both reserving at once, one dying mid-call, one stopping cleanly,
  both delivered the same webhook.
* **Against PostgreSQL** — the SQL that makes the guarantees real: the advisory
  lock around the reservation (six concurrent reservations, a limit of two,
  exactly two succeed), the shared pacing slot, the heartbeat table, the
  claim and release of a dead worker's work, and five concurrent deliveries
  of one webhook applying once. In a throwaway schema; skipped without a
  database.

Run from `server/`:

    uv run python tests/test_scaling.py

Exit status is non-zero when any check fails.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("KB_ENABLED", "false")

import test_worker as tw  # noqa: E402
from loguru import logger  # noqa: E402
from test_webhooks import ACCOUNT, TOKEN, URL, LedgerStore, signed, twilio_fields  # noqa: E402

from src.campaigns import (  # noqa: E402
    AttemptRecovery,
    CallAttemptStatus,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    CampaignWorker,
    MembershipStatus,
    WebhookOutcome,
    WebhookProcessor,
)
from src.campaigns.coordination import (  # noqa: E402
    QueueDepth,
    WorkerRecord,
    WorkerSummary,
    make_worker_id,
    transient_failure,
)
from src.reliability import CallingWindow, CampaignGuards, PacingLimiter  # noqa: E402
from src.telephony import CallStatus  # noqa: E402
from src.telephony.twilio import TwilioProvider  # noqa: E402

check = tw.check
build_world = tw.build_world
NOW = tw.NOW


def _own_dialer(w: tw.World, *, pacing_secs: float, max_concurrent: int) -> tuple[CampaignDialer, CampaignGuards]:
    """A dialer with its *own* guards — a second process's, not the world's shared one."""
    guards = CampaignGuards(
        window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC", clock=w.clock),
        pacing=PacingLimiter(pacing_secs, clock=w.clock.monotonic),
        max_concurrent=max_concurrent,
    )
    dialer = CampaignDialer(w.service, w.carrier, from_number="+15550001111", public_url="https://example.test", guards=guards)
    return dialer, guards


def _process(w: tw.World, *, pacing_secs: float = 0.0, max_concurrent: int = 1, **kwargs) -> CampaignWorker:
    """A worker that looks like a separate process: its own dialer and guards over the shared store."""
    dialer, guards = _own_dialer(w, pacing_secs=pacing_secs, max_concurrent=max_concurrent)
    settings = dict(
        recovery=AttemptRecovery(w.service, w.carrier, min_age_secs=600.0),
        guards=guards,
        poll_secs=2.0,
        idle_secs=30.0,
        recovery_interval_secs=300.0,
        recovery_min_age_secs=600.0,
        drain_secs=900.0,
        report_secs=60.0,
        clock=w.clock,
        sleep=w._sleep,
    )
    settings.update(kwargs)
    worker = CampaignWorker(w.service, dialer, **settings)
    w.workers.append(worker)
    return worker


# --- Two workers, one queue --------------------------------------------------------


async def check_shared_queue() -> None:
    """Every prospect once across the fleet, never more live than the limit, owners on every row."""
    print("\n=== two workers over one queue ===")
    w = build_world(script=[CallStatus.ANSWERED], max_concurrent=2)
    numbers = [f"+92300{n:07d}" for n in range(1, 7)]
    campaign = await w.campaign("Fleet", numbers)
    a = _process(w, max_concurrent=2)
    b = _process(w, max_concurrent=2)
    await a.start()
    await b.start()
    check("two workers registered", set(w.store.workers) == {a.worker_id, b.worker_id})
    check("with different ids", a.worker_id != b.worker_id)

    max_live = 0
    for round_no in range(30):
        # Calls end between ticks and are written by the carrier's webhook
        # (Phase 14) — the receiver is another process — so the slot frees
        # for whichever worker asks first. Neither process is always first:
        # the order a fleet ticks in is chance.
        if round_no % 3 == 2:
            for attempt in list(w.store.attempts.values()):
                if attempt.status.is_live and attempt.telephony_call_id:
                    w.carrier.end_call(attempt.telephony_call_id, CallStatus.COMPLETED)
                    await w.dialer.refresh(attempt)
        for worker in (a, b) if round_no % 2 == 0 else (b, a):
            await worker.tick()
            max_live = max(max_live, await w.store.count_live_attempts())
        w.clock.advance(2)
    counts = Counter(r.to_number for r in w.carrier.requests)
    check("each prospect was called exactly once", sorted(counts) == sorted(numbers) and all(n == 1 for n in counts.values()), str(dict(counts)))
    check("never more live than the shared limit", max_live <= 2, f"max live {max_live}")
    owners = Counter(attempt.worker_id for attempt in w.store.attempts.values())
    check("every attempt names the worker that placed it", set(owners) <= {a.worker_id, b.worker_id} and None not in owners, str(dict(owners)))
    check("both workers placed calls", len(owners) == 2, str(dict(owners)))
    check("each call was followed to its end by exactly one worker", a.metrics.completed + b.metrics.completed == 6 and a.metrics.handed_over == b.metrics.handed_over == 0, f"{a.metrics.describe()} / {b.metrics.describe()}")
    check("neither adopted the other's live calls", a.metrics.adopted == b.metrics.adopted == 0)
    check("the campaign completed itself", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)
    check("both heartbeat rows are running", all(w.store.workers[x.worker_id].status == "running" for x in (a, b)))
    await a.finish()
    await b.finish()
    check("and stopped once finished", all(w.store.workers[x.worker_id].status == "stopped" for x in (a, b)))


async def check_shared_pacing() -> None:
    """Pacing is one clock in the store, not one per process."""
    print("\n=== centralised pacing ===")
    w = build_world(script=[CallStatus.COMPLETED], max_concurrent=5)
    await w.campaign("Paced", ["+923001111111", "+923002222222", "+923003333333", "+923004444444"])
    a = _process(w, pacing_secs=10.0, max_concurrent=5)
    b = _process(w, pacing_secs=10.0, max_concurrent=5)
    await a.start()
    await b.start()
    mark = tw._mark()
    await a.tick()
    await b.tick()
    check("at the same instant only one call is placed", len(w.carrier.requests) == 1, str(len(w.carrier.requests)))
    check("the second worker's own limiter allowed it; the shared slot refused it", b.metrics.skips.get("pacing") == 1, str(dict(b.metrics.skips)))
    check("the refusal is logged as paced", tw._logged("dial.paced", mark) >= 1)
    check("the reservation was given back, not spent", all(m.attempt_count <= 1 for m in w.store.memberships.values()) and len([m for m in w.store.memberships.values() if m.status is MembershipStatus.IN_PROGRESS]) <= 1)
    check("the slot records the moment", w.store.pacing.get("pacing:global") == w.clock())
    w.clock.advance(5)
    await b.tick()
    check("five seconds later it is still refused", len(w.carrier.requests) == 1)
    w.clock.advance(5)
    await b.tick()
    check("ten seconds later the second worker places", len(w.carrier.requests) == 2)

    # A campaign's own interval on top of the deployment's.
    w = build_world(script=[CallStatus.COMPLETED], max_concurrent=5)
    slow = await w.campaign("Slow", ["+923005555555", "+923006666666"])
    await w.store.update_campaign_configuration(slow.id, "pacing_secs", 60)
    fast = await w.campaign("Fast", ["+923007777777", "+923008888888"])
    a = _process(w, pacing_secs=0.0, max_concurrent=5)
    await a.start()
    for _ in range(3):
        await a.tick()
        w.clock.advance(2)
    slow_numbers = {"+923005555555", "+923006666666"}

    def placed_for(numbers: set[str]) -> int:
        return sum(1 for r in w.carrier.requests if r.to_number in numbers)

    check("the campaign with its own pacing placed one call in six seconds", placed_for(slow_numbers) == 1, str([r.to_number for r in w.carrier.requests]))
    check("the unpaced campaign placed both", placed_for({"+923007777777", "+923008888888"}) == 2, str([r.to_number for r in w.carrier.requests]))
    check("its slot is scoped to the campaign", f"pacing:campaign:{slow.id}" in w.store.pacing and "pacing:global" not in w.store.pacing, str(sorted(w.store.pacing)))
    w.clock.advance(60)
    await a.tick()
    check("and a minute later the second call goes out", placed_for(slow_numbers) == 2)


# --- Heartbeats and health ---------------------------------------------------------


async def check_heartbeats() -> None:
    """A worker says it is alive, what it is doing, and when it stops."""
    print("\n=== heartbeat and health ===")
    w = build_world(script=[CallStatus.ANSWERED])
    await w.campaign("Beat", ["+923001111111"])
    worker = _process(w, heartbeat_secs=10.0, stale_secs=60.0)
    await worker.start()
    record = w.store.workers[worker.worker_id]
    check("registered at start", record.status == "running" and record.started_at == w.clock())
    check("with the host and pid", record.hostname == socket.gethostname() and record.pid == os.getpid())
    check("serving every active campaign", record.campaign_ids is None)
    first_beat = record.heartbeat_at

    report = await worker.tick()
    check("a call is placed and followed", len(worker.in_flight) == 1)
    check("the loop will not sleep past the next beat", report.sleep_secs <= 10.0, str(report.sleep_secs))
    w.clock.advance(10)
    await worker.tick()
    record = w.store.workers[worker.worker_id]
    check("the beat lands on time", record.heartbeat_at == w.clock() and record.heartbeat_at != first_beat)
    check("and carries what the worker is doing", record.in_flight == 1 and record.metrics.get("started") == 1, str(record.metrics))
    check("a fresh beat is healthy", record.health(w.clock(), 60.0) == "running")
    check("an old one is stale", record.health(w.clock() + timedelta(seconds=61), 60.0) == "stale")
    summary = await w.store.worker_summary(stale_after_secs=60.0)
    check("the summary counts it", summary.running == 1 and summary.alive == 1 and summary.in_flight == 1, summary.describe())

    worker.request_stop()
    await worker.tick()
    record = w.store.workers[worker.worker_id]
    check("a stop request is announced as draining on the very next tick", record.status == "draining", record.status)
    check("draining still counts as alive", (await w.store.worker_summary(stale_after_secs=60.0)).alive == 1)
    w.carrier.end_call(w.carrier.last_call_id, CallStatus.COMPLETED)
    await worker.tick()
    await worker.finish()
    record = w.store.workers[worker.worker_id]
    check("stopped at the end", record.status == "stopped" and record.stopped_at == w.clock() and record.in_flight == 0)
    check("a stopped row is never alive", record.health(w.clock(), 60.0) == "stopped" and record.is_stale(w.clock(), 60.0))
    check("the final metrics are on the row", record.metrics.get("completed") == 1, str(record.metrics))
    as_dict = record.to_dict(w.clock(), 60.0)
    check("the record serialises for the API", as_dict["worker_id"] == worker.worker_id and as_dict["health"] == "stopped", str(sorted(as_dict)))

    print("\n  identity:")
    plain = make_worker_id()
    named = make_worker_id("fleet node/1")
    check("a default id is host-pid-suffix", plain.startswith(f"{socket.gethostname()}-{os.getpid()}-") and len(plain.rsplit("-", 1)[1]) == 6, plain)
    check("a configured name is kept, sanitised, and still made unique", named.startswith("fleet-node-1-") and named != make_worker_id("fleet node/1"), named)


# --- Abandoned work -----------------------------------------------------------------


async def check_dead_worker() -> None:
    """A worker that stops beating loses its calls to a live one, and its reservations to the queue."""
    print("\n=== a worker dies mid-call ===")
    w = build_world(script=[CallStatus.ANSWERED], max_concurrent=3)
    campaign = await w.campaign("Dead", ["+923001111111", "+923002222222", "+923003333333"])
    a = _process(w, max_concurrent=3, stale_secs=60.0, max_calls=1)
    await a.start()
    await a.tick()
    live = a.in_flight[0]
    check("A placed a call and owns it", w.store.attempts[live.id].worker_id == a.worker_id)
    # A also reserved a second prospect and then died before dialling it.
    reservation = await w.service.next_call(campaign.id, worker_id=a.worker_id)
    check("and holds a reservation it never placed", reservation is not None and reservation.attempt.telephony_call_id is None)
    reserved_membership = reservation.membership.id

    w.clock.advance(30)
    b = _process(w, max_concurrent=3, stale_secs=60.0)
    await b.start()
    check("30s of silence is not death: B adopts nothing", b.in_flight == [] and b.metrics.released == 0)

    w.clock.advance(40)  # 70s since A's last beat.
    await b._adopt_live_attempts()
    check("after stale_secs B adopts A's live call", [x.id for x in b.in_flight] == [live.id], str([x.id for x in b.in_flight]))
    check("the row now names B", w.store.attempts[live.id].worker_id == b.worker_id)
    check("counted as adopted", b.metrics.adopted == 1)
    check("A's unplaced reservation went back to the queue", w.store.memberships[reserved_membership].status is MembershipStatus.PENDING and w.store.memberships[reserved_membership].attempt_count == 0)
    check("with its attempt row gone", reservation.attempt.id not in w.store.attempts)
    check("counted as released", b.metrics.released == 1)

    # A was only slow, not dead. It comes back — and finds the call is not its own any more.
    mark = tw._mark()
    await a.tick()
    check("A stops following the call B now owns", a.in_flight == [] and a.metrics.handed_over == 1)
    check("and says so", tw._logged("worker.handed_over", mark) == 1)
    check("A's beat is fresh again", w.store.workers[a.worker_id].heartbeat_at == w.clock())
    await b.tick()
    check("B does not give it back to a worker that is alive again", w.store.attempts[live.id].worker_id == b.worker_id)

    w.carrier.end_call(live.telephony_call_id, CallStatus.COMPLETED)
    await b.tick()
    await a.tick()
    check("the ending is written once, by B", w.store.attempts[live.id].status is CallAttemptStatus.COMPLETED and a.metrics.completed == 0 and b.metrics.outcomes.get("COMPLETED") == 1 and b.metrics.completed >= 1, f"{a.metrics.describe()} / {b.metrics.describe()}")
    check("the released prospect was dialled by whoever asked next", w.store.memberships[reserved_membership].attempt_count == 1 and w.store.memberships[reserved_membership].status is not MembershipStatus.PENDING, w.store.memberships[reserved_membership].status.value)


async def check_safe_shutdown() -> None:
    """A clean stop hands its calls on at once; no one waits for a row to go stale."""
    print("\n=== safe shutdown and hand-over ===")
    w = build_world(script=[CallStatus.ANSWERED], max_concurrent=2)
    await w.campaign("Hand", ["+923001111111", "+923002222222"])
    a = _process(w, max_concurrent=2, stale_secs=60.0)
    await a.start()
    await a.tick()
    check("A is on two calls", len(a.in_flight) == 2)
    ids = sorted(x.id for x in a.in_flight)

    a.request_stop()
    report = await a.tick()
    check("after a stop request A places nothing more but keeps following", len(a.in_flight) == 2 and w.store.workers[a.worker_id].status == "draining")
    check("and is not done while calls are up", not a._done(report))
    mark = tw._mark()
    await a.finish()
    check("finishing clears ownership of what is still live", all(w.store.attempts[i].worker_id is None for i in ids))
    check("and marks the row stopped", w.store.workers[a.worker_id].status == "stopped")
    check("the closing line says another worker will adopt them", tw._logged("worker.left_live", mark) == 1 and any("another running worker will adopt" in line for line in tw.LOGS[mark:]))

    b = _process(w, max_concurrent=2, stale_secs=60.0)
    await b.start()
    check("a worker starting right away adopts both without waiting for stale_secs", sorted(x.id for x in b.in_flight) == ids and b.metrics.adopted == 2)
    check("the rows name B", all(w.store.attempts[i].worker_id == b.worker_id for i in ids))
    for call_id in list(w.carrier.calls):
        w.carrier.end_call(call_id, CallStatus.COMPLETED)
    await b.tick()
    check("B writes both endings", b.metrics.completed == 2 and all(w.store.attempts[i].status is CallAttemptStatus.COMPLETED for i in ids), b.metrics.describe())

    print("\n  a database without the Phase 21 tables:")
    w = build_world(script=[CallStatus.COMPLETED])
    await w.campaign("Old", ["+923001111111"])

    class OldStore:
        """The store as it was before Phase 21: no coordination methods at all."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name in ("register_worker", "heartbeat_worker", "mark_worker_stopped", "claim_abandoned_attempts", "release_abandoned_reservations", "set_attempt_worker", "take_pacing_slot"):
                async def missing(*args, **kwargs):
                    raise tw.CampaignStoreError('relation "scheduler_workers" does not exist')

                return missing
            return getattr(self._inner, name)

    w.service._store = OldStore(w.store)  # type: ignore[assignment]
    mark = tw._mark()
    worker = _process(w, max_calls=1)
    metrics = await worker.run()
    check("the worker still runs, as a single worker", metrics.completed == 1, metrics.describe())
    check("saying once that coordination is unavailable", tw._logged("worker.coordination_unavailable", mark) == 1)
    check("and naming the fix", any("campaign.py init" in line for line in tw.LOGS[mark:] if "coordination_unavailable" in line))


# --- Retrying failed jobs -----------------------------------------------------------


async def check_transient_retry() -> None:
    """A placement the system failed is queued again; one the number failed is not."""
    print("\n=== retrying failed jobs ===")
    table = {
        "carrier unavailable: 503 Service Unavailable": True,
        "timeout after 10s waiting for the carrier": True,
        "HTTP 429 Too Many Requests": True,
        "the placement never reported an outcome": True,
        "released by recovery: reserved but never placed": True,
        "worker host-1-abc died with the call in progress": True,
        "connection reset by peer": True,
        "Invalid number: 13224": False,
        "the number is on the do-not-call list": False,
        "blocked: outside calling hours": False,
        "": False,
    }
    for reason, expected in table.items():
        check(f"{reason or '(empty)'!r} is {'transient' if expected else 'final'}", transient_failure(reason) is expected)
    check("None is not transient", transient_failure(None) is False)

    w = build_world(max_attempts=3, retry_minutes=60)
    campaign = await w.campaign("Retry", ["+923001111111"])
    membership = await w.membership_of("+923001111111", campaign)
    queued = await w.service.next_call(campaign.id)
    await w.service.record_outcome(queued.attempt, CallAttemptStatus.FAILED, failure_reason="carrier unavailable: 503")
    m = w.store.memberships[membership.id]
    check("a transient failure puts the membership back in the queue", m.status is MembershipStatus.PENDING, m.status.value)
    check("after the retry wait", m.next_attempt_at == w.clock() + timedelta(minutes=60), str(m.next_attempt_at))
    check("having spent the attempt", m.attempt_count == 1)
    check("the attempt row stays FAILED with its reason", w.store.attempts[queued.attempt.id].status is CallAttemptStatus.FAILED and "503" in (w.store.attempts[queued.attempt.id].failure_reason or ""))

    w.clock.advance(61 * 60)
    again = await w.service.next_call(campaign.id)
    check("and is handed out again when due", again is not None and again.attempt.attempt_number == 2)
    await w.service.record_outcome(again.attempt, CallAttemptStatus.FAILED, failure_reason="Invalid number: 13224")
    m = w.store.memberships[membership.id]
    check("a failure about the number itself is final", m.status is MembershipStatus.EXHAUSTED, m.status.value)

    w = build_world(max_attempts=1, retry_minutes=60)
    campaign = await w.campaign("Ceiling", ["+923001111111"])
    queued = await w.service.next_call(campaign.id)
    await w.service.record_outcome(queued.attempt, CallAttemptStatus.FAILED, failure_reason="timeout waiting for the carrier")
    m = next(iter(w.store.memberships.values()))
    check("a transient failure at the attempt ceiling is exhausted, not retried", m.status is MembershipStatus.EXHAUSTED)

    w = build_world(max_attempts=3, retry_minutes=60)
    campaign = await w.campaign("Off", ["+923001111111"])
    service = CampaignService(w.store, default_region="PK", max_attempts=3, retry_minutes=60, clock=w.clock, retry_transient_failures=False)  # type: ignore[arg-type]
    queued = await service.next_call(campaign.id)
    await service.record_outcome(queued.attempt, CallAttemptStatus.FAILED, failure_reason="carrier unavailable: 503")
    m = next(iter(w.store.memberships.values()))
    check("with retries switched off a transient failure is final, as before Phase 21", m.status is MembershipStatus.EXHAUSTED)

    w = build_world(max_attempts=3, retry_minutes=60)
    campaign = await w.campaign("Quick", ["+923001111111"])
    service = CampaignService(w.store, default_region="PK", max_attempts=3, retry_minutes=60, clock=w.clock, transient_retry_minutes=5)  # type: ignore[arg-type]
    queued = await service.next_call(campaign.id)
    await service.record_outcome(queued.attempt, CallAttemptStatus.FAILED, failure_reason="HTTP 502 Bad Gateway")
    m = next(iter(w.store.memberships.values()))
    check("a separate wait for transient retries is honoured", m.status is MembershipStatus.PENDING and m.next_attempt_at == w.clock() + timedelta(minutes=5), str(m.next_attempt_at))


# --- Duplicate webhooks -------------------------------------------------------------


async def check_duplicate_webhooks() -> None:
    """The same carrier event, delivered to several receivers at once, is applied once."""
    print("\n=== duplicate webhook processing ===")
    clock = tw.FakeClock(NOW)
    store = LedgerStore(clock=clock)
    service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
    carrier = tw.ScriptedCarrier(clock, script=[CallStatus.RINGING, CallStatus.ANSWERED])
    guards = CampaignGuards(window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC", clock=clock), pacing=PacingLimiter(0, clock=clock.monotonic), max_concurrent=1)
    dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://abc123.ngrok.app", guards=guards, status_callback_url=URL)
    recovery = AttemptRecovery(service, carrier, min_age_secs=120.0)
    w = tw.World(clock=clock, store=store, service=service, carrier=carrier, guards=guards, dialer=dialer, recovery=recovery)
    campaign = await w.campaign("Twice", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    call = carrier.last_call_id
    membership = await w.membership_of("+923001111111", campaign)

    # Two receivers (two API processes behind one carrier) each get the same event.
    receivers = [WebhookProcessor(service, TwilioProvider(ACCOUNT, TOKEN), expected_url=URL) for _ in range(2)]
    fields = twilio_fields(call, "completed", 3, CallDuration="42")
    receipts = await asyncio.gather(*(r.receive(signed(fields)) for r in receivers for _ in range(3)))
    outcomes = Counter(r.outcome for r in receipts)
    check("six deliveries: one applied, five duplicates", outcomes.get(WebhookOutcome.APPLIED.value) == 1 and outcomes.get(WebhookOutcome.DUPLICATE.value) == 5, str(dict(outcomes)))
    check("every delivery was accepted (the carrier is not asked to resend)", all(r.accepted for r in receipts))
    attempt = w.store.attempts[worker.in_flight[0].id]
    check("the attempt ended once", attempt.status is CallAttemptStatus.COMPLETED and attempt.duration_seconds == 42)
    check("the membership moved on once", w.store.memberships[membership.id].status is MembershipStatus.COMPLETED and w.store.memberships[membership.id].attempt_count == 1)
    check("one ledger row", len(store.deliveries) == 1)
    await worker.tick()
    check("the worker's poll finds it finished and counts it once", worker.metrics.completed == 1 and worker.in_flight == [])

    # An older event arriving after a newer one changes nothing.
    stale = await receivers[0].receive(signed(twilio_fields(call, "in-progress", 2)))
    check("an out-of-order earlier event is recorded and ignored", stale.accepted and w.store.attempts[attempt.id].status is CallAttemptStatus.COMPLETED, stale.outcome)


# --- Metrics -------------------------------------------------------------------------


async def check_metrics() -> None:
    """Queue depth and worker health as numbers, for the dashboard and the health check."""
    print("\n=== metrics: queue depth and worker health ===")
    w = build_world(script=[CallStatus.ANSWERED], max_concurrent=1)
    campaign = await w.campaign("Depth", ["+923001111111", "+923002222222", "+923003333333"])
    later = await w.membership_of("+923003333333", campaign)
    w.store.force_membership(later.id, status=MembershipStatus.PENDING, attempt_count=1, next_attempt_at=w.clock() + timedelta(hours=1))
    worker = _process(w, heartbeat_secs=10.0)
    await worker.start()
    await worker.tick()
    w.clock.advance(10)
    await worker.tick()  # The beat that carries in_flight=1.
    depth = await w.store.queue_depth(max_attempts=3)
    check("one due, one scheduled, one live", (depth.due_now, depth.scheduled, depth.live) == (1, 1, 1), depth.describe())
    check("no unplaced reservation", depth.reserved == 0)
    check("per campaign", depth.per_campaign[0]["campaign_id"] == campaign.id and depth.per_campaign[0]["in_progress"] == 1, str(depth.per_campaign))
    check("backlog is what is due now, queue and callbacks", depth.backlog == depth.due_now + depth.callbacks_due == 1)
    check("it serialises", depth.to_dict()["due_now"] == 1 and "per_campaign" in depth.to_dict())
    summary = await w.store.worker_summary(stale_after_secs=60.0)
    check("the summary reads for a person", "1 alive" in summary.describe() and "1 call(s) followed" in summary.describe(), summary.describe())
    check("and for a machine", summary.to_dict(w.clock(), 60.0)["running"] == 1 and len(summary.to_dict(w.clock(), 60.0)["workers"]) == 1)
    empty = WorkerSummary()
    check("no workers is said plainly", empty.alive == 0 and empty.describe(), empty.describe())
    check("queue depth defaults to zero", QueueDepth().backlog == 0 and QueueDepth().describe())

    print("\n  the dashboard strip:")
    from src.dashboard.stats import _scheduler

    strip = {m.key: m for m in _scheduler(summary, depth)}
    check("workers alive, calls followed, due now, scheduled later", set(strip) == {"workers_alive", "workers_in_flight", "queue_due", "queue_scheduled"}, str(sorted(strip)))
    check("with the numbers", (strip["workers_alive"].value, strip["workers_in_flight"].value, strip["queue_due"].value, strip["queue_scheduled"].value) == (1, 1, 1, 1))
    check("alive is good", strip["workers_alive"].tone == "good")
    missing = {m.key: m for m in _scheduler(None, None)}
    check("without the tables the tiles are unavailable, not zero", all(not m.available for m in missing.values()))
    idle = {m.key: m for m in _scheduler(WorkerSummary(), depth)}
    check("work due with nobody running is a warning", idle["queue_due"].tone == "warn" and idle["workers_alive"].tone == "warn")
    stale = WorkerSummary(stale=1, workers=(WorkerRecord("w", "h", 1, "running"),))
    check("a stale worker is bad", {m.key: m for m in _scheduler(stale, depth)}["workers_alive"].tone == "bad")

    print("\n  the worker's own counters:")
    check("adopted, released and handed over are in the snapshot", {"adopted", "released", "handed_over"} <= set(worker.metrics.snapshot()))
    check("and in the heartbeat row", {"adopted", "released", "handed_over"} <= set(w.store.workers[worker.worker_id].metrics or worker.metrics.snapshot()))


async def check_pipeline_untouched() -> None:
    """Nothing here touches the realtime pipeline."""
    print("\n=== the realtime pipeline is untouched ===")
    root = Path(__file__).resolve().parent.parent
    coordination = (root / "src" / "campaigns" / "coordination.py").read_text(encoding="utf-8")
    check("coordination imports nothing from pipecat or the telephony layer", "pipecat" not in coordination and "telephony" not in coordination.split('"""', 2)[2])
    bot = (root / "bot.py").read_text(encoding="utf-8")
    check("bot.py knows nothing of workers, claims or pacing slots", all(word not in bot for word in ("coordination", "scheduler_workers", "take_pacing_slot", "claim_abandoned", "worker_id")))
    pipeline_dir = root / "src" / "pipeline"
    if pipeline_dir.exists():
        text = "".join(p.read_text(encoding="utf-8") for p in pipeline_dir.rglob("*.py"))
        check("nor does the pipeline package", "coordination" not in text and "scheduler_workers" not in text)


# --- The SQL, against PostgreSQL -------------------------------------------------------


async def run_database_checks(dsn: str) -> None:
    """The Phase 21 SQL, in a schema that is thrown away."""
    from test_campaigns import StubProvider, with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== the SQL, against PostgreSQL ===")
        columns = await store._pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = 'call_attempts'", schema)
        check("call_attempts has a worker_id column", "worker_id" in {r["column_name"] for r in columns})
        tables = {r["table_name"] for r in await store._pool.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = $1", schema)}
        check("the heartbeat and state tables exist", {"scheduler_workers", "scheduler_state"} <= tables, str(sorted(tables)))

        print("\n  the reservation under the advisory lock:")
        service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60)
        campaign = await service.create_campaign(f"Lock {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        prospects = [await service.create_prospect(first_name="Row", last_name=str(n), phone=f"0301 {n:07d}") for n in range(1, 7)]
        await service.add_prospects(campaign.id, [p.id for p in prospects])
        results = await asyncio.gather(
            *(store.reserve_next_call(campaign.id, max_attempts=3, max_concurrent=2, worker_id=f"racer-{n}") for n in range(6))
        )
        taken = [r for r in results if r is not None]
        check("six concurrent reservations with a limit of two: exactly two succeed", len(taken) == 2, f"{len(taken)} reserved")
        check("for two different prospects", len({r.prospect.id for r in taken}) == len(taken))
        check("each stamped with its worker", all(r.attempt.worker_id and r.attempt.worker_id.startswith("racer-") for r in taken))
        for r in taken:
            await store.unreserve_attempt(r.attempt.id, next_attempt_at=datetime.now(UTC))

        print("\n  the pacing slot:")
        first = await store.take_pacing_slot(10.0)
        second = await store.take_pacing_slot(10.0)
        check("the first taker gets the slot", first == (True, 0.0), str(first))
        check("the second is told how long to wait", second[0] is False and 0 < second[1] <= 10.0, str(second))
        check("no interval means no pacing", await store.take_pacing_slot(0.0) == (True, 0.0))
        scoped = await store.take_pacing_slot(0.0, campaign_id=campaign.id, campaign_interval_secs=30.0)
        scoped_again = await store.take_pacing_slot(0.0, campaign_id=campaign.id, campaign_interval_secs=30.0)
        check("a campaign's own interval is a second scope", scoped == (True, 0.0) and scoped_again[0] is False and scoped_again[1] <= 30.0, f"{scoped} {scoped_again}")
        other = await service.create_campaign(f"Other {uuid.uuid4().hex[:6]}")
        check("that does not pace another campaign", (await store.take_pacing_slot(0.0, campaign_id=other.id, campaign_interval_secs=30.0)) == (True, 0.0))
        rows = await store._pool.fetch("SELECT key FROM scheduler_state ORDER BY key")
        check("one state row per scope", {r["key"] for r in rows} == {"pacing:global", f"pacing:campaign:{campaign.id}", f"pacing:campaign:{other.id}"}, str([r["key"] for r in rows]))

        print("\n  the heartbeat table:")
        dead = await store.register_worker("dead-1", hostname="h1", pid=11, campaign_ids=[campaign.id])
        alive = await store.register_worker("alive-1", hostname="h2", pid=22)
        check("registered as running", dead.status == "running" and alive.status == "running" and dead.campaign_ids == (campaign.id,))
        beat = await store.heartbeat_worker("alive-1", status="running", in_flight=2, metrics={"started": 5})
        check("a beat updates the row", beat is not None and beat.in_flight == 2 and beat.metrics.get("started") == 5)
        check("a beat for an unknown worker is None", await store.heartbeat_worker("nobody") is None)
        check("registering again is an upsert, not an error", (await store.register_worker("alive-1", hostname="h2", pid=22)).worker_id == "alive-1")
        listed = await store.list_workers()
        check("both are listed", {w.worker_id for w in listed} >= {"dead-1", "alive-1"})
        summary = await store.worker_summary(stale_after_secs=60.0)
        check("both count as running", summary.running >= 2 and summary.stale == 0, summary.describe())
        await store._pool.execute("UPDATE scheduler_workers SET heartbeat_at = now() - interval '5 minutes' WHERE worker_id = 'dead-1'")
        summary = await store.worker_summary(stale_after_secs=60.0)
        check("a silent worker is stale", summary.stale == 1, summary.describe())

        print("\n  claiming a dead worker's calls:")
        held = await store.reserve_next_call(campaign.id, max_attempts=3, worker_id="dead-1")
        await store._pool.execute("UPDATE call_attempts SET telephony_call_id = 'CAdead001', status = 'QUEUED' WHERE id = $1", held.attempt.id)
        parked = await store.reserve_next_call(campaign.id, max_attempts=3, worker_id="dead-1")
        mine = await store.reserve_next_call(campaign.id, max_attempts=3, worker_id="alive-1")
        check("three reservations: one placed by the dead worker, one it never placed, one the live worker's", held and parked and mine)
        await store._pool.execute("UPDATE scheduler_workers SET heartbeat_at = now() WHERE worker_id = 'dead-1'")
        check("nothing is claimed while the owner is beating", await store.claim_abandoned_attempts("alive-1", stale_after_secs=60.0) == [])
        check("nothing is released either", await store.release_abandoned_reservations(stale_after_secs=60.0) == 0)
        await store._pool.execute("UPDATE scheduler_workers SET heartbeat_at = now() - interval '5 minutes' WHERE worker_id = 'dead-1'")
        claimed = await store.claim_abandoned_attempts("alive-1", stale_after_secs=60.0)
        check("once stale, the placed call is claimed", [a.id for a in claimed] == [held.attempt.id], str([a.id for a in claimed]))
        check("and now names the claimant", (await store.get_attempt(held.attempt.id)).worker_id == "alive-1")
        check("a second claim finds nothing", await store.claim_abandoned_attempts("alive-1", stale_after_secs=60.0) == [])
        released = await store.release_abandoned_reservations(stale_after_secs=60.0)
        check("the unplaced reservation is released", released == 1, str(released))
        check("its membership is pending again with the count restored", (await store.get_membership(parked.membership.id)).status is MembershipStatus.PENDING and (await store.get_membership(parked.membership.id)).attempt_count == 0)
        check("the live worker's own reservation is untouched", (await store.get_attempt(mine.attempt.id)) is not None and (await store.get_membership(mine.membership.id)).status is MembershipStatus.IN_PROGRESS)
        check("a stopped worker's calls are claimable at once", await store.mark_worker_stopped("alive-1") and (await store.set_attempt_worker(held.attempt.id, "alive-1")) and [a.id for a in await store.claim_abandoned_attempts("fresh-1", stale_after_secs=60.0)] == [held.attempt.id])
        check("so are calls owned by nobody", await store.set_attempt_worker(held.attempt.id, None) and [a.id for a in await store.claim_abandoned_attempts("fresh-2", stale_after_secs=60.0)] == [held.attempt.id])
        check("setting a worker on a missing attempt is False", await store.set_attempt_worker(999_999, "x") is False)

        print("\n  queue depth:")
        depth = await store.queue_depth(max_attempts=3)
        row = next((r for r in depth.per_campaign if r["campaign_id"] == campaign.id), None)
        check("the campaign is counted", row is not None and row["in_progress"] == 2 and row["due_now"] >= 3, str(row))
        check("live and reserved attempts are counted deployment-wide", depth.live >= 2 and depth.reserved >= 1, depth.describe())
        check("stopped rows can be pruned once old", (await store.prune_workers(older_than_secs=60.0)) >= 1)

        print("\n  five concurrent deliveries of one webhook:")
        placed = await CampaignDialer(service, StubProvider(outcome=CallStatus.ANSWERED), from_number="+15550001111", public_url="https://abc123.ngrok.app", status_callback_url=URL).dial_next(campaign.id)
        check("a call is placed", placed.placed, placed.describe())
        call = placed.attempt.telephony_call_id
        receivers = [WebhookProcessor(service, TwilioProvider(ACCOUNT, TOKEN), expected_url=URL) for _ in range(5)]
        receipts = await asyncio.gather(*(r.receive(signed(twilio_fields(call, "completed", 3, CallDuration="42"))) for r in receivers))
        outcomes = Counter(r.outcome for r in receipts)
        check("applied once, four duplicates", outcomes.get(WebhookOutcome.APPLIED.value) == 1 and outcomes.get(WebhookOutcome.DUPLICATE.value) == 4, str(dict(outcomes)))
        final = await store.get_attempt(placed.attempt.id)
        check("the attempt ended once with the duration", final.status is CallAttemptStatus.COMPLETED and final.duration_seconds == 42)
        ledger = await store._pool.fetchval("SELECT count(*) FROM telephony_webhook_events WHERE call_id = $1", call)
        check("one ledger row", ledger == 1, str(ledger))
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


# --- Runner -----------------------------------------------------------------------------


async def main() -> int:
    """Run every check and report."""
    print("Scaling checks — several workers, one queue, one database.")
    handler = logger.add(tw.LOGS.append, format="{message}", level="DEBUG")
    try:
        await check_shared_queue()
        await check_shared_pacing()
        await check_heartbeats()
        await check_dead_worker()
        await check_safe_shutdown()
        await check_transient_retry()
        await check_duplicate_webhooks()
        await check_metrics()
        await check_pipeline_untouched()

        from dotenv import load_dotenv

        load_dotenv(override=True)
        dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
        if not dsn:
            tw._skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
        else:
            import asyncpg

            try:
                await run_database_checks(dsn)
            except (OSError, asyncpg.PostgresError) as exc:
                tw._skipped.append(f"database checks (cannot reach PostgreSQL: {exc})")
    finally:
        logger.remove(handler)

    print()
    if tw._skipped:
        print("SKIPPED:")
        for item in tw._skipped:
            print(f"  - {item}")
        print()
    if tw._failures:
        print(f"{len(tw._failures)} check(s) FAILED:")
        for failure in tw._failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed." + (" (some were skipped)" if tw._skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
