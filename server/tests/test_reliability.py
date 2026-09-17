#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Failure-injection checks for Phase 9. No keys, no database, no phone, no audio.

Run it from the `server/` directory::

    uv run python tests/test_reliability.py

**What this is for.** Every other check script in this project proves the system
works. This one proves it fails safely, which needs the failures to be
*injected* rather than waited for: a carrier that times out, a database that has
gone away, an LLM that starts a response and never finishes it, a webhook
delivered twice, a process that dies between reserving a call and placing it.

The failure the whole phase is built around gets the most attention here: **the
system must never place two calls to the same person by accident.** The checks
below drive each of the five mechanisms that prevent that, one at a time, with
the others taken out of the way, so a regression in any single one of them fails
a check rather than being covered by the next.

**Stubs, not mocks.** `FlakyCarrier` is a real `TelephonyProvider` that can be
told to time out, refuse, or vanish mid-request; `BrokenStore` is a real store
whose queries raise. The code under test is the code that runs in production —
the actual dialer, the actual recovery pass, the actual supervisor — with only
the outside world replaced. The SQL half of the duplicate protection is in
`tests/test_campaigns.py`, where it can run against a real PostgreSQL.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from src.campaigns import (  # noqa: E402
    AttemptRecovery,
    CallAttempt,
    CallAttemptStatus,
    Campaign,
    CampaignDialer,
    CampaignProspect,
    CampaignStatus,
    CampaignStoreError,
    MembershipStatus,
    Prospect,
    ProspectStatus,
    QueuedCall,
)
from src.campaigns.models import LIVE_STATUS_SQL, may_advance  # noqa: E402
from src.reliability import (  # noqa: E402
    NEVER_RETRY,
    READ_POLICY,
    AmbiguousOutcomeError,
    CallingWindow,
    CampaignGuards,
    PacingLimiter,
    Reason,
    RetryPolicy,
    SessionSupervisor,
    Verdict,
    call_with_retry,
    campaign_call_key,
    check_concurrency,
    check_duration,
    guarded,
    install_scrubber,
    prospect_timezone,
    read_classifier,
    redact,
    write_classifier,
)
from src.reliability.supervisor import stage_of  # noqa: E402
from src.telephony import (  # noqa: E402
    CallRequest,
    CallSetupError,
    CallSnapshot,
    CallStatus,
    ProviderUnavailableError,
    TelephonyProvider,
)

_failures: list[str] = []

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)  # A Monday, mid-morning.


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


# --- The world, and the ways it breaks ---------------------------------------


class FlakyCarrier(TelephonyProvider):
    """A `TelephonyProvider` that can be told exactly how to fail.

    The real contract, with the phone network replaced by a script. Every
    failure mode Phase 9 has to survive is one attribute here, so a check reads
    as "given a carrier that times out, the dialer does X".
    """

    name = "flaky"
    transports = ("twilio",)

    def __init__(
        self,
        *,
        place_error: Exception | None = None,
        fetch_error: Exception | None = None,
        fetch_status: CallStatus = CallStatus.COMPLETED,
        recent: list[CallSnapshot] | None = None,
        recent_error: Exception | None = None,
        can_list: bool = True,
    ) -> None:
        self.place_error = place_error
        self.fetch_error = fetch_error
        self.fetch_status = fetch_status
        self.recent = recent if recent is not None else []
        self.recent_error = recent_error
        self.can_list = can_list
        self.placed: list[CallRequest] = []
        self.hung_up: list[str] = []
        self.fetches = 0
        self.searches = 0
        self._next = 0

    async def place_call(self, request: CallRequest) -> CallSnapshot:
        self.placed.append(request)
        if self.place_error is not None:
            raise self.place_error
        self._next += 1
        return CallSnapshot(
            provider=self.name,
            call_id=f"CA{self._next:04d}",
            status=CallStatus.QUEUED,
            to_number=request.to_number,
            created_at=NOW,
        )

    async def fetch_call(self, call_id: str) -> CallSnapshot:
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return CallSnapshot(
            provider=self.name,
            call_id=call_id,
            status=self.fetch_status,
            duration_secs=42.0,
            created_at=NOW,
        )

    async def find_recent_calls(
        self, to_number: str, *, since: datetime, limit: int = 20
    ) -> list[CallSnapshot]:
        self.searches += 1
        if not self.can_list:
            raise NotImplementedError("this carrier cannot list calls")
        if self.recent_error is not None:
            raise self.recent_error
        return [c for c in self.recent if c.created_at is None or c.created_at >= since]

    async def hang_up(self, call_id: str) -> None:
        self.hung_up.append(call_id)

    async def transfer_call(self, call_id, to_number, *, caller_id=None) -> None:
        return None

    def make_serializer(self, call_data: Any):
        raise NotImplementedError

    async def check_credentials(self) -> str:
        return "stub account"


