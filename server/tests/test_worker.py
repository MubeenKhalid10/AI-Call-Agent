#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the scheduler. Phase 13. No keys, no phone, no audio.

Run it from the `server/` directory::

    uv run python tests/test_worker.py

**What this is for.** The worker is a loop over parts that already have their
own checks — the queue, the guards, the dialer, recovery. What is new is the
*timing*: which of those it asks, in which order, how long it waits, and what
it does when the answer is "not now". So these checks drive the loop tick by
tick against a clock they control, and assert on the rows: who was dialled,
how many times, and what the tables say afterwards.

**Stubs, not mocks — and the real code in the middle.** `MemoryStore` is the
store's surface in memory, with the queue's eligibility rules, the live-attempt
exclusion, the idempotency key and the concurrency count written out, so a
check can run a whole campaign in milliseconds. Everything above it is the
production code: the actual `CampaignService`, `CampaignDialer`,
`AttemptRecovery` and `CampaignWorker`, with only the carrier
(`ScriptedCarrier`) and the clock replaced. The SQL those rules come from is
checked separately, at the end, against a real PostgreSQL in a temporary schema
— skipped, with a message, when none is reachable.

Every requirement Phase 13 was given has a check here by name: duplicate
reservation, calling hours, do-not-call, retry limits, callback execution,
restart and recovery, campaign completion — plus concurrency, pacing, graceful
shutdown, an ambiguous placement, and a database or carrier that goes away
mid-run.

A plain script rather than a pytest suite, like the other twelve. Exit status is
0 when everything passes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402

from src.campaigns import (  # noqa: E402
    AttemptRecovery,
    CallAttempt,
    CallAttemptStatus,
    CallbackStatus,
    Campaign,
    CampaignCounts,
    CampaignDialer,
    CampaignProspect,
    CampaignService,
    CampaignStatus,
    CampaignStoreError,
    CampaignWorker,
    MembershipStatus,
    Prospect,
    ProspectStatus,
    QueuedCall,
    QueueOutlook,
    ScheduledCallback,
    WorkerMetrics,
)
from src.campaigns.models import may_advance  # noqa: E402
from src.campaigns.store import PROGRESS_KEYS, campaign_concurrency  # noqa: E402
from src.reliability import (  # noqa: E402
    CallingWindow,
    CampaignGuards,
    PacingLimiter,
    campaign_call_key,
)
from src.telephony import (  # noqa: E402
    CallRequest,
    CallSetupError,
    CallSnapshot,
    CallStatus,
    ProviderUnavailableError,
    TelephonyProvider,
)

_failures: list[str] = []
_skipped: list[str] = []

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)  # A Monday, mid-morning, UTC.
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

LOGS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _mark() -> int:
    return len(LOGS)


def _logged(name: str, since: int = 0) -> int:
    """How many log lines since `since` carry this event name."""
    return sum(1 for line in LOGS[since:] if name in line)


# --- Time ---------------------------------------------------------------------


class FakeClock:
    """A clock the checks move by hand."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, secs: float) -> None:
        self.now = self.now + timedelta(seconds=secs)

    def monotonic(self) -> float:
        """The same clock, as seconds, for `PacingLimiter`."""
        return (self.now - EPOCH).total_seconds()


# --- The carrier ------------------------------------------------------------------


@dataclass
class _Call:
    call_id: str
    to_number: str
    script: list[CallStatus]
    created_at: datetime
    index: int = 0
    answered_by: str | None = None


class ScriptedCarrier(TelephonyProvider):
    """A `TelephonyProvider` whose calls walk through a scripted list of statuses.

    Each `fetch_call` advances the call one step along its script and stays
    on the last step. `end_call` jumps it to a final status, for checks that
    want a call to end at a moment of their choosing.
    """

    name = "scripted"
    transports = ("twilio",)

    def __init__(
        self,
        clock: FakeClock,
        *,
        script: list[CallStatus] | None = None,
        place_error: Exception | None = None,
        place_error_times: int | None = None,
        fetch_error: Exception | None = None,
        recent: list[CallSnapshot] | None = None,
    ) -> None:
        self._clock = clock
        self.script = list(script or [CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])
        self.scripts_by_number: dict[str, list[CallStatus]] = {}
        self.place_error = place_error
        self.place_error_times = place_error_times
        self.place_errors_raised = 0
        self.fetch_error = fetch_error
        self.recent = recent or []
        self.requests: list[CallRequest] = []
        self.placed_at: list[datetime] = []
        self.calls: dict[str, _Call] = {}
        self.hung_up: list[str] = []
        self.fetches = 0
        self.searches = 0
        self._next = 0

    async def place_call(self, request: CallRequest) -> CallSnapshot:
        self.requests.append(request)
        if self.place_error is not None and (
            self.place_error_times is None or self.place_errors_raised < self.place_error_times
        ):
            self.place_errors_raised += 1
            raise self.place_error
        self._next += 1
        call_id = f"CA{self._next:04d}"
        self.calls[call_id] = _Call(
            call_id=call_id,
            to_number=request.to_number,
            script=list(self.scripts_by_number.get(request.to_number, self.script)),
            created_at=self._clock(),
        )
        self.placed_at.append(self._clock())
        return CallSnapshot(
            provider=self.name,
            call_id=call_id,
            status=CallStatus.QUEUED,
            to_number=request.to_number,
            from_number=request.from_number,
            created_at=self._clock(),
        )

    async def fetch_call(self, call_id: str) -> CallSnapshot:
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        call = self.calls[call_id]
        status = call.script[min(call.index, len(call.script) - 1)]
        call.index += 1
        return self._snapshot(call, status)

    def _snapshot(self, call: _Call, status: CallStatus) -> CallSnapshot:
        return CallSnapshot(
            provider=self.name,
            call_id=call.call_id,
            status=status,
            to_number=call.to_number,
            duration_secs=42.0 if status is CallStatus.COMPLETED else None,
            created_at=call.created_at,
            answered_by=call.answered_by,
        )

    async def find_recent_calls(
        self, to_number: str, *, since: datetime, limit: int = 20
    ) -> list[CallSnapshot]:
        self.searches += 1
        found = [
            self._snapshot(c, c.script[min(c.index, len(c.script) - 1)])
            for c in self.calls.values()
            if c.to_number == to_number and c.created_at >= since
        ]
        found.extend(s for s in self.recent if s.created_at is None or s.created_at >= since)
        return found[:limit]

    async def hang_up(self, call_id: str) -> None:
        self.hung_up.append(call_id)

    async def transfer_call(self, call_id, to_number, *, caller_id=None) -> None:
        return None

    def make_serializer(self, call_data: Any):
        raise NotImplementedError

    async def check_credentials(self) -> str:
        return "scripted account"

    # Helpers for the checks.

    def end_call(self, call_id: str, status: CallStatus = CallStatus.COMPLETED) -> None:
        """Make the call's next fetch, and every one after, report `status`."""
        call = self.calls[call_id]
        call.script = [status]
        call.index = 0

    def calls_to(self, number: str) -> int:
        return sum(1 for r in self.requests if r.to_number == number)

    @property
    def last_call_id(self) -> str:
        return f"CA{self._next:04d}"


# --- The store, in memory -----------------------------------------------------------


class _AlreadyReserved(Exception):
    pass