@dataclass
class FakeStore:
    """The store's surface, in memory, with the invariants Phase 9 relies on.

    Not a stand-in for the SQL — `tests/test_campaigns.py` checks that against a
    real PostgreSQL — but for the *dialer's and recovery's* behaviour, which is
    what these checks are about. It enforces the two rules that matter here:
    a live attempt blocks its prospect, and one attempt cannot be given two
    carrier call ids.
    """

    attempts: dict[int, CallAttempt] = field(default_factory=dict)
    prospects: dict[int, Prospect] = field(default_factory=dict)
    keys: dict[str, int] = field(default_factory=dict)
    fail_with: Exception | None = None
    events: list[tuple[int, CallAttemptStatus, bool]] = field(default_factory=list)
    _next_id: int = 100

    def add_attempt(self, **fields: Any) -> CallAttempt:
        self._next_id += 1
        attempt = CallAttempt(
            id=fields.pop("id", self._next_id),
            prospect_id=fields.pop("prospect_id", 7),
            campaign_id=fields.pop("campaign_id", 3),
            campaign_prospect_id=fields.pop("campaign_prospect_id", 5),
            attempt_number=fields.pop("attempt_number", 1),
            **fields,
        )
        self.attempts[attempt.id] = attempt
        return attempt

    def _guard(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def get_prospect(self, prospect_id: int) -> Prospect | None:
        self._guard()
        return self.prospects.get(prospect_id)

    async def get_attempt(self, attempt_id: int) -> CallAttempt | None:
        self._guard()
        return self.attempts.get(attempt_id)

    async def count_live_attempts(self, *, campaign_id: int | None = None) -> int:
        self._guard()
        return sum(1 for a in self.attempts.values() if a.status.is_live)

    async def has_live_attempt(self, prospect_id, *, exclude_attempt_id=None) -> bool:
        self._guard()
        return any(
            a.prospect_id == prospect_id and a.status.is_live and a.id != exclude_attempt_id
            for a in self.attempts.values()
        )

    async def list_live_attempts(self, *, older_than_secs=0.0, limit=100) -> list[CallAttempt]:
        self._guard()
        return [a for a in self.attempts.values() if a.status.is_live][:limit]

    async def mark_placement_started(self, attempt_id: int) -> CallAttempt | None:
        self._guard()
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            return None
        updated = _replace(
            attempt,
            status=CallAttemptStatus.CALLING
            if attempt.status is CallAttemptStatus.PENDING
            else attempt.status,
            placement_started_at=attempt.placement_started_at or NOW,
        )
        self.attempts[attempt_id] = updated
        return updated

    async def mark_attempt_placed(self, attempt_id, *, telephony_call_id, provider):
        self._guard()
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            return None
        if attempt.telephony_call_id and attempt.telephony_call_id != telephony_call_id:
            raise CampaignStoreError(
                f"Attempt {attempt_id} is already placed as call {attempt.telephony_call_id}"
            )
        updated = _replace(
            attempt,
            status=CallAttemptStatus.QUEUED,
            telephony_call_id=telephony_call_id,
            telephony_provider=provider,
        )
        self.attempts[attempt_id] = updated
        return updated

    async def mark_attempt_unresolved(self, attempt_id: int, reason: str):
        self._guard()
        attempt = self.attempts.get(attempt_id)
        if attempt is None or attempt.telephony_call_id or not attempt.status.is_live:
            return None
        updated = _replace(
            attempt, status=CallAttemptStatus.UNRESOLVED, failure_reason=reason
        )
        self.attempts[attempt_id] = updated
        return updated

    async def apply_call_event(
        self,
        *,
        status,
        attempt_id=None,
        telephony_call_id=None,
        duration_seconds=None,
        failure_reason=None,
    ):
        self._guard()
        attempt = self.attempts.get(attempt_id) if attempt_id else next(
            (a for a in self.attempts.values() if a.telephony_call_id == telephony_call_id), None
        )
        if attempt is None:
            return None, False
        if not may_advance(attempt.status, status):
            self.events.append((attempt.id, status, False))
            return attempt, False
        updated = _replace(
            attempt,
            status=status,
            duration_seconds=duration_seconds or attempt.duration_seconds,
            failure_reason=attempt.failure_reason or failure_reason,
        )
        self.attempts[attempt.id] = updated
        self.events.append((attempt.id, status, True))
        return updated, True

    async def update_attempt_status(
        self, attempt_id, status, *, failure_reason=None, duration_seconds=None
    ):
        self._guard()
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            return None
        updated = _replace(
            attempt,
            status=status,
            failure_reason=attempt.failure_reason or failure_reason,
            duration_seconds=duration_seconds or attempt.duration_seconds,
        )
        self.attempts[attempt_id] = updated
        return updated

    async def set_membership_status(self, membership_id, status, *, next_attempt_at=None):
        self._guard()

    async def set_prospect_status(self, prospect_id, status):
        self._guard()

    async def get_membership(self, membership_id):
        self._guard()
        return None

    async def save_call_result(self, result):
        self._guard()
        return None

    async def set_callbacks_status(self, prospect_id, status, **kwargs):
        self._guard()
        return 0


def _replace(attempt: CallAttempt, **changes: Any) -> CallAttempt:
    """A `CallAttempt` with fields changed. `dataclasses.replace` for a frozen row."""
    import dataclasses

    return dataclasses.replace(attempt, **changes)


class FakeService:
    """`CampaignService`'s surface, over `FakeStore`, recording what it was told."""

    def __init__(self, store: FakeStore, *, callable_reason: str = "") -> None:
        self.store = store
        self.released: list[tuple[int, str]] = []
        self.outcomes: list[tuple[int, CallAttemptStatus, str | None]] = []
        self._callable_reason = callable_reason

    async def check_callable(self, prospect, campaign=None, membership=None, *, ignore_attempt_id=None, ignore_attempt_limit=False):
        from src.campaigns import CallabilityCheck

        if self._callable_reason:
            return CallabilityCheck(False, self._callable_reason)
        return CallabilityCheck(True)

    async def unreserve(self, attempt) -> bool:
        # Phase 13: this fake cannot undo a reservation, so recovery falls
        # back to closing it — the path the checks below pin.
        return False

    async def release(self, queued: QueuedCall, reason: str) -> None:
        self.released.append((queued.attempt.id, reason))
        await self.store.update_attempt_status(
            queued.attempt.id, CallAttemptStatus.FAILED, failure_reason=reason
        )

    async def record_outcome(self, attempt, status, *, failure_reason=None, duration_seconds=None):
        self.outcomes.append((attempt.id, status, failure_reason))
        return await self.store.update_attempt_status(
            attempt.id, status, failure_reason=failure_reason, duration_seconds=duration_seconds
        )

    async def next_call(self, campaign_id):
        return None


def queued_call(store: FakeStore, **attempt_fields: Any) -> QueuedCall:
    """A reserved call, ready to be dialled, with its prospect in the store."""
    prospect = Prospect(
        id=7,
        first_name="Sarah",
        last_name="Khan",
        phone="+923001234567",
        phone_normalized="+923001234567",
        custom_data=attempt_fields.pop("custom_data", {}),
    )
    store.prospects[prospect.id] = prospect
    attempt = store.add_attempt(**attempt_fields)
    return QueuedCall(
        attempt=attempt,
        prospect=prospect,
        campaign=Campaign(id=3, name="Q1", status=CampaignStatus.ACTIVE),
        membership=CampaignProspect(id=5, campaign_id=3, prospect_id=7),
    )


def make_dialer(service: FakeService, provider: TelephonyProvider, **kwargs: Any) -> CampaignDialer:
    """The real dialer, over the stubs."""
    return CampaignDialer(
        service,  # type: ignore[arg-type]
        provider,
        from_number="+15550001111",
        public_url="https://example.test",
        **kwargs,
    )


# --- The checks ---------------------------------------------------------------


async def check_retry_policy() -> None:
    """Bounded attempts, exponential backoff, jitter, and the three verdicts."""
    print("\n=== retry policy ===")
    policy = RetryPolicy(attempts=4, base_delay_secs=1.0, multiplier=2.0, max_delay_secs=5.0, jitter=0.0)
    check("backoff is exponential", [policy.delay_for(n) for n in (1, 2, 3)] == [1.0, 2.0, 4.0])
    check("and capped", policy.delay_for(9) == 5.0)
    check("attempt 0 waits not at all", policy.delay_for(0) == 0.0)

    jittered_policy = RetryPolicy(attempts=2, base_delay_secs=1.0, jitter=0.5)
    check("jitter is bounded below", jittered_policy.delay_for(1, rand=lambda: 0.0) == 0.5)
    check("and above", jittered_policy.delay_for(1, rand=lambda: 1.0) == 1.0)
    check("a policy with no attempts is refused", _raises(lambda: RetryPolicy(attempts=0), ValueError))
    check("so is impossible jitter", _raises(lambda: RetryPolicy(jitter=2.0), ValueError))

    slept: list[float] = []
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("network went away")
        return "ok"

    result = await call_with_retry(
        flaky,
        policy=RetryPolicy(attempts=5, base_delay_secs=0.1, jitter=0.0),
        classify=read_classifier,
        name="flaky",
        sleep=_record(slept),
    )
    check("a transient failure is retried until it works", result == "ok" and calls == 3)
    check("with a wait between each", [round(s, 3) for s in slept] == [0.1, 0.2])

    attempts = 0

    async def always_fails() -> str:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("still down")

    check(
        "retries are bounded",
        await _araises(
            call_with_retry(
                always_fails,
                policy=RetryPolicy(attempts=3, base_delay_secs=0, jitter=0),
                classify=read_classifier,
                name="down",
                sleep=_record([]),
            ),
            ConnectionError,
        )
        and attempts == 3,
        f"{attempts} attempts",
    )

    fatal_calls = 0

    async def refused() -> str:
        nonlocal fatal_calls
        fatal_calls += 1
        raise CallSetupError("the number is not valid")

    check(
        "a fatal failure is not retried",
        await _araises(
            call_with_retry(refused, policy=READ_POLICY, classify=read_classifier, name="refused"),
            CallSetupError,
        )
        and fatal_calls == 1,
    )

    ambiguous_calls = 0

    async def timed_out() -> str:
        nonlocal ambiguous_calls
        ambiguous_calls += 1
        raise TimeoutError("no answer")

    check(
        "an ambiguous write is not retried",
        await _araises(
            call_with_retry(
                timed_out,
                policy=RetryPolicy(attempts=5, base_delay_secs=0, jitter=0),
                classify=write_classifier,
                name="write",
            ),
            AmbiguousOutcomeError,
        )
        and ambiguous_calls == 1,
        f"{ambiguous_calls} attempt(s)",
    )
    check(
        "but the same failure on a read is retried",
        write_classifier(TimeoutError()) is Verdict.AMBIGUOUS
        and read_classifier(TimeoutError()) is Verdict.RETRY,
    )
    check("an unfamiliar failure is fatal, not hammered", read_classifier(ValueError()) is Verdict.FATAL)

    async def hangs() -> str:
        await asyncio.sleep(5)
        return "never"

    check(
        "a hung attempt is cut off by the policy's timeout",
        await _araises(
            call_with_retry(
                hangs,
                policy=RetryPolicy(attempts=1, timeout_secs=0.05),
                classify=write_classifier,
                name="hangs",
            ),
            AmbiguousOutcomeError,
        ),
    )

    async def cancelled() -> str:
        raise asyncio.CancelledError

    check(
        "cancellation is never reclassified as a failure",
        await _araises(
            call_with_retry(cancelled, policy=READ_POLICY, classify=read_classifier, name="c"),
            asyncio.CancelledError,
        ),
    )

    async def boom() -> str:
        raise RuntimeError("the database went away")

    check("a guarded write returns its default instead of raising", await guarded(boom, default=False, name="write") is False)


async def check_duplicate_call_protection() -> None:
    """The heart of the phase: five mechanisms, each driven on its own."""
    print("\n=== duplicate call protection ===")

    check(
        "the idempotency key is derived, so two callers compute the same one",
        campaign_call_key(campaign_id=3, membership_id=12, attempt_number=2)
        == campaign_call_key(campaign_id=3, membership_id=12, attempt_number=2),
    )
    check(
        "and a different attempt is a different key",
        campaign_call_key(campaign_id=3, membership_id=12, attempt_number=2)
        != campaign_call_key(campaign_id=3, membership_id=12, attempt_number=3),
    )

    # 1. A live attempt blocks its prospect, and UNRESOLVED is live.
    store = FakeStore()
    store.add_attempt(id=1, status=CallAttemptStatus.UNRESOLVED)
    check("an unresolved attempt blocks the prospect", await store.has_live_attempt(7))
    check("and counts against concurrency", await store.count_live_attempts() == 1)
    check("UNRESOLVED is in the queue's live-status SQL", "UNRESOLVED" in LIVE_STATUS_SQL)

    # 2. An ambiguous placement holds rather than releases.
    store = FakeStore()
    service = FakeService(store)
    carrier = FlakyCarrier(place_error=ProviderUnavailableError("did not answer within 20s"))
    queued = queued_call(store)
    result = await make_dialer(service, carrier).dial(queued)

    check("a timed-out placement is asked exactly once", len(carrier.placed) == 1)
    check("and reported as ambiguous, not as a failure", result.ambiguous and not result.placed)
    check("the attempt is held UNRESOLVED", store.attempts[queued.attempt.id].status is CallAttemptStatus.UNRESOLVED)
    check("it is NOT released", not service.released)
    check("so the prospect is still blocked", await store.has_live_attempt(7))
    check("and the operator is told why", "UNRESOLVED" in result.describe() and "not be dialled again" in result.describe())

    # 3. A refusal is different: nothing was dialled, so the attempt is freed.
    store = FakeStore()
    service = FakeService(store)
    carrier = FlakyCarrier(place_error=CallSetupError("the To number is not verified (21219)"))
    queued = queued_call(store)
    result = await make_dialer(service, carrier).dial(queued)
    check("a carrier refusal is a plain failure", not result.ambiguous and result.error)
    check("the attempt is released", service.released and not store.attempts[queued.attempt.id].status.is_live)
    check("and the prospect is free again", not await store.has_live_attempt(7))

    # 4. Two carrier call ids can never land on one attempt.
    store = FakeStore()
    service = FakeService(store)
    carrier = FlakyCarrier()
    queued = queued_call(store, telephony_call_id="CAalready", status=CallAttemptStatus.QUEUED)
    result = await make_dialer(service, carrier).dial(queued)
    check("a second placement for a placed attempt is refused", result.error is not None and not result.placed)
    check("and the duplicate call is hung up", carrier.hung_up == ["CA0001"], str(carrier.hung_up))
    check("the original call id is untouched", store.attempts[queued.attempt.id].telephony_call_id == "CAalready")

    # 5. Placement is stamped before the request, so a crash is recoverable.
    store = FakeStore()
    service = FakeService(store)
    carrier = FlakyCarrier(place_error=TimeoutError("connection lost"))
    queued = queued_call(store)
    await make_dialer(service, carrier).dial(queued)
    check(
        "placement is stamped before the carrier is asked",
        store.attempts[queued.attempt.id].placement_started_at is not None,
    )


async def check_process_restart() -> None:
    """A restart resolves what was in flight, and never redials to find out."""
    print("\n=== process restart ===")

    # An attempt that was placed and whose watcher died.
    store = FakeStore()
    service = FakeService(store)
    attempt = store.add_attempt(
        id=1, status=CallAttemptStatus.CONNECTED, telephony_call_id="CA1", telephony_provider="flaky"
    )
    store.prospects[7] = Prospect(id=7, first_name="S", last_name="K", phone="+92300", phone_normalized="+92300")
    carrier = FlakyCarrier(fetch_status=CallStatus.COMPLETED)
    report = await AttemptRecovery(service, carrier, min_age_secs=0).run()  # type: ignore[arg-type]

    check("a placed call is reconciled from the carrier", report.resolved == 1)
    check("to the status the carrier reports", store.attempts[1].status is CallAttemptStatus.COMPLETED)
    check("and nothing was dialled", not carrier.placed)

    # A call the carrier says is still up must be left alone.
    store = FakeStore()
    service = FakeService(store)
    store.add_attempt(id=1, status=CallAttemptStatus.CONNECTED, telephony_call_id="CA1")
    carrier = FlakyCarrier(fetch_status=CallStatus.ANSWERED)
    report = await AttemptRecovery(service, carrier, min_age_secs=0).run()  # type: ignore[arg-type]
    check("a call still in progress is left alone", report.left == 1 and store.attempts[1].status is CallAttemptStatus.CONNECTED)

    # An ambiguous attempt where no call was ever created.
    store = FakeStore()
    service = FakeService(store)
    store.prospects[7] = Prospect(id=7, first_name="S", last_name="K", phone="+92300", phone_normalized="+92300")
    store.add_attempt(id=1, status=CallAttemptStatus.UNRESOLVED, placement_started_at=NOW)
    carrier = FlakyCarrier(recent=[])
    report = await AttemptRecovery(service, carrier, min_age_secs=0).run()  # type: ignore[arg-type]
    check("an ambiguous attempt is searched for at the carrier", carrier.searches == 1)
    check("no call found means it is closed as failed", report.no_call == 1 and store.attempts[1].status is CallAttemptStatus.FAILED)
    check("and still nothing was dialled", not carrier.placed)
    check("so the prospect is free for the campaign's own retry policy", not await store.has_live_attempt(7))

    # An ambiguous attempt where a call *was* created: adopt it, never redial.
    store = FakeStore()
    service = FakeService(store)
    store.prospects[7] = Prospect(id=7, first_name="S", last_name="K", phone="+92300", phone_normalized="+92300")
    store.add_attempt(id=1, status=CallAttemptStatus.UNRESOLVED, placement_started_at=NOW)
    carrier = FlakyCarrier(
        recent=[CallSnapshot(provider="flaky", call_id="CAghost", status=CallStatus.COMPLETED, created_at=NOW, duration_secs=30.0)]
    )
    report = await AttemptRecovery(service, carrier, min_age_secs=0).run()  # type: ignore[arg-type]
    check("a call the carrier does have is adopted", report.found_calls == 1)
    check("the attempt takes its call id", store.attempts[1].telephony_call_id == "CAghost")
    check("and its outcome", store.attempts[1].status is CallAttemptStatus.COMPLETED)
    check("nothing was dialled to find that out", not carrier.placed)

    # A call created *before* this attempt started must not be adopted.
    store = FakeStore()
    service = FakeService(store)
    store.prospects[7] = Prospect(id=7, first_name="S", last_name="K", phone="+92300", phone_normalized="+92300")
    store.add_attempt(id=1, status=CallAttemptStatus.UNRESOLVED, placement_started_at=NOW)
    carrier = FlakyCarrier(
        recent=[CallSnapshot(provider="flaky", call_id="COLD", status=CallStatus.COMPLETED, created_at=NOW - timedelta(hours=2))]
    )
    report = await AttemptRecovery(service, carrier, min_age_secs=0).run()  # type: ignore[arg-type]
    check("an older call to the same number is not adopted", report.no_call == 1 and store.attempts[1].telephony_call_id is None)

    # Reserved, never placed: released cleanly.
    store = FakeStore()
    service = FakeService(store)
    store.add_attempt(id=1, status=CallAttemptStatus.PENDING)
    report = await AttemptRecovery(service, FlakyCarrier(), min_age_secs=0).run()  # type: ignore[arg-type]
    check("an attempt that was never placed is released", report.released == 1)
    check("as a failure with a reason on the row", "never placed" in (store.attempts[1].failure_reason or ""))

    # A carrier that cannot list calls: an honest dead end, not a guess.
    store = FakeStore()
    service = FakeService(store)
    store.prospects[7] = Prospect(id=7, first_name="S", last_name="K", phone="+92300", phone_normalized="+92300")
    store.add_attempt(id=1, status=CallAttemptStatus.UNRESOLVED, placement_started_at=NOW)
    report = await AttemptRecovery(service, FlakyCarrier(can_list=False), min_age_secs=0).run()  # type: ignore[arg-type]
    check("a carrier that cannot list calls closes the attempt", store.attempts[1].status is CallAttemptStatus.FAILED)
    check("and says to check the carrier's log by hand", any("by hand" in note for note in report.notes), str(report.notes))

    # No carrier credentials at all.
    store = FakeStore()
    service = FakeService(store)
    store.add_attempt(id=1, status=CallAttemptStatus.UNRESOLVED, placement_started_at=NOW)
    report = await AttemptRecovery(service, None, min_age_secs=0).run()  # type: ignore[arg-type]
    check("with no carrier, an ambiguous attempt is still closed", store.attempts[1].status is CallAttemptStatus.FAILED)

    # A young attempt is a live call, not an abandoned one.
    store = FakeStore()
    service = FakeService(store)
    store.add_attempt(id=1, status=CallAttemptStatus.CONNECTED, telephony_call_id="CA1")
    carrier = FlakyCarrier()

    async def nothing_stale(*, older_than_secs=0.0, limit=100):
        return []

    store.list_live_attempts = nothing_stale  # type: ignore[assignment]
    report = await AttemptRecovery(service, carrier, min_age_secs=120).run()  # type: ignore[arg-type]
    check("a call in progress is not reconciled", report.total == 0 and carrier.fetches == 0)


async def check_duplicate_events() -> None:
    """A webhook or a poll delivered twice changes nothing."""
    print("\n=== duplicate webhooks and events ===")
    store = FakeStore()
    store.add_attempt(id=1, status=CallAttemptStatus.QUEUED, telephony_call_id="CA1")

    first = await store.apply_call_event(attempt_id=1, status=CallAttemptStatus.CONNECTED)
    second = await store.apply_call_event(attempt_id=1, status=CallAttemptStatus.CONNECTED)
    check("the first delivery is applied", first[1] is True)
    check("the second is not", second[1] is False)

    await store.apply_call_event(attempt_id=1, status=CallAttemptStatus.COMPLETED)
    late = await store.apply_call_event(attempt_id=1, status=CallAttemptStatus.CALLING)
    check("an out-of-order event does not walk it backwards", late[1] is False and store.attempts[1].status is CallAttemptStatus.COMPLETED)

    replay = await store.apply_call_event(attempt_id=1, status=CallAttemptStatus.COMPLETED)
    check("a final status is never rewritten", replay[1] is False)

    store.attempts[2] = _replace(store.attempts[1], id=2, status=CallAttemptStatus.DO_NOT_CALL, telephony_call_id="CA2")
    carrier_says = await store.apply_call_event(attempt_id=2, status=CallAttemptStatus.COMPLETED)
    check(
        "the carrier's 'completed' cannot flatten a do-not-call",
        carrier_says[1] is False and store.attempts[2].status is CallAttemptStatus.DO_NOT_CALL,
    )

    check("a webhook can address an attempt by call id alone", (await store.apply_call_event(telephony_call_id="CA1", status=CallAttemptStatus.BUSY))[0] is not None)
    check("an unknown call id is a no-op, not an error", (await store.apply_call_event(telephony_call_id="nope", status=CallAttemptStatus.BUSY)) == (None, False))

    # The dialer's own refresh path, twice.
    store = FakeStore()
    service = FakeService(store)
    attempt = store.add_attempt(id=1, status=CallAttemptStatus.QUEUED, telephony_call_id="CA1")
    carrier = FlakyCarrier(fetch_status=CallStatus.COMPLETED)
    dialer = make_dialer(service, carrier)
    await dialer.refresh(attempt)
    outcomes_after_one = len(service.outcomes)
    await dialer.refresh(store.attempts[1])
    check("refreshing twice records one outcome", len(service.outcomes) == outcomes_after_one == 1)
    check("and the second read still happened", carrier.fetches == 2)


async def check_carrier_failures() -> None:
    """Twilio down, refusing, or unreachable — each read differently."""
    print("\n=== carrier failure ===")
    store = FakeStore()
    service = FakeService(store)
    attempt = store.add_attempt(id=1, status=CallAttemptStatus.QUEUED, telephony_call_id="CA1")
    carrier = FlakyCarrier(fetch_error=ProviderUnavailableError("503 from the carrier"))
    result = await make_dialer(service, carrier).refresh(attempt)
    check("an unreadable call leaves the attempt alone", result is None and store.attempts[1].status is CallAttemptStatus.QUEUED)
    check("after retrying the read", carrier.fetches == READ_POLICY.attempts, f"{carrier.fetches} reads")

    check("placement never retries", NEVER_RETRY.attempts == 1)
    check("but it does time out", NEVER_RETRY.timeout_secs is not None)

    store = FakeStore()
    service = FakeService(store, callable_reason="the prospect is marked DO_NOT_CALL")
    carrier = FlakyCarrier()
    queued = queued_call(store)
    result = await make_dialer(service, carrier).dial(queued)
    check("a do-not-call landing mid-dial stops the call", not carrier.placed and result.error)


async def check_database_failure() -> None:
    """A database that has gone away does not take a call or a run down."""
    print("\n=== database unavailable ===")
    store = FakeStore(fail_with=CampaignStoreError("connection reset by peer"))
    service = FakeService(store)

    report = await AttemptRecovery(service, FlakyCarrier(), min_age_secs=0).run()  # type: ignore[arg-type]
    check("recovery reports rather than raises", report.total == 0 and report.notes)

    guards = CampaignGuards(window=_open_window(), pacing=PacingLimiter(0), max_concurrent=1)
    dialer = make_dialer(service, FlakyCarrier(), guards=guards)
    result = await dialer.dial_next(3)
    check("a dialer that cannot count live calls refuses to place one", result.blocked)
    # `result.blocked_by` is a refusing `Decision`, which is falsy — hence
    # `.refusal`, and hence the check being written with `is not None`.
    check("and says why", "cannot count live calls" in result.refusal)


async def check_campaign_safety() -> None:
    """Calling hours, concurrency, pacing, max attempts and the maximum call duration."""
    print("\n=== campaign safety ===")
    window = CallingWindow.parse("09:00-18:00", "mon-fri", "UTC")
    check("mid-morning on a weekday is allowed", bool(window.check(now=NOW)))
    check("four in the morning is not", not window.check(now=NOW.replace(hour=4)))
    check("and it says when it opens", window.check(now=NOW.replace(hour=4)).retry_after_secs == 5 * 3600)
    check("Sunday is not", not window.check(now=NOW - timedelta(days=1)))
    check("nor is one minute after closing", not window.check(now=NOW.replace(hour=18, minute=0)))
    check("one minute before closing is", bool(window.check(now=NOW.replace(hour=17, minute=59))))

    karachi = CallingWindow.parse("09:00-18:00", "mon-fri", "Asia/Karachi")
    check(
        "the window is judged in the prospect's timezone",
        not karachi.check(now=NOW.replace(hour=20)) and bool(karachi.check(now=NOW.replace(hour=6))),
    )
    check(
        "a prospect's own timezone overrides the default",
        not window.check(now=NOW.replace(hour=17), timezone="Asia/Karachi"),
    )
    check("enforcement can be turned off", bool(CallingWindow.parse("09:00-18:00", "mon-fri", "UTC", enabled=False).check(now=NOW.replace(hour=3))))
    check("a bad window is refused at parse time", _raises(lambda: CallingWindow.parse("18:00-09:00", "mon-fri"), ValueError))
    check("so is a bad timezone", _raises(lambda: CallingWindow.parse("09:00-18:00", "mon-fri", "Mars/Olympus"), ValueError))

    check("a timezone can be imported with a prospect", prospect_timezone({"timezone": "Asia/Karachi"}) == "Asia/Karachi")
    check("but is never inferred", prospect_timezone({"country": "PK"}) is None)
    check("and a bad one is ignored rather than read as UTC", prospect_timezone({"tz": "Mars/Olympus"}) is None)

    check("concurrency refuses at the limit", not check_concurrency(1, 1) and bool(check_concurrency(0, 1)))
    check("a limit of zero is no limit", bool(check_concurrency(99, 0)))

    ticks = [100.0]
    pacing = PacingLimiter(30.0, clock=lambda: ticks[0])
    check("the first call is unpaced", bool(pacing.check()))
    pacing.record_placement()
    check("the next is refused", not pacing.check())
    check("and says how long to wait", pacing.check().retry_after_secs == 30.0)
    ticks[0] += 30.0
    check("until the interval has passed", bool(pacing.check()))

    check("a call under the ceiling continues", bool(check_duration(100, 600)))
    check("one over it does not", not check_duration(601, 600))
    check("a ceiling of zero is no ceiling", bool(check_duration(10_000, 0)))

    # The guardrails are checked before a reservation is taken.
    store = FakeStore()
    service = FakeService(store)
    closed = CampaignGuards(
        window=CallingWindow.parse("09:00-18:00", "mon-fri", "UTC"),
        pacing=PacingLimiter(0),
        max_concurrent=1,
    )
    reserved = False

    async def should_not_reserve(campaign_id):
        nonlocal reserved
        reserved = True
        return None

    service.next_call = should_not_reserve  # type: ignore[assignment]
    closed.window = CallingWindow.parse("09:00-09:01", "mon-fri", "UTC")
    result = await make_dialer(service, FlakyCarrier(), guards=closed).dial_next(3)
    check("a closed window blocks before anything is reserved", result.blocked and not reserved)


async def check_supervisor() -> None:
    """STT/LLM/TTS failure, an LLM that stalls, and a call that runs too long."""
    print("\n=== in-call failures ===")
    check("a processor is mapped to its stage", (stage_of("DeepgramFluxSTTService#0"), stage_of("GroqLLMService#0"), stage_of("CartesiaTTSService#0")) == ("stt", "llm", "tts"))

    ended: list[str] = []
    spoken: list[str] = []
    noted: list[tuple[Reason, str]] = []

    def build(**kwargs: Any) -> SessionSupervisor:
        ended.clear()
        spoken.clear()
        noted.clear()
        return SessionSupervisor(
            end_session=_append(ended, "end"),
            cancel_session=_append(ended, "cancel"),
            say=_collect(spoken),
            on_terminated=lambda reason, detail: noted.append((reason, detail)),
            **kwargs,
        )

    supervisor = build(max_service_failures=3)
    await supervisor.note_error("stt", "websocket closed")
    await supervisor.note_error("stt", "websocket closed")
    check("two failures in a row do not end the call", not spoken and not ended)
    supervisor.note_success("stt")
    await supervisor.note_error("stt", "websocket closed")
    check("a success in between resets the count", not spoken and not ended)
    await supervisor.note_error("stt", "websocket closed")
    await supervisor.note_error("stt", "websocket closed")
    check("three in a row do close the call", supervisor.terminated_by is Reason.SERVICE_FAILURE)
    check("asking for a goodbye first, because the agent can still speak", spoken)
    check("and it is recorded on the call", noted and noted[0][0] is Reason.SERVICE_FAILURE)
    # The session ends when the goodbye has actually reached the caller — the
    # same signal the silence handler waits for, and for the same reason.
    check("but not before the goodbye has played", not ended)
    await supervisor.on_bot_stopped_speaking()
    check("and then it ends gracefully", ended == ["end"], str(ended))

    supervisor = build(max_service_failures=2)
    await supervisor.note_error("llm", "502 from the provider")
    await supervisor.note_error("llm", "502 from the provider")
    check("an LLM that is down ends the call at once, with no goodbye", ended == ["end"] and not spoken)

    # The bug the first live run of this class exposed: Pipecat pushes the
    # response-end frame from a `finally`, so it follows a failed request too.
    # Counting that as a success reset the failure count on every failure, and
    # an LLM refusing every request looked healthy for ever.
    # `note_speech` first in both, so what is being measured is the counter and
    # not the "never spoke" rule checked below.
    supervisor = build(max_service_failures=2)
    await supervisor.note_speech()
    for _ in range(2):
        supervisor.note_llm_started()
        await supervisor.note_error("llm", "429 rate limit reached")
        supervisor.note_llm_finished()  # No tokens were produced.
    check("a response that produced nothing does not count as a success", ended == ["end"])

    supervisor = build(max_service_failures=2)
    await supervisor.note_speech()
    for _ in range(3):
        supervisor.note_llm_started()
        await supervisor.note_error("llm", "one bad turn")
        supervisor.note_llm_output()
        supervisor.note_llm_finished()
    check("but a response that produced tokens does", not ended)

    supervisor = build(max_service_failures=0)
    for _ in range(10):
        await supervisor.note_error("tts", "down")
    check("the threshold can be turned off", not ended and not spoken)

    # The second bug the live run exposed: when the *greeting's* inference
    # fails, nothing triggers another one — the silence escalation is armed by
    # the agent finishing speaking — so the count never reaches the threshold
    # and the caller hears nothing until the idle timeout.
    supervisor = build(max_service_failures=5)
    await supervisor.note_error("llm", "429 on the greeting")
    check("one failure before the agent has ever spoken ends the call", ended == ["end"])
    check("and says so plainly", "NEVER SPOKE" in supervisor.describe())

    supervisor = build(max_service_failures=5)
    await supervisor.note_speech()
    await supervisor.note_error("llm", "429 mid-call")
    check("but the same failure mid-call is only a blip", not ended)
    check("and the summary no longer flags silence", "NEVER SPOKE" not in supervisor.describe())

    supervisor = build(max_service_failures=5)
    await supervisor.note_error("stt", "no transcript yet")
    check("an STT failure before the greeting is not fatal", not ended, "the agent can still speak")

    supervisor = build(max_call_secs=0.15, llm_stall_secs=0, goodbye_grace_secs=0.1)
    supervisor.start()
    await asyncio.sleep(1.3)
    check("a call over its ceiling is ended", ended and noted[0][0] is Reason.MAX_DURATION)
    check("after being asked to say goodbye", spoken)
    supervisor.stop()

    supervisor = build(max_call_secs=0, llm_stall_secs=0.15, goodbye_grace_secs=0.05)
    supervisor.start()
    supervisor.note_llm_started()
    await asyncio.sleep(1.3)
    check("an inference that never finishes ends the call", ended and noted[0][0] is Reason.LLM_STALLED)
    check("with no goodbye, since the model is what failed", not spoken)
    supervisor.stop()

    supervisor = build(max_call_secs=0, llm_stall_secs=0.15)
    supervisor.start()
    supervisor.note_llm_started()
    await asyncio.sleep(0.2)
    supervisor.note_llm_finished()
    await asyncio.sleep(1.2)
    check("an inference that finishes does not", not ended)
    supervisor.stop()

    # The goodbye that never arrives must not hold the line open.
    supervisor = build(max_service_failures=1, goodbye_grace_secs=0.1)
    await supervisor.note_error("stt", "down")
    check("a goodbye is requested", spoken and not ended)
    await asyncio.sleep(0.4)
    check("and the call ends anyway when it never plays", ended == ["cancel"], str(ended))

    supervisor = build(max_service_failures=1, goodbye_grace_secs=5.0)
    await supervisor.note_error("stt", "down")
    await supervisor.on_bot_stopped_speaking()
    check("a goodbye that does play ends it gracefully", ended == ["end"])
    await supervisor.on_bot_stopped_speaking()
    check("and only once", ended == ["end"])
    check("the summary says why the call ended", "service_failure" in supervisor.describe())


async def check_observability() -> None:
    """Structured fields, and credentials that never reach a log."""
    print("\n=== observability ===")
    os.environ["GROQ_API_KEY"] = "gsk_supersecretvalue1234567890"
    os.environ["TWILIO_AUTH_TOKEN"] = "aabbccddeeff00112233445566778899"
    count = install_scrubber()
    check("the scrubber loads the configured secrets", count >= 2, f"{count} value(s)")
    check("an API key is scrubbed", "gsk_supersecretvalue1234567890" not in redact("key is gsk_supersecretvalue1234567890"))
    check("a carrier token is scrubbed", "aabbccddeeff00112233445566778899" not in redact("token aabbccddeeff00112233445566778899"))
    check("an unknown bearer token is scrubbed by shape", "abcdefghijklmnop" not in redact("Authorization: Bearer abcdefghijklmnop"))
    check("so is a password in a DSN", "hunter2" not in redact("postgresql://user:hunter2@localhost/db"))
    check("ordinary text is untouched", redact("calling Sarah at Meridian") == "calling Sarah at Meridian")
    check("a short value is not treated as a secret", redact("a" * 4) == "a" * 4)

    from src.reliability.observability import CallContext, event

    fields = CallContext(campaign_id=3, prospect_id=7, attempt_id=11, call_id="CA1", provider="twilio").fields()
    check("the context carries the ids a call is followed by", fields == {"campaign": 3, "prospect": 7, "attempt": 11, "call": "CA1", "provider": "twilio"})
    check("and omits what is not known", CallContext(prospect_id=7).fields() == {"prospect": 7})
    line = event("call.placed", outcome="queued", latency_ms=412, error=None)
    check("an event renders as name and fields", line == "call.placed | outcome=queued latency_ms=412", line)
    check("an event scrubs its values too", "gsk_supersecretvalue1234567890" not in event("x", error="gsk_supersecretvalue1234567890"))


async def check_carrier_contract() -> None:
    """The carrier-side pieces Phase 9 added, against a stub HTTP session."""
    print("\n=== carrier contract ===")
    from tests.test_telephony import StubSession  # Reuses Phase 4's stub.

    from src.telephony.twilio import TwilioProvider

    from tests.test_telephony import StubResponse

    session = StubSession(
        [
            StubResponse(
                200,
                {
                    "calls": [
                        {"sid": "CAnew", "status": "completed", "to": "+92300", "date_created": "Mon, 07 Sep 2026 10:05:00 +0000"},
                        {"sid": "CAold", "status": "completed", "to": "+92300", "date_created": "Mon, 07 Sep 2026 08:00:00 +0000"},
                        {"sid": "CAodd", "status": "queued", "to": "+92300", "date_created": "not a date"},
                    ]
                },
            )
        ]
    )
    provider = TwilioProvider("AC1", "token", session=session)
    found = await provider.find_recent_calls("+92300", since=NOW)
    check("recent calls are filtered by when they were created", [c.call_id for c in found] == ["CAnew", "CAodd"], str([c.call_id for c in found]))
    check("an unreadable date keeps the call rather than dropping it", "CAodd" in [c.call_id for c in found])
    check("the number is passed as a query parameter", session.requests[-1][2]["params"].get("To") == "+92300")
    check("and every request is bounded by a timeout", session.requests[-1][2]["timeout"] is not None)

    session = StubSession([StubResponse(200, {"friendly_name": "My Project", "status": "active"})])
    provider = TwilioProvider("AC1", "token", session=session)
    check("the credential check reads the account", await provider.check_credentials() == "My Project (active)")
    check("from the account resource", session.requests[-1][1].endswith("/Accounts/AC1.json"), session.requests[-1][1])
    check("and it places no call", all(method == "GET" for method, _url, _kw in session.requests))

    session = StubSession([StubResponse(500, {"message": "internal error"})])
    provider = TwilioProvider("AC1", "token", session=session)
    check(
        "a carrier 5xx is marked retryable by the exception itself",
        await _araises(provider.fetch_call("CA1"), ProviderUnavailableError)
        and ProviderUnavailableError("x").retryable
        and not CallSetupError("x").retryable,
    )


def _open_window() -> CallingWindow:
    """A window that is always open, for checks that are not about hours."""
    return CallingWindow.parse("00:00-23:59", "mon-sun", "UTC")


def _record(into: list[float]):
    async def sleep(seconds: float) -> None:
        into.append(seconds)

    return sleep


def _append(into: list[str], value: str):
    async def action() -> None:
        into.append(value)

    return action


def _collect(into: list[str]):
    async def say(text: str) -> None:
        into.append(text)

    return say


def _raises(call, exception_type) -> bool:
    try:
        call()
    except exception_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def _araises(coroutine, exception_type) -> bool:
    try:
        await coroutine
    except exception_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def main() -> int:
    """Run every check and report."""
    print("Reliability checks — failures injected, nothing real is called.")

    await check_retry_policy()
    await check_duplicate_call_protection()
    await check_process_restart()
    await check_duplicate_events()
    await check_carrier_failures()
    await check_database_failure()
    await check_campaign_safety()
    await check_supervisor()
    await check_observability()
    await check_carrier_contract()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