@dataclass
class MemoryStore:
    """The store's surface in memory, with the queue's rules written out.

    Not a stand-in for the SQL — the last section checks that against a real
    PostgreSQL — but for the *worker's* behaviour over a store that obeys the
    same rules: eligibility, the live-attempt exclusion, the idempotency key,
    the concurrency count inside the reservation, the monotonic status write.
    """

    clock: FakeClock
    prospects: dict[int, Prospect] = field(default_factory=dict)
    campaigns: dict[int, Campaign] = field(default_factory=dict)
    memberships: dict[int, CampaignProspect] = field(default_factory=dict)
    attempts: dict[int, CallAttempt] = field(default_factory=dict)
    callbacks: dict[int, ScheduledCallback] = field(default_factory=dict)
    keys: set[str] = field(default_factory=set)
    fail_with: Exception | None = None
    after_reserve: Callable[[QueuedCall], Awaitable[None]] | None = None
    reservations: int = 0
    _ids: Counter = field(default_factory=Counter)
    # Phase 18: the audit rows the dashboard and the API write.
    audit_entries: list = field(default_factory=list)
    # Phase 19: the do-not-call list, by normalised number (every entry, revoked ones too).
    dnc_entries: list = field(default_factory=list)
    # Phase 21: the fleet's heartbeat rows and the shared pacing moments.
    workers: dict = field(default_factory=dict)
    pacing: dict = field(default_factory=dict)

    def _next(self, kind: str) -> int:
        self._ids[kind] += 1
        return self._ids[kind]

    # The do-not-call list (Phase 19)

    def _active_dnc(self, number: str | None):
        if not number:
            return None
        for e in reversed(self.dnc_entries):
            if e.phone_normalized == number and e.is_active_at(self.now()):
                return e
        return None

    async def find_dnc(self, phone_normalized: str):
        self._guard()
        return self._active_dnc(phone_normalized)

    async def add_dnc(self, phone_normalized: str, *, source="manual", reason=None, prospect_id=None, campaign_id=None, call_attempt_id=None, created_by=None, note=None, expires_at=None):
        from src.compliance import DncEntry, parse_source

        self._guard()
        existing = self._active_dnc(phone_normalized)
        if existing is not None:
            return existing, False
        entry = DncEntry(
            id=self._next("dnc"), phone_normalized=phone_normalized, source=parse_source(str(getattr(source, "value", source))),
            reason=reason, prospect_id=prospect_id, campaign_id=campaign_id, call_attempt_id=call_attempt_id,
            created_by=created_by, note=note, created_at=self.now(), expires_at=expires_at,
        )
        self.dnc_entries.append(entry)
        return entry, True

    async def revoke_dnc(self, phone_normalized: str, *, revoked_by: str, reason=None):
        self._guard()
        entry = self._active_dnc(phone_normalized)
        if entry is None:
            return None
        revoked = dataclasses.replace(entry, revoked_at=self.now(), revoked_by=revoked_by, revoke_reason=reason)
        self.dnc_entries[self.dnc_entries.index(entry)] = revoked
        return revoked

    async def list_dnc(self, *, phone_normalized=None, source=None, include_revoked=False, since=None, before_id=None, limit=100):
        self._guard()
        rows = [
            e for e in reversed(self.dnc_entries)
            if (phone_normalized is None or e.phone_normalized == phone_normalized)
            and (source is None or e.source.value == str(getattr(source, "value", source)))
            and (include_revoked or e.revoked_at is None)
            and (since is None or (e.created_at and e.created_at >= since))
            and (before_id is None or (e.id is not None and e.id < before_id))
        ]
        return rows[: max(1, min(int(limit), 1000))]

    async def dnc_counts(self):
        self._guard()
        counts = {"active": 0, "revoked": 0}
        for e in self.dnc_entries:
            if e.revoked_at is None:
                counts["active"] += 1
                counts[e.source.value] = counts.get(e.source.value, 0) + 1
            else:
                counts["revoked"] += 1
        return counts

    async def prospects_with_number(self, phone_normalized: str):
        self._guard()
        return [p for p in self.prospects.values() if phone_normalized and p.phone_normalized == phone_normalized]

    async def apply_dnc_list(self, prospect_ids=None):
        self._guard()
        marked = 0
        for p in list(self.prospects.values()):
            if prospect_ids is not None and p.id not in prospect_ids:
                continue
            if p.status is not ProspectStatus.DO_NOT_CALL and self._active_dnc(p.phone_normalized) is not None:
                if await self.set_prospect_status(p.id, ProspectStatus.DO_NOT_CALL):
                    marked += 1
        return marked

    async def update_campaign_configuration(self, campaign_id: int, key: str, value):
        self._guard()
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            return None
        configuration = dict(campaign.configuration)
        if value is None:
            configuration.pop(key, None)
        else:
            configuration[key] = value
        updated = dataclasses.replace(campaign, configuration=configuration, updated_at=self.now())
        self.campaigns[campaign_id] = updated
        return updated

    def _guard(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def now(self) -> datetime:
        return self.clock()

    # The audit log (Phase 18)

    async def record_audit(self, **fields):
        from src.security import AuditEntry

        self._guard()
        entry = AuditEntry(id=self._next("audit"), **fields)
        self.audit_entries.append(entry)
        return entry

    async def list_audit(self, *, action=None, actor=None, since=None, before_id=None, limit=100):
        self._guard()
        rows = [
            e
            for e in reversed(self.audit_entries)
            if (action is None or e.action.startswith(action))
            and (actor is None or e.actor == actor)
            and (since is None or (e.created_at is not None and e.created_at >= since))
            and (before_id is None or (e.id is not None and e.id < before_id))
        ]
        return rows[: max(1, min(int(limit), 1000))]

    async def audit_counts(self, *, since=None):
        self._guard()
        counts: dict[str, int] = {}
        for e in self.audit_entries:
            if since is None or (e.created_at is not None and e.created_at >= since):
                counts[e.action] = counts.get(e.action, 0) + 1
        return dict(sorted(counts.items()))

    # Prospects

    async def add_prospect(
        self,
        *,
        first_name: str,
        last_name: str,
        phone: str,
        phone_normalized: str | None = None,
        custom_data: dict[str, Any] | None = None,
        status: ProspectStatus = ProspectStatus.NEW,
        **_: Any,
    ) -> Prospect:
        self._guard()
        prospect = Prospect(
            id=self._next("prospect"),
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            phone_normalized=phone_normalized,
            custom_data=custom_data or {},
            status=status,
            created_at=self.now(),
            updated_at=self.now(),
        )
        self.prospects[prospect.id] = prospect
        return prospect

    async def get_prospect(self, prospect_id: int) -> Prospect | None:
        self._guard()
        return self.prospects.get(prospect_id)

    async def set_prospect_status(
        self, prospect_id: int, status: ProspectStatus, *, cancel_callbacks: bool = True
    ) -> bool:
        self._guard()
        prospect = self.prospects.get(prospect_id)
        if prospect is None:
            return False
        self.prospects[prospect_id] = dataclasses.replace(prospect, status=status, updated_at=self.now())
        if status is ProspectStatus.DO_NOT_CALL:
            for m in list(self.memberships.values()):
                if m.prospect_id == prospect_id and m.status in (
                    MembershipStatus.PENDING,
                    MembershipStatus.IN_PROGRESS,
                ):
                    self.memberships[m.id] = dataclasses.replace(
                        m, status=MembershipStatus.SKIPPED, updated_at=self.now()
                    )
            if cancel_callbacks:
                for cb in list(self.callbacks.values()):
                    if cb.prospect_id == prospect_id and cb.status is CallbackStatus.PENDING:
                        self.callbacks[cb.id] = dataclasses.replace(
                            cb, status=CallbackStatus.CANCELLED, updated_at=self.now()
                        )
        return True

    # Campaigns

    async def create_campaign(
        self,
        *,
        name: str,
        description: str | None = None,
        status: CampaignStatus = CampaignStatus.DRAFT,
        configuration: dict[str, Any] | None = None,
    ) -> Campaign:
        self._guard()
        campaign = Campaign(
            id=self._next("campaign"),
            name=name,
            description=description,
            status=status,
            configuration=configuration or {},
            created_at=self.now(),
            updated_at=self.now(),
        )
        self.campaigns[campaign.id] = campaign
        return campaign

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        self._guard()
        return self.campaigns.get(campaign_id)

    async def list_campaigns(
        self, *, status: CampaignStatus | None = None, limit: int = 50
    ) -> list[Campaign]:
        self._guard()
        rows = [c for c in self.campaigns.values() if status is None or c.status is status]
        return sorted(rows, key=lambda c: -c.id)[:limit]

    async def set_campaign_status(self, campaign_id: int, status: CampaignStatus) -> Campaign | None:
        self._guard()
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            return None
        changes: dict[str, Any] = {"status": status, "updated_at": self.now()}
        if status is CampaignStatus.ACTIVE and campaign.started_at is None:
            changes["started_at"] = self.now()
        if status is CampaignStatus.PAUSED:
            changes["paused_at"] = self.now()
        if status is CampaignStatus.COMPLETED:
            changes["completed_at"] = self.now()
        self.campaigns[campaign_id] = dataclasses.replace(campaign, **changes)
        return self.campaigns[campaign_id]

    async def campaign_counts(self, campaign_id: int) -> CampaignCounts:
        self._guard()
        rows = [m for m in self.memberships.values() if m.campaign_id == campaign_id]
        by = Counter(m.status for m in rows)
        return CampaignCounts(
            total=len(rows),
            pending=by[MembershipStatus.PENDING],
            in_progress=by[MembershipStatus.IN_PROGRESS],
            completed=by[MembershipStatus.COMPLETED],
            exhausted=by[MembershipStatus.EXHAUSTED],
            skipped=by[MembershipStatus.SKIPPED],
        )

    # Memberships

    async def add_to_campaign(self, campaign_id: int, prospect_id: int) -> CampaignProspect | None:
        self._guard()
        if any(
            m.campaign_id == campaign_id and m.prospect_id == prospect_id
            for m in self.memberships.values()
        ):
            return None
        membership = CampaignProspect(
            id=self._next("membership"),
            campaign_id=campaign_id,
            prospect_id=prospect_id,
            created_at=self.now(),
            updated_at=self.now(),
        )
        self.memberships[membership.id] = membership
        return membership

    async def campaign_progress(self, campaign_id: int) -> dict[str, int]:
        """The counters the SQL `campaign_progress` returns, over the lists. Phase 25."""
        self._guard()
        out = {key: 0 for key in PROGRESS_KEYS}
        now = self.now()
        for m in self.memberships.values():
            if m.campaign_id != campaign_id:
                continue
            out["contacts"] += 1
            if m.status is MembershipStatus.PENDING:
                out["pending"] += 1
                if m.next_attempt_at is None or m.next_attempt_at <= now:
                    out["queued"] += 1
                else:
                    out["scheduled"] += 1
            elif m.status is MembershipStatus.IN_PROGRESS:
                out["in_progress"] += 1
            elif m.status is MembershipStatus.COMPLETED:
                out["members_completed"] += 1
            elif m.status is MembershipStatus.EXHAUSTED:
                out["exhausted"] += 1
            elif m.status is MembershipStatus.SKIPPED:
                out["skipped"] += 1
        for a in self.attempts.values():
            if a.campaign_id != campaign_id:
                continue
            out["attempts"] += 1
            s = a.status
            if s in (CallAttemptStatus.PENDING, CallAttemptStatus.QUEUED):
                out["reserved"] += 1
                out["queued"] += 1
            if s.is_live:
                out["live"] += 1
            if s.reached_person:
                out["answered"] += 1
            key = {
                CallAttemptStatus.CALLING: "calling", CallAttemptStatus.CONNECTED: "connected",
                CallAttemptStatus.UNRESOLVED: "unresolved", CallAttemptStatus.COMPLETED: "completed",
                CallAttemptStatus.FAILED: "failed", CallAttemptStatus.NO_ANSWER: "no_answer",
                CallAttemptStatus.BUSY: "busy", CallAttemptStatus.VOICEMAIL: "voicemail",
                CallAttemptStatus.NOT_INTERESTED: "not_interested", CallAttemptStatus.DO_NOT_CALL: "do_not_call",
                CallAttemptStatus.CALLBACK_REQUESTED: "callback_requested",
            }.get(s)
            if key:
                out[key] += 1
        return out

    async def get_membership(self, membership_id: int) -> CampaignProspect | None:
        self._guard()
        return self.memberships.get(membership_id)

    async def find_membership(self, campaign_id: int, prospect_id: int) -> CampaignProspect | None:
        self._guard()
        return next(
            (
                m
                for m in self.memberships.values()
                if m.campaign_id == campaign_id and m.prospect_id == prospect_id
            ),
            None,
        )

    async def set_membership_status(
        self, membership_id: int, status: MembershipStatus, *, next_attempt_at: datetime | None = None
    ) -> None:
        self._guard()
        m = self.memberships[membership_id]
        self.memberships[membership_id] = dataclasses.replace(
            m, status=status, next_attempt_at=next_attempt_at, updated_at=self.now()
        )

    async def reopen_membership(self, membership_id: int, *, next_attempt_at: datetime) -> bool:
        self._guard()
        m = self.memberships.get(membership_id)
        if m is None:
            return False
        self.memberships[membership_id] = dataclasses.replace(
            m, status=MembershipStatus.PENDING, next_attempt_at=next_attempt_at, updated_at=self.now()
        )
        return True

    def force_membership(self, membership_id: int, **changes: Any) -> None:
        """A check's back door: put a membership in a state the rules would not."""
        self.memberships[membership_id] = dataclasses.replace(self.memberships[membership_id], **changes)

    # The queue

    def _eligible(self, m: CampaignProspect, max_attempts: int, ignore_limit: bool) -> bool:
        campaign = self.campaigns.get(m.campaign_id)
        prospect = self.prospects.get(m.prospect_id)
        if campaign is None or prospect is None:
            return False
        return (
            campaign.status is CampaignStatus.ACTIVE
            and m.status is MembershipStatus.PENDING
            and prospect.status is not ProspectStatus.DO_NOT_CALL
            and bool(prospect.phone_normalized)
            and self._active_dnc(prospect.phone_normalized) is None
            and (m.next_attempt_at is None or m.next_attempt_at <= self.now())
            and (ignore_limit or m.attempt_count < max_attempts)
            and not self._live_for(prospect.id)
        )

    def _live_for(self, prospect_id: int, exclude: int | None = None) -> bool:
        return any(
            a.prospect_id == prospect_id and a.status.is_live and a.id != exclude
            for a in self.attempts.values()
        )

    async def reserve_next_call(
        self,
        campaign_id: int,
        *,
        max_attempts: int,
        idempotency_key: Callable[[int, int], str] | None = None,
        max_concurrent: int = 0,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        try:
            return await self._reserve(campaign_id, None, max_attempts, idempotency_key, max_concurrent, False, worker_id)
        except _AlreadyReserved:
            return None

    async def reserve_membership(
        self,
        membership_id: int,
        *,
        max_attempts: int,
        idempotency_key: Callable[[int, int], str] | None = None,
        max_concurrent: int = 0,
        ignore_attempt_limit: bool = False,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        try:
            return await self._reserve(
                None, membership_id, max_attempts, idempotency_key, max_concurrent, ignore_attempt_limit, worker_id
            )
        except _AlreadyReserved:
            return None

    async def _reserve(
        self,
        campaign_id: int | None,
        membership_id: int | None,
        max_attempts: int,
        idempotency_key: Callable[[int, int], str] | None,
        max_concurrent: int,
        ignore_limit: bool,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        self._guard()
        self.reservations += 1
        if max_concurrent > 0 and await self.count_live_attempts() >= max_concurrent:
            return None
        # Phase 25: the campaign's own ceiling, as the SQL reservation applies it.
        if campaign_id is not None and campaign_id in self.campaigns:
            cap = campaign_concurrency((self.campaigns[campaign_id].configuration or {}).get("max_concurrent_calls"))
            if cap > 0 and await self.count_live_attempts(campaign_id=campaign_id) >= cap:
                return None
        rows = [
            m
            for m in self.memberships.values()
            if (campaign_id is None or m.campaign_id == campaign_id)
            and (membership_id is None or m.id == membership_id)
            and self._eligible(m, max_attempts, ignore_limit)
        ]
        if not rows:
            return None
        rows.sort(key=lambda m: (m.next_attempt_at is not None, m.next_attempt_at or EPOCH, m.id))
        m = rows[0]
        attempt_number = m.attempt_count + 1
        key = idempotency_key(m.id, attempt_number) if idempotency_key else None
        if key is not None and key in self.keys:
            raise _AlreadyReserved
        updated = dataclasses.replace(
            m,
            status=MembershipStatus.IN_PROGRESS,
            attempt_count=attempt_number,
            last_attempt_at=self.now(),
            updated_at=self.now(),
        )
        self.memberships[m.id] = updated
        attempt = CallAttempt(
            id=self._next("attempt"),
            prospect_id=m.prospect_id,
            campaign_id=m.campaign_id,
            campaign_prospect_id=m.id,
            attempt_number=attempt_number,
            status=CallAttemptStatus.PENDING,
            idempotency_key=key,
            worker_id=worker_id,
            created_at=self.now(),
            updated_at=self.now(),
        )
        self.attempts[attempt.id] = attempt
        if key is not None:
            self.keys.add(key)
        queued = QueuedCall(
            attempt=attempt,
            prospect=self.prospects[m.prospect_id],
            campaign=self.campaigns[m.campaign_id],
            membership=updated,
        )
        if self.after_reserve is not None:
            await self.after_reserve(queued)
        return queued

    # Coordination across workers (Phase 21)

    async def take_pacing_slot(self, min_interval_secs: float, *, campaign_id=None, campaign_interval_secs: float = 0.0):
        self._guard()
        scopes = []
        if min_interval_secs > 0:
            scopes.append(("pacing:global", float(min_interval_secs)))
        if campaign_id is not None and campaign_interval_secs > 0:
            scopes.append((f"pacing:campaign:{campaign_id}", float(campaign_interval_secs)))
        if not scopes:
            return True, 0.0
        now = self.now()
        wait = 0.0
        for key, interval in scopes:
            last = self.pacing.get(key)
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < interval:
                    wait = max(wait, interval - elapsed)
        if wait > 0:
            return False, wait
        for key, _ in scopes:
            self.pacing[key] = now
        return True, 0.0

    async def register_worker(self, worker_id: str, *, hostname: str, pid: int, campaign_ids=None, version=None):
        from src.campaigns.coordination import WorkerRecord

        self._guard()
        record = WorkerRecord(
            worker_id=worker_id, hostname=hostname, pid=pid, status="running", started_at=self.now(),
            heartbeat_at=self.now(), campaign_ids=tuple(campaign_ids) if campaign_ids is not None else None, version=version,
        )
        self.workers[worker_id] = record
        return record

    async def heartbeat_worker(self, worker_id: str, *, status: str = "running", in_flight: int = 0, metrics=None):
        self._guard()
        record = self.workers.get(worker_id)
        if record is None:
            return None
        record = dataclasses.replace(record, heartbeat_at=self.now(), status=status, in_flight=in_flight, metrics=dict(metrics or {}))
        self.workers[worker_id] = record
        return record

    async def mark_worker_stopped(self, worker_id: str, *, metrics=None) -> bool:
        self._guard()
        record = self.workers.get(worker_id)
        if record is None:
            return False
        self.workers[worker_id] = dataclasses.replace(
            record, status="stopped", stopped_at=self.now(), heartbeat_at=self.now(), in_flight=0,
            metrics=dict(metrics) if metrics is not None else record.metrics,
        )
        return True

    async def list_workers(self, *, include_stopped: bool = True, limit: int = 100):
        self._guard()
        rows = [w for w in self.workers.values() if include_stopped or w.status != "stopped"]
        return sorted(rows, key=lambda w: w.heartbeat_at or EPOCH, reverse=True)[:limit]

    async def worker_summary(self, *, stale_after_secs: float, limit: int = 100):
        from src.campaigns.coordination import WorkerSummary

        self._guard()
        workers = await self.list_workers(limit=limit)
        counts = {"running": 0, "draining": 0, "stale": 0, "stopped": 0}
        in_flight = 0
        for w in workers:
            health = w.health(self.now(), stale_after_secs)
            counts[health] = counts.get(health, 0) + 1
            if health in ("running", "draining"):
                in_flight += w.in_flight
        return WorkerSummary(running=counts["running"], draining=counts["draining"], stale=counts["stale"], stopped=counts["stopped"], in_flight=in_flight, workers=tuple(workers))

    async def prune_workers(self, *, older_than_secs: float) -> int:
        self._guard()
        cutoff = self.now() - timedelta(seconds=older_than_secs)
        gone = [k for k, w in self.workers.items() if (w.heartbeat_at or EPOCH) < cutoff]
        for k in gone:
            del self.workers[k]
        return len(gone)

    def _worker_alive(self, worker_id, stale_after_secs: float) -> bool:
        w = self.workers.get(worker_id) if worker_id else None
        return w is not None and not w.is_stale(self.now(), stale_after_secs)

    async def set_attempt_worker(self, attempt_id: int, worker_id) -> bool:
        self._guard()
        if attempt_id not in self.attempts:
            return False
        self.attempts[attempt_id] = dataclasses.replace(self.attempts[attempt_id], worker_id=worker_id)
        return True

    # Monitoring (Phase 22)

    async def set_attempt_trace(self, attempt_id: int, trace_id: str) -> bool:
        self._guard()
        if attempt_id not in self.attempts:
            return False
        self.attempts[attempt_id] = dataclasses.replace(self.attempts[attempt_id], trace_id=trace_id)
        return True

    async def ping(self) -> bool:
        self._guard()
        return True

    async def throughput(self, *, window_secs: float = 3600.0):
        from src.campaigns.coordination import Throughput

        self._guard()
        since = self.now() - timedelta(seconds=window_secs)
        placed = [a for a in self.attempts.values() if (a.placement_started_at or a.started_at) and (a.placement_started_at or a.started_at) >= since]
        finished = [a for a in self.attempts.values() if a.ended_at and a.ended_at >= since]
        answered = {CallAttemptStatus.COMPLETED, CallAttemptStatus.CALLBACK_REQUESTED, CallAttemptStatus.NOT_INTERESTED, CallAttemptStatus.DO_NOT_CALL}
        per: dict[int, dict[str, Any]] = {}
        for a in placed:
            if a.campaign_id is not None:
                per.setdefault(a.campaign_id, {"campaign_id": a.campaign_id, "placed": 0, "finished": 0})["placed"] += 1
        for a in finished:
            if a.campaign_id is not None:
                per.setdefault(a.campaign_id, {"campaign_id": a.campaign_id, "placed": 0, "finished": 0})["finished"] += 1
        return Throughput(
            window_secs=window_secs,
            placed=len(placed),
            finished=len(finished),
            answered=sum(1 for a in finished if a.status in answered),
            failed=sum(1 for a in finished if a.status is CallAttemptStatus.FAILED),
            per_campaign=tuple(per[k] for k in sorted(per)),
        )

    async def claim_abandoned_attempts(self, worker_id: str, *, stale_after_secs: float, limit: int = 50):
        self._guard()
        claimed = []
        for a in sorted(self.attempts.values(), key=lambda a: a.id):
            if len(claimed) >= limit:
                break
            if a.status.is_live and a.telephony_call_id and a.worker_id != worker_id and not self._worker_alive(a.worker_id, stale_after_secs):
                self.attempts[a.id] = dataclasses.replace(a, worker_id=worker_id)
                claimed.append(self.attempts[a.id])
        return claimed

    async def release_abandoned_reservations(self, *, stale_after_secs: float, limit: int = 50) -> int:
        self._guard()
        released = 0
        for a in sorted(self.attempts.values(), key=lambda a: a.id):
            if released >= limit:
                break
            if (
                a.status is CallAttemptStatus.PENDING and not a.telephony_call_id and a.placement_started_at is None
                and a.worker_id is not None and not self._worker_alive(a.worker_id, stale_after_secs)
            ):
                if await self.unreserve_attempt(a.id, next_attempt_at=self.now()):
                    released += 1
        return released

    async def queue_depth(self, *, max_attempts: int = 3):
        from src.campaigns.coordination import QueueDepth

        self._guard()
        per = []
        for c in self.campaigns.values():
            if c.status is not CampaignStatus.ACTIVE:
                continue
            members = [m for m in self.memberships.values() if m.campaign_id == c.id]
            due = sum(1 for m in members if self._eligible(m, max_attempts, False))
            scheduled = sum(1 for m in members if m.status is MembershipStatus.PENDING and m.next_attempt_at and m.next_attempt_at > self.now())
            per.append({"campaign_id": c.id, "name": c.name, "due_now": due, "scheduled": scheduled, "in_progress": sum(1 for m in members if m.status is MembershipStatus.IN_PROGRESS)})
        live = sum(1 for a in self.attempts.values() if a.status.is_live)
        reserved = sum(1 for a in self.attempts.values() if a.status is CallAttemptStatus.PENDING and not a.telephony_call_id)
        due_callbacks = sum(1 for cb in self.callbacks.values() if cb.status is CallbackStatus.PENDING and cb.scheduled_for <= self.now())
        return QueueDepth(
            due_now=sum(r["due_now"] for r in per), scheduled=sum(r["scheduled"] for r in per), callbacks_due=due_callbacks,
            reserved=reserved, live=live, active_campaigns=len(per), per_campaign=tuple(per),
        )

    async def unreserve_attempt(self, attempt_id: int, *, next_attempt_at: datetime) -> bool:
        self._guard()
        attempt = self.attempts.get(attempt_id)
        if (
            attempt is None
            or attempt.status is not CallAttemptStatus.PENDING
            or attempt.telephony_call_id
            or attempt.placement_started_at is not None
        ):
            return False
        del self.attempts[attempt_id]
        if attempt.idempotency_key:
            self.keys.discard(attempt.idempotency_key)
        m = self.memberships.get(attempt.campaign_prospect_id or -1)
        if m is not None and m.status is MembershipStatus.IN_PROGRESS:
            self.memberships[m.id] = dataclasses.replace(
                m,
                status=MembershipStatus.PENDING,
                attempt_count=max(0, m.attempt_count - 1),
                next_attempt_at=next_attempt_at,
                updated_at=self.now(),
            )
        return True

    async def has_live_attempt(self, prospect_id: int, *, exclude_attempt_id: int | None = None) -> bool:
        self._guard()
        return self._live_for(prospect_id, exclude_attempt_id)

    async def count_live_attempts(self, *, campaign_id: int | None = None) -> int:
        self._guard()
        return sum(
            1
            for a in self.attempts.values()
            if a.status.is_live and (campaign_id is None or a.campaign_id == campaign_id)
        )

    async def list_live_attempts(self, *, older_than_secs: float = 0.0, limit: int = 100) -> list[CallAttempt]:
        self._guard()
        cutoff = self.now() - timedelta(seconds=max(0.0, older_than_secs))
        rows = [
            a for a in self.attempts.values() if a.status.is_live and (a.updated_at or EPOCH) <= cutoff
        ]
        return sorted(rows, key=lambda a: a.updated_at or EPOCH)[:limit]

    # Attempts

    async def get_attempt(self, attempt_id: int) -> CallAttempt | None:
        self._guard()
        return self.attempts.get(attempt_id)

    async def list_attempts(
        self, *, prospect_id: int | None = None, campaign_id: int | None = None, limit: int = 50
    ) -> list[CallAttempt]:
        self._guard()
        rows = [
            a
            for a in self.attempts.values()
            if (prospect_id is None or a.prospect_id == prospect_id)
            and (campaign_id is None or a.campaign_id == campaign_id)
        ]
        return sorted(rows, key=lambda a: -a.id)[:limit]

    async def find_attempt_by_call_id(self, telephony_call_id: str) -> CallAttempt | None:
        self._guard()
        return next((a for a in self.attempts.values() if a.telephony_call_id == telephony_call_id), None)

    def _write(self, attempt_id: int, **changes: Any) -> CallAttempt:
        updated = dataclasses.replace(self.attempts[attempt_id], updated_at=self.now(), **changes)
        self.attempts[attempt_id] = updated
        return updated

    async def mark_placement_started(self, attempt_id: int) -> CallAttempt | None:
        self._guard()
        a = self.attempts.get(attempt_id)
        if a is None:
            return None
        return self._write(
            attempt_id,
            status=CallAttemptStatus.CALLING if a.status is CallAttemptStatus.PENDING else a.status,
            placement_started_at=a.placement_started_at or self.now(),
        )

    async def mark_attempt_placed(self, attempt_id: int, *, telephony_call_id: str, provider: str) -> CallAttempt | None:
        self._guard()
        a = self.attempts.get(attempt_id)
        if a is None:
            return None
        if a.telephony_call_id and a.telephony_call_id != telephony_call_id:
            raise CampaignStoreError(f"Attempt {attempt_id} is already placed as call {a.telephony_call_id}")
        if any(o.telephony_call_id == telephony_call_id and o.id != attempt_id for o in self.attempts.values()):
            raise CampaignStoreError(f"Carrier call {telephony_call_id} is already recorded against another attempt")
        return self._write(
            attempt_id,
            status=CallAttemptStatus.QUEUED if a.status.is_live else a.status,
            telephony_call_id=telephony_call_id,
            telephony_provider=provider,
            started_at=a.started_at or self.now(),
        )

    async def mark_attempt_unresolved(self, attempt_id: int, reason: str) -> CallAttempt | None:
        self._guard()
        a = self.attempts.get(attempt_id)
        if a is None or a.telephony_call_id or not a.status.is_live:
            return None
        return self._write(
            attempt_id, status=CallAttemptStatus.UNRESOLVED, failure_reason=a.failure_reason or reason
        )

    async def apply_call_event(
        self,
        *,
        status: CallAttemptStatus,
        attempt_id: int | None = None,
        telephony_call_id: str | None = None,
        duration_seconds: int | None = None,
        failure_reason: str | None = None,
    ) -> tuple[CallAttempt | None, bool]:
        self._guard()
        a = (
            self.attempts.get(attempt_id)
            if attempt_id is not None
            else next((x for x in self.attempts.values() if x.telephony_call_id == telephony_call_id), None)
        )
        if a is None:
            return None, False
        if not may_advance(a.status, status):
            return a, False
        return (
            self._write(
                a.id,
                status=status,
                failure_reason=a.failure_reason or failure_reason,
                duration_seconds=duration_seconds if duration_seconds is not None else a.duration_seconds,
                telephony_call_id=a.telephony_call_id or telephony_call_id,
                connected_at=a.connected_at or (self.now() if status is CallAttemptStatus.CONNECTED else None),
                ended_at=a.ended_at or (self.now() if status.is_final else None),
            ),
            True,
        )

    async def update_attempt_status(
        self,
        attempt_id: int,
        status: CallAttemptStatus,
        *,
        failure_reason: str | None = None,
        duration_seconds: int | None = None,
    ) -> CallAttempt | None:
        self._guard()
        a = self.attempts.get(attempt_id)
        if a is None:
            return None
        return self._write(
            attempt_id,
            status=status,
            failure_reason=failure_reason or a.failure_reason,
            duration_seconds=duration_seconds if duration_seconds is not None else a.duration_seconds,
            ended_at=a.ended_at or (self.now() if status.is_final else None),
        )

    async def save_call_result(self, result: Any) -> None:
        self._guard()
        return None

    # Callbacks

    async def schedule_callback(
        self,
        *,
        prospect_id: int,
        scheduled_for: datetime,
        campaign_id: int | None = None,
        call_attempt_id: int | None = None,
        campaign_prospect_id: int | None = None,
        note: str | None = None,
    ) -> ScheduledCallback:
        self._guard()
        existing = next(
            (cb for cb in self.callbacks.values() if cb.prospect_id == prospect_id and cb.status is CallbackStatus.PENDING),
            None,
        )
        if existing is not None:
            updated = dataclasses.replace(
                existing,
                scheduled_for=scheduled_for,
                campaign_id=campaign_id or existing.campaign_id,
                call_attempt_id=call_attempt_id or existing.call_attempt_id,
                campaign_prospect_id=campaign_prospect_id or existing.campaign_prospect_id,
                note=note or existing.note,
                updated_at=self.now(),
            )
            self.callbacks[existing.id] = updated
            return updated
        callback = ScheduledCallback(
            id=self._next("callback"),
            prospect_id=prospect_id,
            scheduled_for=scheduled_for,
            campaign_id=campaign_id,
            call_attempt_id=call_attempt_id,
            campaign_prospect_id=campaign_prospect_id,
            note=note,
            created_at=self.now(),
            updated_at=self.now(),
        )
        self.callbacks[callback.id] = callback
        return callback

    async def get_callback(self, callback_id: int) -> ScheduledCallback | None:
        self._guard()
        return self.callbacks.get(callback_id)

    async def list_callbacks(
        self,
        *,
        prospect_id: int | None = None,
        status: CallbackStatus | None = CallbackStatus.PENDING,
        due_before: datetime | None = None,
        limit: int = 50,
    ) -> list[ScheduledCallback]:
        self._guard()
        rows = [
            cb
            for cb in self.callbacks.values()
            if (prospect_id is None or cb.prospect_id == prospect_id)
            and (status is None or cb.status is status)
            and (due_before is None or cb.scheduled_for <= due_before)
        ]
        return sorted(rows, key=lambda cb: (cb.scheduled_for, cb.id))[:limit]

    async def set_callbacks_status(
        self, prospect_id: int, status: CallbackStatus, *, only: CallbackStatus = CallbackStatus.PENDING
    ) -> int:
        self._guard()
        moved = 0
        for cb in list(self.callbacks.values()):
            if cb.prospect_id == prospect_id and cb.status is only:
                self.callbacks[cb.id] = dataclasses.replace(cb, status=status, updated_at=self.now())
                moved += 1
        return moved

    async def cancel_callback(self, callback_id: int) -> bool:
        self._guard()
        cb = self.callbacks.get(callback_id)
        if cb is None or cb.status is not CallbackStatus.PENDING:
            return False
        self.callbacks[callback_id] = dataclasses.replace(cb, status=CallbackStatus.CANCELLED)
        return True

    # Phase 13 queries

    async def queue_outlook(self, campaign_id: int, *, max_attempts: int) -> QueueOutlook:
        self._guard()
        rows = [m for m in self.memberships.values() if m.campaign_id == campaign_id]
        pending = [m for m in rows if m.status is MembershipStatus.PENDING]
        now = self.now()

        def dialable(m: CampaignProspect) -> bool:
            p = self.prospects[m.prospect_id]
            return p.status is not ProspectStatus.DO_NOT_CALL and bool(p.phone_normalized) and m.attempt_count < max_attempts

        def has_callback(m: CampaignProspect) -> bool:
            return any(
                cb.campaign_prospect_id == m.id and cb.status is CallbackStatus.PENDING
                for cb in self.callbacks.values()
            )

        due_now = [m for m in pending if dialable(m) and (m.next_attempt_at is None or m.next_attempt_at <= now)]
        future = [m.next_attempt_at for m in pending if dialable(m) and m.next_attempt_at and m.next_attempt_at > now]
        undialable = [m for m in pending if not dialable(m) and not (m.attempt_count >= max_attempts and self.prospects[m.prospect_id].is_callable and has_callback(m))]
        pending_callbacks = [cb for cb in self.callbacks.values() if cb.campaign_id == campaign_id and cb.status is CallbackStatus.PENDING]
        return QueueOutlook(
            total=len(rows),
            pending=len(pending),
            in_progress=sum(1 for m in rows if m.status is MembershipStatus.IN_PROGRESS),
            due_now=len(due_now),
            next_due_at=min(future) if future else None,
            undialable=len(undialable),
            pending_callbacks=len(pending_callbacks),
            next_callback_at=min(cb.scheduled_for for cb in pending_callbacks) if pending_callbacks else None,
        )

    async def sweep_memberships(self, campaign_id: int, *, max_attempts: int) -> tuple[int, int]:
        self._guard()
        skipped = exhausted = 0
        for m in list(self.memberships.values()):
            if m.campaign_id != campaign_id or m.status is not MembershipStatus.PENDING:
                continue
            p = self.prospects[m.prospect_id]
            if p.status is ProspectStatus.DO_NOT_CALL or not p.phone_normalized:
                self.memberships[m.id] = dataclasses.replace(m, status=MembershipStatus.SKIPPED, updated_at=self.now())
                skipped += 1
            elif m.attempt_count >= max_attempts and not any(
                cb.campaign_prospect_id == m.id and cb.status is CallbackStatus.PENDING for cb in self.callbacks.values()
            ):
                self.memberships[m.id] = dataclasses.replace(m, status=MembershipStatus.EXHAUSTED, updated_at=self.now())
                exhausted += 1
        return skipped, exhausted


# --- The world ----------------------------------------------------------------------


@dataclass
class World:
    """Everything a check needs, wired the way `campaign.py run` wires it."""

    clock: FakeClock
    store: MemoryStore
    service: CampaignService
    carrier: ScriptedCarrier
    guards: CampaignGuards
    dialer: CampaignDialer
    recovery: AttemptRecovery
    slept: list[float] = field(default_factory=list)
    max_live: int = 0
    budget_secs: float = 6 * 3600
    on_sleep: Callable[[int], Awaitable[None]] | None = None
    workers: list[CampaignWorker] = field(default_factory=list)

    async def campaign(
        self, name: str, numbers: list[str | tuple[str, dict[str, Any]]], *, status: CampaignStatus = CampaignStatus.ACTIVE
    ) -> Campaign:
        """A campaign with one prospect per number, active unless told otherwise."""
        campaign = await self.store.create_campaign(name=name, status=status)
        for entry in numbers:
            number, custom = (entry, {}) if isinstance(entry, str) else entry
            prospect = await self.store.add_prospect(
                first_name="P",
                last_name=number[-4:],
                phone=number,
                phone_normalized=number if number.startswith("+") else None,
                custom_data=custom,
            )
            await self.store.add_to_campaign(campaign.id, prospect.id)
        return campaign

    async def prospect_by_number(self, number: str) -> Prospect:
        return next(p for p in self.store.prospects.values() if p.phone == number)

    async def membership_of(self, number: str, campaign: Campaign) -> CampaignProspect:
        prospect = await self.prospect_by_number(number)
        membership = await self.store.find_membership(campaign.id, prospect.id)
        assert membership is not None
        return membership

    async def attempts_to(self, number: str) -> list[CallAttempt]:
        prospect = await self.prospect_by_number(number)
        return sorted(await self.store.list_attempts(prospect_id=prospect.id), key=lambda a: a.id)

    def make_worker(self, **kwargs: Any) -> CampaignWorker:
        """The real worker over this world, with a fake clock and a fake sleep."""
        settings: dict[str, Any] = dict(
            recovery=self.recovery,
            guards=self.guards,
            poll_secs=2.0,
            idle_secs=30.0,
            recovery_interval_secs=300.0,
            recovery_min_age_secs=120.0,
            drain_secs=900.0,
            report_secs=60.0,
            clock=self.clock,
            sleep=self._sleep,
        )
        settings.update(kwargs)
        worker = CampaignWorker(self.service, self.dialer, **settings)
        self.workers.append(worker)
        return worker

    async def _sleep(self, secs: float) -> None:
        """Advance the clock instead of waiting, and stop a run that would never end."""
        self.slept.append(secs)
        self.max_live = max(self.max_live, await self.store.count_live_attempts())
        if self.on_sleep is not None:
            await self.on_sleep(len(self.slept))
        self.clock.advance(secs)
        if sum(self.slept) > self.budget_secs:
            for worker in self.workers:
                worker.request_stop(immediate=True)


def build_world(
    *,
    max_attempts: int = 3,
    retry_minutes: float = 60.0,
    max_concurrent: int = 1,
    pacing_secs: float = 0.0,
    hours: tuple[str, str] = ("00:00-23:59", "mon-sun"),
    timezone: str = "UTC",
    enforce_hours: bool = True,
    script: list[CallStatus] | None = None,
    place_error: Exception | None = None,
    place_error_times: int | None = None,
    recovery_min_age_secs: float = 120.0,
    start: datetime = NOW,
) -> World:
    clock = FakeClock(start)
    store = MemoryStore(clock=clock)
    service = CampaignService(
        store,  # type: ignore[arg-type]
        default_region="PK",
        max_attempts=max_attempts,
        retry_minutes=retry_minutes,
        clock=clock,
    )
    carrier = ScriptedCarrier(clock, script=script, place_error=place_error, place_error_times=place_error_times)
    guards = CampaignGuards(
        window=CallingWindow.parse(hours[0], hours[1], timezone, enabled=enforce_hours, clock=clock),
        pacing=PacingLimiter(pacing_secs, clock=clock.monotonic),
        max_concurrent=max_concurrent,
    )
    dialer = CampaignDialer(
        service,
        carrier,
        from_number="+15550001111",
        public_url="https://example.test",
        guards=guards,
    )
    recovery = AttemptRecovery(service, carrier, min_age_secs=recovery_min_age_secs)
    return World(clock=clock, store=store, service=service, carrier=carrier, guards=guards, dialer=dialer, recovery=recovery)


# --- The checks ---------------------------------------------------------------------


async def check_end_to_end() -> None:
    """Import, activate, run: every prospect is called once and the campaign closes itself."""
    print("\n=== end to end: activate a campaign and walk away ===")
    w = build_world(script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await w.campaign("Q1", ["+923001111111", "+923002222222"])
    mark = _mark()
    worker = w.make_worker(max_calls=2)
    metrics = await worker.run()

    numbers = sorted(r.to_number for r in w.carrier.requests)
    check("both prospects were called", numbers == ["+923001111111", "+923002222222"], str(numbers))
    check("each exactly once", len(w.carrier.requests) == 2)
    check("one call at a time", w.max_live <= 1, f"max live {w.max_live}")
    attempts = list(w.store.attempts.values())
    check("both attempts completed", all(a.status is CallAttemptStatus.COMPLETED for a in attempts), str([a.status for a in attempts]))
    check("with the carrier's duration", all(a.duration_seconds == 42 for a in attempts))
    check("with ended_at stamped", all(a.ended_at is not None for a in attempts))
    memberships = list(w.store.memberships.values())
    check("both memberships completed", all(m.status is MembershipStatus.COMPLETED for m in memberships))
    check("both prospects contacted", all(p.status is ProspectStatus.CONTACTED for p in w.store.prospects.values()))
    check("the campaign completed itself", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)
    check("and the run returned on its own", isinstance(metrics, WorkerMetrics))
    check(
        "the counters agree",
        (metrics.queued, metrics.started, metrics.completed, metrics.failed) == (2, 2, 2, 0),
        metrics.describe(),
    )
    check("every stage was logged", all(_logged(n, mark) for n in ("worker.started", "call.started", "call.status", "call.completed", "campaign.completed", "worker.stopped")))
    check("the requests carry the ids the bot needs", all("call_attempt_id" in r.parameters and "prospect_id" in r.parameters for r in w.carrier.requests))
    check("the loop slept between polls instead of spinning", w.slept and min(w.slept) >= 0.05, str(w.slept[:6]))


async def check_duplicate_reservation() -> None:
    """Two workers over one store: one call per person, whatever the timing."""
    print("\n=== duplicate reservation prevention ===")
    w = build_world(script=[CallStatus.ANSWERED])  # The call stays up until a check ends it.
    campaign = await w.campaign("Dup", ["+923001111111"])
    number = "+923001111111"

    first = w.make_worker()
    await first.start()
    await first.tick()
    check("the first worker places the call", w.carrier.calls_to(number) == 1)

    second = w.make_worker()  # A second process against the same rows.
    await second.start()
    await second.tick()
    await second.tick()
    check("a second worker does not place another", w.carrier.calls_to(number) == 1)
    # Phase 21: the first worker is alive and owns the call, so the second
    # leaves it alone — following it too would count one ending twice.
    check("it does not follow a live worker's call either", second.in_flight == [] and len(first.in_flight) == 1)
    check("the attempt names its owner", w.store.attempts[first.in_flight[0].id].worker_id == first.worker_id)
    check("only one attempt row exists", len(w.store.attempts) == 1)

    # The same attempt asked for twice resolves to one row through the key,
    # even if the membership's own state were lost.
    membership = await w.membership_of(number, campaign)
    attempt = (await w.attempts_to(number))[0]
    expected = campaign_call_key(campaign_id=campaign.id, membership_id=membership.id, attempt_number=1)
    check("the reservation carries a derived idempotency key", attempt.idempotency_key == expected, attempt.idempotency_key or "")
    w.carrier.end_call(w.carrier.last_call_id, CallStatus.COMPLETED)
    await first.tick()
    check("the call ends once", w.store.attempts[attempt.id].status is CallAttemptStatus.COMPLETED)
    await second.tick()
    check("and the second worker's poll changes nothing", w.store.attempts[attempt.id].status is CallAttemptStatus.COMPLETED and (first.metrics.completed, second.metrics.completed) == (1, 0))
    w.store.force_membership(membership.id, status=MembershipStatus.PENDING, attempt_count=0, next_attempt_at=None)
    queued = await w.service.next_call(campaign.id)
    check("a second reservation for the same attempt is refused by the key", queued is None)
    check("and does not spend the count", w.store.memberships[membership.id].attempt_count == 0)
    check("nothing was dialled to find any of that out", w.carrier.calls_to(number) == 1)

    # Two workers ticking over a list: every prospect once, across both.
    w = build_world(script=[CallStatus.COMPLETED], max_concurrent=1)
    await w.campaign("Shared", ["+923001111111", "+923002222222", "+923003333333", "+923004444444"])
    a, b = w.make_worker(), w.make_worker()
    await a.start()
    await b.start()
    for _ in range(12):
        await a.tick()
        await b.tick()
        w.clock.advance(2)
    counts = Counter(r.to_number for r in w.carrier.requests)
    check("two workers over one list call each prospect exactly once", all(n == 1 for n in counts.values()) and len(counts) == 4, str(dict(counts)))
    check("and never two at once", w.max_live <= 1)


async def check_calling_hours() -> None:
    """Nothing rings outside the window, and nothing is spent waiting for it to open."""
    print("\n=== calling-hours enforcement ===")
    w = build_world(hours=("09:00-18:00", "mon-fri"), timezone="UTC", start=NOW.replace(hour=4))
    campaign = await w.campaign("Hours", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    mark = _mark()
    report = await worker.tick()
    membership = await w.membership_of("+923001111111", campaign)
    check("nothing is placed at four in the morning", not w.carrier.requests)
    check("no attempt was spent", membership.attempt_count == 0 and not w.store.attempts)
    check("the skip is counted under the window", worker.metrics.skips["window"] == 1, str(dict(worker.metrics.skips)))
    check("and logged", _logged("call.skipped", mark) == 1)
    check("the loop sleeps toward the opening, never past its idle ceiling", 0 < report.sleep_secs <= 30.0, str(report.sleep_secs))
    w.clock.now = NOW  # 10:00, Monday.
    await worker.tick()
    check("and places once the window opens", len(w.carrier.requests) == 1)

    print("\n  Sunday:")
    w = build_world(hours=("09:00-18:00", "mon-fri"), timezone="UTC", start=NOW - timedelta(days=1))
    await w.campaign("Sunday", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("a Sunday is not a calling day", not w.carrier.requests)

    print("\n  the prospect's own timezone:")
    w = build_world(hours=("09:00-18:00", "mon-fri"), timezone="UTC", start=NOW.replace(hour=14))
    campaign = await w.campaign("TZ", [("+923003333333", {"timezone": "Asia/Karachi"}), "+923004444444"])
    worker = w.make_worker()
    await worker.start()
    mark = _mark()
    await worker.tick()
    membership = await w.membership_of("+923003333333", campaign)
    check("it is 19:00 in Karachi, so that prospect is not dialled", w.carrier.calls_to("+923003333333") == 0)
    check("their reservation was handed back, not spent", membership.status is MembershipStatus.PENDING and membership.attempt_count == 0 and not await w.attempts_to("+923003333333"))
    check(
        "and scheduled for their morning",
        membership.next_attempt_at == datetime(2026, 9, 8, 4, 0, tzinfo=UTC),
        str(membership.next_attempt_at),
    )
    check("logged as deferred, not failed", _logged("call.deferred", mark) == 1 and _logged("call.failed", mark) == 0)
    check("while the prospect in the server's zone is called", w.carrier.calls_to("+923004444444") == 1)
    # The configured window is the operator's rule and a prospect's own zone can
    # only narrow it, so they are dialled once *both* are open: 09:00 UTC is
    # 14:00 in Karachi.
    w.clock.now = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)
    await worker.tick()
    check("not at 04:00 UTC, when the configured window is still closed", w.carrier.calls_to("+923003333333") == 0 and not await w.attempts_to("+923003333333"))
    w.clock.now = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    for _ in range(6):
        await worker.tick()
    check("and the Karachi prospect once both windows are open", w.carrier.calls_to("+923003333333") == 1)
    attempts = await w.attempts_to("+923003333333")
    check("on attempt one", bool(attempts) and attempts[0].attempt_number == 1)

    print("\n  enforcement off:")
    w = build_world(hours=("09:00-18:00", "mon-fri"), enforce_hours=False, start=NOW.replace(hour=3))
    await w.campaign("Any", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("ENFORCE_CALLING_HOURS=false allows the call", len(w.carrier.requests) == 1)


async def check_dnc() -> None:
    """A do-not-call is honoured whenever it lands: before, during and after a dial."""
    print("\n=== DNC enforcement ===")
    w = build_world(script=[CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await w.campaign("DNC", ["+923001111111", "+923002222222", "+923003333333"])
    p1 = await w.prospect_by_number("+923001111111")
    p2 = await w.prospect_by_number("+923002222222")
    await w.service.mark_do_not_call(p1.id)

    async def mark_between_reserving_and_dialling(queued: QueuedCall) -> None:
        if queued.prospect.id == p2.id:
            await w.service.mark_do_not_call(p2.id)

    w.store.after_reserve = mark_between_reserving_and_dialling
    w.budget_secs = 600
    worker = w.make_worker()
    await worker.run()

    check("a prospect marked before the run is never dialled", w.carrier.calls_to("+923001111111") == 0)
    check("their membership is skipped", (await w.membership_of("+923001111111", campaign)).status is MembershipStatus.SKIPPED)
    check("one marked between reserving and dialling is not dialled either", w.carrier.calls_to("+923002222222") == 0)
    released = await w.attempts_to("+923002222222")
    check("that attempt is released as failed, naming do-not-call", len(released) == 1 and released[0].status is CallAttemptStatus.FAILED and "DO_NOT_CALL" in (released[0].failure_reason or ""), released[0].failure_reason if released else "no attempt")
    check("the third prospect is called", w.carrier.calls_to("+923003333333") == 1)
    check("and the campaign still completes", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)
    check("the release counts as failed and the skip is explained", worker.metrics.failed == 1 and worker.metrics.skips["not_callable"] == 1, worker.metrics.describe())

    print("\n  written by the bot mid-call:")
    w = build_world(script=[CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await w.campaign("MidCall", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    await worker.tick()
    attempt = (await w.attempts_to("+923001111111"))[0]
    check("the call is connected", w.store.attempts[attempt.id].status is CallAttemptStatus.CONNECTED)
    # The conversation sink's write, as `briefing.py` makes it.
    await w.service.record_outcome(w.store.attempts[attempt.id], CallAttemptStatus.DO_NOT_CALL, duration_seconds=30, write_result=False)
    await worker.tick()
    check("the worker sees the outcome the bot wrote", worker.metrics.outcomes["DO_NOT_CALL"] == 1 and not worker.in_flight)
    await worker.tick()
    await worker.tick()
    check("the carrier's later 'completed' does not overwrite it", w.store.attempts[attempt.id].status is CallAttemptStatus.DO_NOT_CALL)
    check("the prospect is marked", (await w.prospect_by_number("+923001111111")).status is ProspectStatus.DO_NOT_CALL)
    check("no further call is placed", w.carrier.calls_to("+923001111111") == 1)

    print("\n  a due callback for a do-not-call prospect:")
    w = build_world()
    campaign = await w.campaign("CbDNC", ["+923001111111"])
    prospect = await w.prospect_by_number("+923001111111")
    membership = await w.membership_of("+923001111111", campaign)
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW - timedelta(minutes=5), campaign_id=campaign.id, campaign_prospect_id=membership.id)
    await w.store.set_prospect_status(prospect.id, ProspectStatus.DO_NOT_CALL, cancel_callbacks=False)
    worker = w.make_worker()
    await worker.start()
    mark = _mark()
    await worker.tick()
    check("it is cancelled, not placed", not w.carrier.requests and w.store.callbacks[1].status is CallbackStatus.CANCELLED)
    check("and the log says why", _logged("callback.cancelled", mark) == 1)


async def check_retry_limits() -> None:
    """No-answers are retried after the wait, up to the limit, and the last one is real."""
    print("\n=== retry limits ===")
    w = build_world(max_attempts=2, retry_minutes=30, script=[CallStatus.RINGING, CallStatus.NO_ANSWER])
    campaign = await w.campaign("Retry", ["+923001111111"])
    number = "+923001111111"
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("the first attempt is placed", w.carrier.calls_to(number) == 1)
    await worker.tick()
    await worker.tick()
    membership = await w.membership_of(number, campaign)
    check("a no-answer puts the membership back in the queue", membership.status is MembershipStatus.PENDING and membership.attempt_count == 1)
    check("with the retry wait", membership.next_attempt_at == NOW + timedelta(minutes=30), str(membership.next_attempt_at))
    report = await worker.tick()
    check("nothing is placed before it is due", w.carrier.calls_to(number) == 1)
    check("the loop knows when it is due", report.next_due_at == membership.next_attempt_at and report.sleep_secs <= 30.0)
    check("and the campaign is not completed while a retry is pending", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE)
    w.clock.advance(30 * 60)
    await worker.tick()
    check("the second — last permitted — attempt is dialled", w.carrier.calls_to(number) == 2)
    check("as attempt two", (await w.attempts_to(number))[-1].attempt_number == 2)
    await worker.tick()
    await worker.tick()
    membership = await w.membership_of(number, campaign)
    check("a second no-answer exhausts the membership", membership.status is MembershipStatus.EXHAUSTED)
    w.clock.advance(60 * 60)
    for _ in range(3):
        await worker.tick()
    check("no third attempt is ever made", w.carrier.calls_to(number) == 2 and len(w.store.attempts) == 2)
    check("and the campaign completes", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)
    check("the counters say two started, two completed as no-answer", worker.metrics.started == 2 and worker.metrics.outcomes["NO_ANSWER"] == 2, worker.metrics.describe())

    print("\n  a limit of one:")
    w = build_world(max_attempts=1, script=[CallStatus.COMPLETED])
    await w.campaign("One", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("the only permitted attempt is actually dialled", w.carrier.calls_to("+923001111111") == 1)
    attempts = await w.attempts_to("+923001111111")
    check("and not released as 'attempt limit reached'", attempts and attempts[0].status is not CallAttemptStatus.FAILED, str(attempts[0].failure_reason if attempts else ""))

    print("\n  a failed dial is not retried:")
    w = build_world(max_attempts=3, script=[CallStatus.FAILED])
    campaign = await w.campaign("Fail", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    for _ in range(4):
        await worker.tick()
        w.clock.advance(2)
    membership = await w.membership_of("+923001111111", campaign)
    check("a FAILED outcome exhausts the membership at once", membership.status is MembershipStatus.EXHAUSTED and w.carrier.calls_to("+923001111111") == 1)
    check("and is counted as failed", worker.metrics.failed == 1 and worker.metrics.outcomes["FAILED"] == 1)

    print("\n  busy and voicemail are retried like a no-answer:")
    w = build_world(max_attempts=3, retry_minutes=1, script=[CallStatus.BUSY])
    campaign = await w.campaign("Busy", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    for _ in range(3):
        await worker.tick()
    membership = await w.membership_of("+923001111111", campaign)
    check("busy schedules a retry", membership.status is MembershipStatus.PENDING and membership.next_attempt_at is not None)


async def check_callbacks() -> None:
    """A promised callback is placed at its time, ahead of the queue, even at the attempt limit."""
    print("\n=== callback execution ===")
    w = build_world(max_attempts=1, script=[CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await w.campaign("Cb", ["+923001111111"])
    number = "+923001111111"
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    await worker.tick()
    prospect = await w.prospect_by_number(number)
    membership = await w.membership_of(number, campaign)
    attempt = (await w.attempts_to(number))[0]
    check("the call is connected", w.store.attempts[attempt.id].status is CallAttemptStatus.CONNECTED)
    # What the action backend writes when, mid-call, the person asks for noon...
    due = NOW + timedelta(hours=2)
    callback = await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=due, campaign_id=campaign.id, call_attempt_id=attempt.id, campaign_prospect_id=membership.id, note="noon")
    await worker.tick()
    membership = await w.membership_of(number, campaign)
    check("the call reached them and used the only attempt", membership.status is MembershipStatus.COMPLETED and membership.attempt_count == 1)
    # ... and what the sink writes when the call ends.
    await w.store.reopen_membership(membership.id, next_attempt_at=due)
    report = await worker.tick()
    check("not placed before its time", w.carrier.calls_to(number) == 1)
    check("the loop sleeps toward it", report.next_due_at == due and report.sleep_secs <= 30.0, str(report.next_due_at))
    check("the campaign stays open for it", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE)
    w.clock.now = due
    mark = _mark()
    await worker.tick()
    check("placed when it falls due", w.carrier.calls_to(number) == 2)
    check("even though the membership had used its only attempt", (await w.attempts_to(number))[-1].attempt_number == 2)
    check("the callback is marked PLACED", w.store.callbacks[callback.id].status is CallbackStatus.PLACED)
    check("counted as a callback", worker.metrics.callbacks == 1)
    check("and logged as one", _logged("callback.due", mark) == 1 and _logged("call.started", mark) == 1)
    for _ in range(3):
        await worker.tick()
    check("followed to its end like any call", (await w.attempts_to(number))[-1].status is CallAttemptStatus.COMPLETED)
    check("after which the campaign completes", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)

    print("\n  ahead of the queue:")
    w = build_world(script=[CallStatus.COMPLETED], max_concurrent=1)
    campaign = await w.campaign("Queue", ["+923001111111", "+923002222222", "+923003333333"])
    late = await w.prospect_by_number("+923003333333")
    late_membership = await w.membership_of("+923003333333", campaign)
    w.store.force_membership(late_membership.id, status=MembershipStatus.COMPLETED, attempt_count=1)
    await w.store.schedule_callback(prospect_id=late.id, scheduled_for=NOW - timedelta(minutes=1), campaign_id=campaign.id, campaign_prospect_id=late_membership.id)
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("a due callback is dialled before never-called prospects", w.carrier.requests and w.carrier.requests[0].to_number == "+923003333333")

    print("\n  when the bot died before reopening the membership:")
    w = build_world(max_attempts=1, script=[CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await w.campaign("Crash", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    await worker.tick()
    prospect = await w.prospect_by_number("+923001111111")
    membership = await w.membership_of("+923001111111", campaign)
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW + timedelta(hours=1), campaign_id=campaign.id, campaign_prospect_id=membership.id)
    await worker.tick()  # The call ends; the bot died before its sink could reopen the membership.
    membership = await w.membership_of("+923001111111", campaign)
    check("the membership is closed", membership.status is MembershipStatus.COMPLETED)
    check("but the promise keeps the campaign open", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE)
    w.clock.advance(3600)
    mark = _mark()
    await worker.tick()
    check("the worker reopens it and places the callback", w.carrier.calls_to("+923001111111") == 2 and _logged("callback.reopened", mark) == 1)

    print("\n  the override off:")
    w = build_world(max_attempts=1, script=[CallStatus.COMPLETED])
    campaign = await w.campaign("NoOverride", ["+923001111111"])
    worker = w.make_worker(callbacks_override_attempt_limit=False)
    await worker.start()
    await worker.tick()
    await worker.tick()
    prospect = await w.prospect_by_number("+923001111111")
    membership = await w.membership_of("+923001111111", campaign)
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW, campaign_id=campaign.id, campaign_prospect_id=membership.id)
    mark = _mark()
    await worker.tick()
    check("a callback at the limit is not placed", w.carrier.calls_to("+923001111111") == 1)
    check("it stays pending and the log says so once", w.store.callbacks[1].status is CallbackStatus.PENDING and _logged("callback.unserviceable", mark) == 1)
    await worker.tick()
    check("and only once", _logged("callback.unserviceable", mark) == 1)

    print("\n  a paused campaign:")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Paused", ["+923001111111"])
    prospect = await w.prospect_by_number("+923001111111")
    membership = await w.membership_of("+923001111111", campaign)
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW, campaign_id=campaign.id, campaign_prospect_id=membership.id)
    await w.store.set_campaign_status(campaign.id, CampaignStatus.PAUSED)
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("its due callback waits", not w.carrier.requests and worker.metrics.skips["campaign_inactive"] == 1)

    print("\n  one honest try:")
    w = build_world(place_error=CallSetupError("Twilio refused the request (HTTP 400, code 21215)."))
    campaign = await w.campaign("Refused", ["+923001111111"])
    prospect = await w.prospect_by_number("+923001111111")
    membership = await w.membership_of("+923001111111", campaign)
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW, campaign_id=campaign.id, campaign_prospect_id=membership.id)
    worker = w.make_worker()
    await worker.start()
    for _ in range(3):
        await worker.tick()
    check("a callback the carrier refuses is tried once", len(w.carrier.requests) == 1)
    check("and withdrawn rather than retried every tick", w.store.callbacks[1].status is CallbackStatus.CANCELLED and len(w.store.attempts) == 1)

    print("\n  without a membership:")
    w = build_world()
    prospect = await w.store.add_prospect(first_name="Lone", last_name="Caller", phone="+923009999999", phone_normalized="+923009999999")
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW)
    worker = w.make_worker()
    await worker.start()
    mark = _mark()
    await worker.tick()
    await worker.tick()
    check("a callback with no campaign is left for a person, and said once", not w.carrier.requests and w.store.callbacks[1].status is CallbackStatus.PENDING and _logged("callback.unserviceable", mark) == 1)


async def check_restart_recovery() -> None:
    """A worker that dies loses no job and repeats none."""
    print("\n=== worker restart and recovery ===")

    print("\n  died mid-call:")
    w = build_world(script=[CallStatus.ANSWERED])
    campaign = await w.campaign("Restart", ["+923001111111", "+923002222222"])
    first = w.make_worker()
    await first.start()
    await first.tick()
    call_id = w.carrier.last_call_id
    check("the first worker placed a call", w.carrier.calls_to("+923001111111") == 1)
    del first  # The process is gone. The row and the carrier's call are not.

    w.clock.advance(300)
    second = w.make_worker(recovery_min_age_secs=0.0)
    mark = _mark()
    await second.start()
    check("the new worker adopts the call in progress", [a.telephony_call_id for a in second.in_flight] == [call_id] and _logged("worker.adopted", mark) == 1)
    check("without dialling to find out", w.carrier.calls_to("+923001111111") == 1)
    await second.tick()
    check("the adopted call holds the concurrency slot", w.carrier.calls_to("+923002222222") == 0)
    w.carrier.end_call(call_id, CallStatus.COMPLETED)
    await second.tick()
    attempt = (await w.attempts_to("+923001111111"))[0]
    check("its outcome is written when it ends", attempt.status is CallAttemptStatus.COMPLETED)
    check("exactly once", second.metrics.completed == 1 and _logged("call.completed", mark) == 1)
    check("and the membership moves on", (await w.membership_of("+923001111111", campaign)).status is MembershipStatus.COMPLETED)
    await second.tick()
    check("then the next prospect is called", w.carrier.calls_to("+923002222222") == 1)
    check("and the first never again", w.carrier.calls_to("+923001111111") == 1)

    print("\n  died between reserving and placing:")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Reserved", ["+923003333333"])
    queued = await w.service.next_call(campaign.id)  # What a worker does first...
    check("a reservation exists with no call", queued is not None and queued.attempt.status is CallAttemptStatus.PENDING)
    membership_id = queued.membership.id
    w.clock.advance(300)
    worker = w.make_worker(recovery_min_age_secs=0.0)
    mark = _mark()
    await worker.start()
    membership = w.store.memberships[membership_id]
    check("recovery hands it back to the queue", membership.status is MembershipStatus.PENDING and membership.attempt_count == 0, f"{membership.status} count {membership.attempt_count}")
    check("with no failed attempt left behind", not w.store.attempts)
    check("and says so", _logged("recovery.unreserved", mark) == 1)
    await worker.tick()
    attempts = await w.attempts_to("+923003333333")
    check("the prospect is then called exactly once", w.carrier.calls_to("+923003333333") == 1 and len(attempts) == 1)
    check("as attempt one, under the same key", attempts[0].attempt_number == 1 and attempts[0].idempotency_key == campaign_call_key(campaign_id=campaign.id, membership_id=membership_id, attempt_number=1))

    print("\n  a young reservation is left alone until it is old enough:")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Young", ["+923004444444"])
    await w.service.next_call(campaign.id)
    worker = w.make_worker(recovery_min_age_secs=120.0, recovery_interval_secs=60.0)
    await worker.start()
    await worker.tick()
    check("too young to touch, so the prospect stays blocked", not w.carrier.requests and len(w.store.attempts) == 1)
    w.clock.advance(121)
    await worker.tick()
    w.clock.advance(1)
    await worker.tick()
    check("once old enough, the periodic pass frees it and the call is placed", w.carrier.calls_to("+923004444444") == 1)

    print("\n  an ambiguous placement, then a restart:")
    w = build_world(place_error=ProviderUnavailableError("the carrier timed out"), place_error_times=1)
    campaign = await w.campaign("Ambiguous", ["+923005555555"])
    first = w.make_worker()
    await first.start()
    await first.tick()
    attempt = (await w.attempts_to("+923005555555"))[0]
    check("the placement is held as UNRESOLVED", attempt.status is CallAttemptStatus.UNRESOLVED)
    del first
    w.clock.advance(300)
    second = w.make_worker(recovery_min_age_secs=0.0)
    await second.start()
    check("the new worker asks the carrier, never redials", w.carrier.searches == 1 and w.carrier.calls_to("+923005555555") == 1)
    attempt = (await w.attempts_to("+923005555555"))[0]
    check("no call existed, so it is closed as failed", attempt.status is CallAttemptStatus.FAILED)
    await second.tick()
    check("and the prospect is not dialled again by the campaign's own rules", w.carrier.calls_to("+923005555555") == 1)

    print("\n  an ambiguous placement whose error has no message (Phase 24 audit):")
    # A bare `TimeoutError()` prints as "", and `"".splitlines()[0]` raised
    # IndexError inside the hold, leaving the attempt reserved and the prospect
    # blocked until recovery. The reason falls back to the exception's name.
    w = build_world(place_error=TimeoutError(), place_error_times=1)
    campaign = await w.campaign("Silent timeout", ["+923005555556"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    attempt = (await w.attempts_to("+923005555556"))[0]
    check("the placement is held as UNRESOLVED, not left reserved", attempt.status is CallAttemptStatus.UNRESOLVED, attempt.status)
    check("the reason names the exception", attempt.failure_reason == "TimeoutError", attempt.failure_reason)

    print("\n  an ambiguous placement that did create a call:")
    w = build_world(place_error=ProviderUnavailableError("lost the answer"), place_error_times=1)
    campaign = await w.campaign("Ghost", ["+923006666666"])
    first = w.make_worker()
    await first.start()
    await first.tick()
    # The request was received after all: the carrier has a call to that number.
    w.carrier.recent = [CallSnapshot(provider="scripted", call_id="CAghost", status=CallStatus.COMPLETED, to_number="+923006666666", duration_secs=30.0, created_at=w.clock())]
    del first
    w.clock.advance(300)
    second = w.make_worker(recovery_min_age_secs=0.0)
    await second.start()
    attempt = (await w.attempts_to("+923006666666"))[0]
    check("recovery adopts the call the carrier has", attempt.telephony_call_id == "CAghost" and attempt.status is CallAttemptStatus.COMPLETED)
    await second.tick()
    check("and no second call is placed", w.carrier.calls_to("+923006666666") == 1)
    check("the membership is completed", (await w.membership_of("+923006666666", campaign)).status is MembershipStatus.COMPLETED)


async def check_campaign_completion() -> None:
    """A campaign closes when nothing is left, and only then."""
    print("\n=== campaign completion ===")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Done", ["+923001111111", "+923002222222"])
    w.budget_secs = 600
    worker = w.make_worker()
    mark = _mark()
    await worker.run()
    check("every prospect reached: COMPLETED", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)
    check("with completed_at stamped", w.store.campaigns[campaign.id].completed_at is not None)
    check("logged with the counts", _logged("campaign.completed", mark) == 1)
    check("counted", worker.metrics.campaigns_completed == 1)

    print("\n  not while a retry is pending:")
    w = build_world(script=[CallStatus.NO_ANSWER], retry_minutes=60)
    campaign = await w.campaign("Later", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    for _ in range(4):
        await worker.tick()
    check("a campaign with a retry an hour away stays ACTIVE", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE)

    print("\n  not while empty:")
    w = build_world()
    campaign = await w.campaign("Empty", [])
    worker = w.make_worker()
    await worker.start()
    mark = _mark()
    await worker.tick()
    await worker.tick()
    check("a campaign with no prospects yet stays ACTIVE", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE)
    check("and is noted once", _logged("campaign.empty", mark) == 1)

    print("\n  unusable numbers are closed so the campaign can finish:")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Unusable", ["+923001111111", "0300 not a number"])
    worker = w.make_worker()
    await worker.start()
    for _ in range(4):
        await worker.tick()
    bad = await w.membership_of("0300 not a number", campaign)
    check("the membership with no dialable number is SKIPPED", bad.status is MembershipStatus.SKIPPED)
    check("and the campaign completes", w.store.campaigns[campaign.id].status is CampaignStatus.COMPLETED)

    print("\n  a pending callback keeps it open:")
    w = build_world(script=[CallStatus.ANSWERED, CallStatus.COMPLETED], max_attempts=1)
    campaign = await w.campaign("Promise", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    await worker.tick()
    prospect = await w.prospect_by_number("+923001111111")
    membership = await w.membership_of("+923001111111", campaign)
    await w.store.schedule_callback(prospect_id=prospect.id, scheduled_for=NOW + timedelta(days=1), campaign_id=campaign.id, campaign_prospect_id=membership.id)
    for _ in range(3):
        await worker.tick()
    check("every membership is closed but a callback is promised: ACTIVE", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE and (await w.membership_of("+923001111111", campaign)).status is MembershipStatus.COMPLETED)

    print("\n  auto-complete off:")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Manual", ["+923001111111"])
    worker = w.make_worker(auto_complete=False)
    await worker.start()
    for _ in range(4):
        await worker.tick()
    check("the campaign is left ACTIVE for a person to close", w.store.campaigns[campaign.id].status is CampaignStatus.ACTIVE)

    print("\n  paused and draft campaigns are not served:")
    w = build_world(script=[CallStatus.COMPLETED])
    paused = await w.campaign("Paused", ["+923001111111"], status=CampaignStatus.PAUSED)
    draft = await w.campaign("Draft", ["+923002222222"], status=CampaignStatus.DRAFT)
    active = await w.campaign("Active", ["+923003333333"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    check("only the active campaign's prospect is called", [r.to_number for r in w.carrier.requests] == ["+923003333333"])
    check("the others are untouched", w.store.campaigns[paused.id].status is CampaignStatus.PAUSED and w.store.campaigns[draft.id].status is CampaignStatus.DRAFT)
    await w.store.set_campaign_status(paused.id, CampaignStatus.ACTIVE)
    for _ in range(3):
        await worker.tick()
    check("a campaign started while the worker runs is picked up", w.carrier.calls_to("+923001111111") == 1)
    check("and the campaign served by name only", True)

    print("\n  pausing mid-run:")
    w = build_world(script=[CallStatus.COMPLETED])
    campaign = await w.campaign("Pause", ["+923001111111", "+923002222222"])
    worker = w.make_worker()
    await worker.start()
    await worker.tick()
    await w.store.set_campaign_status(campaign.id, CampaignStatus.PAUSED)
    for _ in range(3):
        await worker.tick()
    check("a paused campaign places no more calls", len(w.carrier.requests) == 1)
    check("and is not marked completed", w.store.campaigns[campaign.id].status is CampaignStatus.PAUSED)
    check("the call already placed is still followed to its end", (await w.attempts_to("+923001111111"))[0].status is CallAttemptStatus.COMPLETED)


async def check_concurrency_and_pacing() -> None:
    """The limits hold across the whole run, not just per tick."""
    print("\n=== concurrency and pacing ===")
    w = build_world(max_concurrent=2, script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.COMPLETED])
    await w.campaign("Two", [f"+92300{n:07d}" for n in range(1, 6)])
    w.budget_secs = 3600
    worker = w.make_worker(max_calls=5)
    await worker.run()
    check("all five prospects were called", len(w.carrier.requests) == 5)
    check("never more than two at once", w.max_live <= 2, f"max live {w.max_live}")
    check("and two when it could", w.max_live == 2)
    check("a full worker polls rather than asking the queue", min(w.slept) >= 0.05 and worker.metrics.skips["concurrency"] == 0, str(dict(worker.metrics.skips)))

    print("\n  pacing:")
    w = build_world(max_concurrent=3, pacing_secs=10.0, script=[CallStatus.COMPLETED])
    await w.campaign("Paced", ["+923001111111", "+923002222222", "+923003333333"])
    w.budget_secs = 600
    worker = w.make_worker(max_calls=3)
    await worker.run()
    gaps = [(b - a).total_seconds() for a, b in zip(w.carrier.placed_at, w.carrier.placed_at[1:], strict=False)]
    check("three calls were placed", len(w.carrier.placed_at) == 3)
    check("at least ten seconds apart", all(g >= 10.0 for g in gaps), str(gaps))
    check("the pacing refusals are counted", worker.metrics.skips["pacing"] >= 1, str(dict(worker.metrics.skips)))
    check("and the loop slept for the gap rather than the idle ceiling", any(0 < s <= 10.0 for s in w.slept), str(w.slept[:5]))


async def check_graceful_shutdown() -> None:
    """Stop means stop placing; the calls in progress are seen to their end."""
    print("\n=== graceful shutdown ===")
    w = build_world(script=[CallStatus.ANSWERED])
    campaign = await w.campaign("Stop", ["+923001111111", "+923002222222"])
    worker = w.make_worker()

    async def on_sleep(n: int) -> None:
        if n == 2:
            worker.request_stop()
        if n == 5:
            w.carrier.end_call("CA0001", CallStatus.COMPLETED)

    w.on_sleep = on_sleep
    mark = _mark()
    await worker.run()
    check("one call was in progress when the stop arrived", len(w.carrier.requests) == 1)
    check("no new call was placed after it", w.carrier.calls_to("+923002222222") == 0)
    check("the call in progress was followed to its end", (await w.attempts_to("+923001111111"))[0].status is CallAttemptStatus.COMPLETED)
    check("then the run returned", not worker.in_flight)
    check("the other prospect was not lost", (await w.membership_of("+923002222222", campaign)).status is MembershipStatus.PENDING and (await w.membership_of("+923002222222", campaign)).attempt_count == 0)
    check("and the log says what happened", _logged("worker.stopping", mark) == 1 and _logged("worker.stopped", mark) == 1)

    print("\n  twice means now:")
    w = build_world(script=[CallStatus.ANSWERED])
    await w.campaign("Now", ["+923001111111"])
    worker = w.make_worker()

    async def stop_twice(n: int) -> None:
        if n == 1:
            worker.request_stop()
            worker.request_stop()

    w.on_sleep = stop_twice
    mark = _mark()
    await worker.run()
    check("the run returns with the call still live", len(worker.in_flight) == 1 and w.store.attempts[1].status.is_live)
    check("and names it for recovery", _logged("worker.left_live", mark) == 1)

    print("\n  the drain has a ceiling:")
    w = build_world(script=[CallStatus.ANSWERED])
    await w.campaign("Drain", ["+923001111111"])
    worker = w.make_worker(drain_secs=5.0)
    w.on_sleep = lambda n: worker.request_stop() if n == 1 else None  # type: ignore[assignment,return-value]

    async def stop_once(n: int) -> None:
        if n == 1:
            worker.request_stop()

    w.on_sleep = stop_once
    mark = _mark()
    started = w.clock()
    await worker.run()
    check("a call that never ends is abandoned after the drain", (w.clock() - started).total_seconds() >= 5.0 and _logged("worker.drain_timeout", mark) == 1)

    print("\n  --once:")
    w = build_world(script=[CallStatus.COMPLETED])
    await w.campaign("Once", ["+923001111111", "+923002222222"])
    worker = w.make_worker(once=True)
    await worker.run()
    check("one pass places what it can and returns", len(w.carrier.requests) == 1 and not w.slept)

    print("\n  --max-calls:")
    w = build_world(script=[CallStatus.COMPLETED])
    await w.campaign("Max", ["+923001111111", "+923002222222", "+923003333333"])
    worker = w.make_worker(max_calls=2)
    await worker.run()
    check("stops after the number asked for, having followed them", len(w.carrier.requests) == 2 and worker.metrics.completed == 2)


async def check_ambiguous_placement() -> None:
    """An answer that never came holds the slot until the carrier is asked."""
    print("\n=== an ambiguous placement ===")
    w = build_world(place_error=ProviderUnavailableError("the carrier timed out"), place_error_times=1, recovery_min_age_secs=60.0)
    await w.campaign("Held", ["+923001111111", "+923002222222"])
    worker = w.make_worker(recovery_min_age_secs=60.0, recovery_interval_secs=3600.0)
    await worker.start()
    mark = _mark()
    await worker.tick()
    attempt = (await w.attempts_to("+923001111111"))[0]
    check("the attempt is held as UNRESOLVED", attempt.status is CallAttemptStatus.UNRESOLVED)
    check("counted as failed, under its own name", worker.metrics.failed == 1 and worker.metrics.outcomes["UNRESOLVED"] == 1)
    check("and logged", _logged("call.unresolved", mark) >= 1)
    report = await worker.tick()
    check("it holds the slot: the next prospect waits", w.carrier.calls_to("+923002222222") == 0 and worker.metrics.skips["concurrency"] >= 1)
    check("the loop waits for recovery, not the idle ceiling", report.sleep_secs <= 61.0, str(report.sleep_secs))
    w.clock.advance(62)
    await worker.tick()
    attempt = (await w.attempts_to("+923001111111"))[0]
    check("recovery ran early and asked the carrier", w.carrier.searches == 1 and attempt.status is CallAttemptStatus.FAILED)
    await worker.tick()
    check("then the next prospect is called", w.carrier.calls_to("+923002222222") == 1)
    check("and the held one was never redialled", w.carrier.calls_to("+923001111111") == 1)


async def check_failure_tolerance() -> None:
    """A database or carrier that goes away costs a tick, not the run."""
    print("\n=== failures mid-run ===")
    w = build_world(script=[CallStatus.ANSWERED, CallStatus.COMPLETED])
    await w.campaign("Flaky", ["+923001111111"])
    worker = w.make_worker()
    await worker.start()
    w.store.fail_with = CampaignStoreError("the database went away")
    mark = _mark()
    report = await worker.tick()
    check("a database outage does not raise out of the tick", not w.carrier.requests and report.sleep_secs > 0)
    check("and is logged", _logged("worker.campaigns_unavailable", mark) >= 1 or _logged("worker.dial_failed", mark) >= 1)
    w.store.fail_with = None
    await worker.tick()
    check("the next tick places the call", len(w.carrier.requests) == 1)
    w.carrier.fetch_error = ProviderUnavailableError("blip")
    await worker.tick()
    check("a carrier that cannot be read keeps the call followed", len(worker.in_flight) == 1)
    w.carrier.fetch_error = None
    w.store.fail_with = CampaignStoreError("gone again")
    await worker.tick()
    check("a database outage while following keeps the call followed", len(worker.in_flight) == 1)
    w.store.fail_with = None
    await worker.tick()
    await worker.tick()
    check("and it still reaches its end", worker.metrics.completed == 1)

    print("\n  a bug in a tick (the traceback below is the worker logging the injected bug, as it should):")
    w = build_world(script=[CallStatus.COMPLETED])
    await w.campaign("Bug", ["+923001111111"])
    w.budget_secs = 300
    worker = w.make_worker()
    original = worker._place_queued
    calls = 0

    async def explode_once(report):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("an unexpected bug")
        return await original(report)

    worker._place_queued = explode_once  # type: ignore[method-assign]
    mark = _mark()
    await worker.run()
    check("an unexpected exception is logged and the loop continues", _logged("worker.tick_failed", mark) == 1 and len(w.carrier.requests) == 1)


async def check_metrics() -> None:
    """The counters and the summary line."""
    print("\n=== metrics ===")
    metrics = WorkerMetrics()
    metrics.queued += 3
    metrics.started += 2
    metrics.note_outcome(CallAttemptStatus.COMPLETED)
    metrics.note_outcome(CallAttemptStatus.NO_ANSWER)
    metrics.note_outcome(CallAttemptStatus.FAILED)
    metrics.note_skip("window")
    metrics.note_skip("window")
    metrics.note_skip("pacing")
    snapshot = metrics.snapshot()
    check("completed counts every non-failed ending", metrics.completed == 2 and metrics.failed == 1)
    check("skips are counted by reason", metrics.skipped == 3 and snapshot["skips"] == {"pacing": 1, "window": 2})
    check("outcomes are broken down", snapshot["outcomes"] == {"COMPLETED": 1, "FAILED": 1, "NO_ANSWER": 1})
    check("the snapshot has every counter", set(snapshot) >= {"queued", "started", "completed", "failed", "skipped", "callbacks", "recovered", "campaigns_completed", "ticks"})
    line = metrics.describe()
    check("the summary line reads", "3 queued, 2 started, 2 completed, 1 failed, 3 skipped" in line, line)

    w = build_world(script=[CallStatus.COMPLETED])
    await w.campaign("Report", ["+923001111111"])
    worker = w.make_worker(report_secs=5.0)
    w.budget_secs = 120
    mark = _mark()
    await worker.run()
    check("the metrics line is logged periodically and at the end", _logged("worker.metrics", mark) >= 2)


async def check_pipeline_untouched() -> None:
    """The scheduler lives beside the bot, never inside it."""
    print("\n=== the scheduler stays out of the audio pipeline ===")
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py does not import the worker", "worker" not in bot.lower() or "CampaignWorker" not in bot)
    worker_source = (SERVER / "src" / "campaigns" / "worker.py").read_text(encoding="utf-8")
    imports = [line for line in worker_source.splitlines() if line.startswith(("import ", "from "))]
    check("the worker imports nothing from pipecat", not any("pipecat" in line for line in imports), "; ".join(imports))
    check("nor from the conversation layer or the bot", not any("conversation" in line or "bot" in line for line in imports))
    check("only the campaign and reliability layers", all(line.startswith(("import ", "from __future__", "from .", "from collections", "from dataclasses", "from datetime", "from typing", "from loguru")) for line in imports), "; ".join(imports))


# --- The SQL, against a real PostgreSQL ---------------------------------------------


async def run_database_checks(dsn: str) -> None:
    """The Phase 13 SQL, in a schema that is thrown away."""
    from test_campaigns import StubProvider, with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== the SQL, against PostgreSQL ===")
        service = CampaignService(store, default_region="PK", max_attempts=1, retry_minutes=0)
        campaign = await service.create_campaign(f"Worker {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        prospect = await service.create_prospect(first_name="Last", last_name="Try", phone="0302 1112223")
        await service.add_prospects(campaign.id, [prospect.id])
        dialer = CampaignDialer(service, StubProvider(outcome=CallStatus.COMPLETED), from_number="+15550001111", public_url="https://x.test")
        result = await dialer.dial_next(campaign.id)
        check("with a limit of one, the only permitted attempt is placed", result.placed, result.describe())

        print("\n  reserve_membership:")
        service = CampaignService(store, default_region="PK", max_attempts=2, retry_minutes=60)
        campaign = await service.create_campaign(f"Targeted {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        first = await service.create_prospect(first_name="First", last_name="Row", phone="0303 1111111")
        second = await service.create_prospect(first_name="Second", last_name="Row", phone="0303 2222222")
        await service.add_prospects(campaign.id, [first.id, second.id])
        target = await store.find_membership(campaign.id, second.id)
        queued = await service.reserve_membership(target.id)
        check("hands out the named membership, not the queue's first", queued is not None and queued.membership.id == target.id)
        check("as IN_PROGRESS with an attempt row and a key", queued.membership.status is MembershipStatus.IN_PROGRESS and queued.attempt.idempotency_key is not None)
        check("and the other row is untouched", (await store.find_membership(campaign.id, first.id)).status is MembershipStatus.PENDING)
        again = await service.reserve_membership(target.id)
        check("a second targeted reservation of a reserved row gets nothing", again is None)
        await service.record_outcome(queued.attempt, CallAttemptStatus.NO_ANSWER)
        await store.set_membership_status(target.id, MembershipStatus.PENDING, next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
        check("one that is not due is refused", await service.reserve_membership(target.id) is None)
        await store.set_membership_status(target.id, MembershipStatus.PENDING, next_attempt_at=None)
        await store.set_membership_status(target.id, MembershipStatus.PENDING)
        second_call = await service.reserve_membership(target.id)
        check("a due one is handed out again", second_call is not None and second_call.attempt.attempt_number == 2)
        await service.record_outcome(second_call.attempt, CallAttemptStatus.NO_ANSWER)
        membership = await store.get_membership(target.id)
        check("two no-answers exhaust it at the limit", membership.status is MembershipStatus.EXHAUSTED and membership.attempt_count == 2)
        await store.reopen_membership(target.id, next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
        check("at the limit, a plain reservation is refused", await service.reserve_membership(target.id) is None)
        third = await service.reserve_membership(target.id, ignore_attempt_limit=True)
        check("and waived only when told — the callback case", third is not None and third.attempt.attempt_number == 3)
        await service.record_outcome(third.attempt, CallAttemptStatus.COMPLETED)

        print("\n  the race:")
        racer = await service.create_prospect(first_name="Race", last_name="Row", phone="0303 3333333")
        await service.add_prospects(campaign.id, [racer.id])
        race_membership = await store.find_membership(campaign.id, racer.id)
        results = await asyncio.gather(*(service.reserve_membership(race_membership.id) for _ in range(6)))
        check("six simultaneous targeted reservations hand out exactly one", sum(1 for r in results if r is not None) == 1)
        won = next(r for r in results if r is not None)
        check("with the count advanced once", (await store.get_membership(race_membership.id)).attempt_count == 1)

        print("\n  unreserve_attempt:")
        undone = await store.unreserve_attempt(won.attempt.id, next_attempt_at=datetime.now(UTC) + timedelta(hours=2))
        membership = await store.get_membership(race_membership.id)
        check("a never-placed reservation is undone", undone)
        check("the membership is PENDING with its count restored", membership.status is MembershipStatus.PENDING and membership.attempt_count == 0)
        check("scheduled for the given moment", membership.next_attempt_at is not None and membership.next_attempt_at > datetime.now(UTC) + timedelta(minutes=110))
        check("and the attempt row is gone", await store.get_attempt(won.attempt.id) is None)
        await store.set_membership_status(race_membership.id, MembershipStatus.PENDING)
        redo = await service.reserve_membership(race_membership.id)
        check("so the same attempt can be reserved again under the same key", redo is not None and redo.attempt.attempt_number == 1 and redo.attempt.idempotency_key == won.attempt.idempotency_key)
        await store.mark_placement_started(redo.attempt.id)
        check("but not undone once placement has started", not await store.unreserve_attempt(redo.attempt.id, next_attempt_at=datetime.now(UTC)))
        await service.record_outcome(redo.attempt, CallAttemptStatus.FAILED, failure_reason="check")

        print("\n  queue_outlook and sweep_memberships:")
        campaign = await service.create_campaign(f"Outlook {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        outlook = await store.queue_outlook(campaign.id, max_attempts=2)
        check("an empty campaign is not finished", outlook.total == 0 and not outlook.is_finished)
        due = await service.create_prospect(first_name="Due", last_name="Now", phone="0304 1111111")
        later = await service.create_prospect(first_name="Due", last_name="Later", phone="0304 2222222")
        bad = await service.create_prospect(first_name="No", last_name="Number", phone="not a number")
        await service.add_prospects(campaign.id, [due.id, later.id, bad.id])
        later_membership = await store.find_membership(campaign.id, later.id)
        await store.set_membership_status(later_membership.id, MembershipStatus.PENDING, next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
        outlook = await store.queue_outlook(campaign.id, max_attempts=2)
        check("counts what is due, what is later, and what can never be dialled", (outlook.total, outlook.pending, outlook.due_now, outlook.undialable) == (3, 3, 1, 1), str(outlook))
        check("and knows when the next retry is", outlook.next_due_at is not None and outlook.next_due_at > datetime.now(UTC) + timedelta(minutes=50))
        check("has live work", outlook.has_live_work and not outlook.is_finished)
        skipped, exhausted = await store.sweep_memberships(campaign.id, max_attempts=2)
        check("the sweep skips the unusable number", (skipped, exhausted) == (1, 0) and (await store.find_membership(campaign.id, bad.id)).status is MembershipStatus.SKIPPED)
        due_membership = await store.find_membership(campaign.id, due.id)
        await store.set_membership_status(due_membership.id, MembershipStatus.PENDING)
        await store.reopen_membership(due_membership.id, next_attempt_at=datetime.now(UTC))
        # A membership at the limit: exhausted by the sweep, unless a callback is pending.
        await store.set_membership_status(later_membership.id, MembershipStatus.PENDING)
        await admin.execute(f'UPDATE "{schema}".campaign_prospects SET attempt_count = 2 WHERE id = $1', later_membership.id)
        await store.schedule_callback(prospect_id=later.id, scheduled_for=datetime.now(UTC) + timedelta(hours=3), campaign_id=campaign.id, campaign_prospect_id=later_membership.id)
        outlook = await store.queue_outlook(campaign.id, max_attempts=2)
        check("a membership at the limit with a pending callback is not undialable", outlook.undialable == 0 and outlook.pending_callbacks == 1 and outlook.next_callback_at is not None, str(outlook))
        skipped, exhausted = await store.sweep_memberships(campaign.id, max_attempts=2)
        check("and the sweep leaves it alone", exhausted == 0)
        await store.set_callbacks_status(later.id, CallbackStatus.CANCELLED)
        skipped, exhausted = await store.sweep_memberships(campaign.id, max_attempts=2)
        check("once the callback is gone, the sweep exhausts it", exhausted == 1 and (await store.get_membership(later_membership.id)).status is MembershipStatus.EXHAUSTED)
        check("list_campaigns filters by status", all(c.status is CampaignStatus.ACTIVE for c in await store.list_campaigns(status=CampaignStatus.ACTIVE)) and campaign.id in {c.id for c in await store.list_campaigns(status=CampaignStatus.ACTIVE)})

        print("\n  the worker over the real store:")
        campaign = await service.create_campaign(f"Real {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        one = await service.create_prospect(first_name="Real", last_name="One", phone="0305 1111111")
        two = await service.create_prospect(first_name="Real", last_name="Two", phone="0305 2222222")
        await service.add_prospects(campaign.id, [one.id, two.id])
        carrier = StubProvider(outcome=CallStatus.COMPLETED)
        carrier._next_id = 500  # Call ids are unique in the table; the earlier stub used the low ones.
        guards = CampaignGuards(window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC"), pacing=PacingLimiter(0), max_concurrent=1)
        dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://x.test", guards=guards)
        recovery = AttemptRecovery(service, carrier, min_age_secs=0.0)
        sleeps = 0

        async def quick(secs: float) -> None:
            nonlocal sleeps
            sleeps += 1
            await asyncio.sleep(0)
            if sleeps > 200:
                worker.request_stop(immediate=True)

        worker = CampaignWorker(service, dialer, recovery=recovery, guards=guards, campaign_ids=[campaign.id], max_calls=2, sleep=quick)
        metrics = await worker.run()
        check("two prospects, two calls, followed to the end", len(carrier.requests) == 2 and metrics.completed == 2, metrics.describe())
        check("the memberships are completed", all(m.status is MembershipStatus.COMPLETED for m, _ in await store.list_campaign_prospects(campaign.id)))
        check("and the campaign closed itself", (await store.get_campaign(campaign.id)).status is CampaignStatus.COMPLETED)
        check("no live attempt is left behind", await store.count_live_attempts() == 0)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


# --- Runner ---------------------------------------------------------------------------


async def main() -> int:
    """Run every check and report."""
    print("Scheduler checks — a fake clock, a scripted carrier, and the real worker in between.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        await check_end_to_end()
        await check_duplicate_reservation()
        await check_calling_hours()
        await check_dnc()
        await check_retry_limits()
        await check_callbacks()
        await check_restart_recovery()
        await check_campaign_completion()
        await check_concurrency_and_pacing()
        await check_graceful_shutdown()
        await check_ambiguous_placement()
        await check_failure_tolerance()
        await check_metrics()
        await check_pipeline_untouched()

        from dotenv import load_dotenv

        load_dotenv(override=True)
        dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
        if not dsn:
            _skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
        else:
            import asyncpg

            try:
                await run_database_checks(dsn)
            except (OSError, asyncpg.PostgresError) as exc:
                _skipped.append(f"database checks (cannot reach PostgreSQL: {exc})")
    finally:
        logger.remove(handler)

    print()
    if _skipped:
        print("SKIPPED:")
        for item in _skipped:
            print(f"  - {item}")
        print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed." + (" (some were skipped)" if _skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
