#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the automation integration. Phase 17. No keys, no phone, no n8n.

Run it from the `server/` directory::

    uv run python tests/test_automation.py

**What this is for.** Phase 17 opens two doors: an API an automation
platform calls to make things happen, and an outbox that tells it what
happened. Both are writes to somebody else's system, so the checks are
arranged around the two promises the phase makes — *nothing happens twice*
and *nothing happens in the call* — and around the one thing an automation
retrying blindly needs: the same request, again, answered the same way.

**The real code, a fake world.** The API is the real FastAPI app over the
real `CampaignService`, driven through FastAPI's test client, over
`test_worker.py`'s in-memory store with the Phase 17 tables added. The
scheduler section runs the real `CampaignWorker` and the real
`CampaignDialer` against a scripted carrier to prove that a call the API
asked for is placed by the worker and by nothing else. The deliverer is the
real `EventDeliverer` over a scripted sender that records every POST — the
headers, the signature, the body — and answers what the check tells it to.
The SQL — the two tables, the claim's six creation statements, the settle
window, `call.updated`, the stale reclaim, the idempotency ledger — is
checked at the end against PostgreSQL in a temporary schema, skipped with a
message when none is reachable.

A plain script rather than a pytest suite, like the other sixteen. Exit
status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

from loguru import logger  # noqa: E402
from test_crm import rich_result  # noqa: E402
from test_worker import NOW, FakeClock, MemoryStore, ScriptedCarrier, World  # noqa: E402

from src.automation import (  # noqa: E402
    API_PREFIX,
    DELIVERY_HEADER,
    EVENT_HEADER,
    EVENT_ID_HEADER,
    IDEMPOTENCY_HEADER,
    PING_PATH,
    REPLAYED_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ApiSettings,
    EventDeliverer,
    SendResult,
    build_payload,
    create_automation_app,
    extract_key,
    fingerprint,
    key_matches,
    sign,
    verify_signature,
)
from src.campaigns import (  # noqa: E402
    AUTOMATION_EVENT_KINDS,
    AttemptRecovery,
    AutomationEvent,
    AutomationEventState,
    CallAttempt,
    CallAttemptStatus,
    CallbackStatus,
    CallResult,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    CampaignStore,
    CampaignStoreError,
    DuplicateProspectError,
    Meeting,
    MeetingStatus,
    MembershipStatus,
    Prospect,
    ProspectStatus,
    ResultSource,
    ScheduledCallback,
    build_carrier_result,
)
from src.campaigns.models import ApiRequestRecord  # noqa: E402
from src.campaigns.results import Disposition  # noqa: E402
from src.config import AutomationConfig, ConfigError  # noqa: E402
from src.conversation.qualification import DecisionRole  # noqa: E402
from src.reliability import CallingWindow, CampaignGuards, PacingLimiter  # noqa: E402
from src.telephony import CallStatus  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []

KEY = "test-key-0123456789abcdef"
OTHER_KEY = "second-key-fedcba9876543210"
SECRET = "webhook-secret-0123456789"
TOKEN = "header-token-abcdefghijkl"
URL_A = "https://n8n.example.test/webhook/aiva-events"
URL_B = "https://n8n.example.test/webhook/aiva-qualified"
AUTH = {"Authorization": f"Bearer {KEY}"}


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        _failures.append(label + (f" — {detail}" if detail else ""))


def _mark() -> int:
    return len(LOGS)


def _logged(name: str, since: int = 0) -> int:
    return sum(1 for line in LOGS[since:] if name in line)


# --- The fake store --------------------------------------------------------------------


@dataclass
class FakeStore(MemoryStore):
    """`test_worker.py`'s in-memory store, plus everything Phase 17 reads and writes.

    The claim mirrors the SQL's rules — the settle window, one event per
    fact, `call.updated` only once the completed event is closed and only
    one open at a time, the stale reclaim — so the deliverer can be driven
    whole in milliseconds. The SQL itself is checked in the last section.
    """

    results: dict[int, CallResult] = field(default_factory=dict)  # by attempt id
    meetings: dict[int, Meeting] = field(default_factory=dict)
    events: dict[int, AutomationEvent] = field(default_factory=dict)
    api_requests: dict[tuple[str, str], ApiRequestRecord] = field(default_factory=dict)
    transfers_missing: bool = False
    event_writes: int = 0
    dashboard_users: dict[int, Any] = field(default_factory=dict)  # Phase 27: sign-ups, by id

    async def close(self) -> None:
        return None

    # Dashboard users (Phase 27) — the table's two rules: name and email unique, case-insensitively.

    async def add_dashboard_user(self, *, name: str, email: str, role: str, password_hash: str, status: str = "active") -> Any:
        from src.campaigns.store import DashboardUser, DuplicateUserError

        self._guard()
        for row in self.dashboard_users.values():
            if row.name.lower() == name.lower():
                raise DuplicateUserError("name", name)
            if row.email.lower() == email.lower():
                raise DuplicateUserError("email", email)
        row = DashboardUser(id=self._next("dashboard_user"), name=name, email=email, role=role, password_hash=password_hash, created_at=self.clock(), status=status)
        self.dashboard_users[row.id] = row
        return row

    async def list_dashboard_users(self, *, status: str | None = None) -> list[Any]:
        self._guard()
        return [row for row in self.dashboard_users.values() if status is None or row.status == status]

    async def decide_dashboard_user(self, user_id: int, *, approve: bool, decided_by: str) -> Any:
        self._guard()
        row = self.dashboard_users.get(user_id)
        if row is None or row.status != "pending":
            return None
        if approve:
            row = dataclasses.replace(row, status="active", decided_by=decided_by, decided_at=self.clock())
            self.dashboard_users[user_id] = row
        else:
            del self.dashboard_users[user_id]
        return row

    async def get_dashboard_user(self, name_or_email: str) -> Any:
        self._guard()
        key = name_or_email.strip().lower()
        return next((row for row in self.dashboard_users.values() if row.name.lower() == key or row.email.lower() == key), None)

    # Prospects

    async def add_prospect(self, *, first_name: str, last_name: str, phone: str, phone_normalized: str | None = None, custom_data: dict[str, Any] | None = None, status: ProspectStatus | None = None, **fields: Any) -> Prospect:
        self._guard()
        if phone_normalized:
            existing = await self.find_prospect_by_phone(phone_normalized)
            if existing is not None:
                raise DuplicateProspectError(phone_normalized, existing.id)
        resolved = status or (ProspectStatus.NEW if phone_normalized else ProspectStatus.UNREACHABLE)
        prospect = await super().add_prospect(first_name=first_name, last_name=last_name, phone=phone, phone_normalized=phone_normalized, custom_data=custom_data, status=resolved)
        keep = {k: v for k, v in fields.items() if k in ("email", "company", "job_title", "industry", "location", "website")}
        if keep:
            prospect = dataclasses.replace(prospect, **keep)
            self.prospects[prospect.id] = prospect
        return prospect

    async def find_prospect_by_phone(self, phone_normalized: str) -> Prospect | None:
        self._guard()
        if not phone_normalized:
            return None
        return next((p for p in self.prospects.values() if p.phone_normalized == phone_normalized), None)

    async def list_prospects(self, *, limit: int = 50, offset: int = 0, status: ProspectStatus | None = None) -> list[Prospect]:
        self._guard()
        rows = [p for p in self.prospects.values() if status is None or p.status is status]
        return sorted(rows, key=lambda p: -p.id)[offset : offset + limit]

    async def count_prospects(self) -> int:
        self._guard()
        return len(self.prospects)

    # Campaigns

    async def find_campaign_by_name(self, name: str):
        self._guard()
        return next((c for c in self.campaigns.values() if c.name.lower() == name.lower()), None)

    async def list_campaign_prospects(self, campaign_id: int, *, limit: int = 50, offset: int = 0):
        self._guard()
        rows = sorted((m for m in self.memberships.values() if m.campaign_id == campaign_id), key=lambda m: m.id)
        return [(m, self.prospects[m.prospect_id]) for m in rows[offset : offset + limit]]

    async def create_attempt(self, *, prospect_id: int, campaign_id: int | None = None, campaign_prospect_id: int | None = None, attempt_number: int = 1, status: CallAttemptStatus = CallAttemptStatus.PENDING) -> CallAttempt:
        self._guard()
        attempt = CallAttempt(id=self._next("attempt"), prospect_id=prospect_id, campaign_id=campaign_id, campaign_prospect_id=campaign_prospect_id, attempt_number=attempt_number, status=status, created_at=self.now(), updated_at=self.now(), ended_at=self.now() if status.is_final else None)
        self.attempts[attempt.id] = attempt
        return attempt

    async def list_callbacks(self, *, prospect_id=None, status=CallbackStatus.PENDING, due_before=None, limit=50, campaign_id=None) -> list[ScheduledCallback]:
        rows = await super().list_callbacks(prospect_id=prospect_id, status=status, due_before=due_before, limit=10_000)
        return [cb for cb in rows if campaign_id is None or cb.campaign_id == campaign_id][:limit]

    # Results, transfers, meetings

    async def save_call_result(self, result: CallResult) -> CallResult | None:
        self._guard()
        existing = self.results.get(result.call_attempt_id)
        if existing is not None and existing.source is ResultSource.CONVERSATION and result.source is not ResultSource.CONVERSATION:
            return None
        stored = dataclasses.replace(
            result,
            id=existing.id if existing else self._next("result"),
            created_at=existing.created_at if existing else self.now(),
            updated_at=self.now(),
        )
        self.results[result.call_attempt_id] = stored
        return stored

    def touch_result(self, attempt_id: int, **changes: Any) -> CallResult:
        """The result changed after its events were delivered."""
        updated = dataclasses.replace(self.results[attempt_id], updated_at=self.now(), **changes)
        self.results[attempt_id] = updated
        return updated

    async def get_call_result(self, call_attempt_id: int) -> CallResult | None:
        self._guard()
        return self.results.get(call_attempt_id)

    async def list_call_results(self, *, prospect_id=None, campaign_id=None, disposition=None, limit=50, since=None, before_id=None) -> list[CallResult]:
        self._guard()
        rows = [
            r
            for r in self.results.values()
            if (prospect_id is None or r.prospect_id == prospect_id)
            and (campaign_id is None or r.campaign_id == campaign_id)
            and (disposition is None or r.disposition is disposition)
            and (since is None or (r.updated_at or self.now()) >= since)
            and (before_id is None or (r.id or 0) < before_id)
        ]
        return sorted(rows, key=lambda r: -(r.id or 0))[:limit]

    async def list_transfers(self, *, call_attempt_id: int | None = None, limit: int = 50) -> list[Any]:
        self._guard()
        if self.transfers_missing:
            raise CampaignStoreError("The call_transfers table does not exist.\n  Run:  uv run campaign.py init")
        return []

    async def add_meeting(self, *, start_at: datetime, end_at: datetime, provider: str, prospect_id: int | None = None, reference: str | None = None, timezone: str = "UTC", campaign_id: int | None = None, call_attempt_id: int | None = None, attendee_name: str | None = None, attendee_email: str | None = None, notes: str | None = None) -> Meeting:
        self._guard()
        meeting = Meeting(id=self._next("meeting"), prospect_id=prospect_id, start_at=start_at, end_at=end_at, provider=provider, reference=reference, timezone=timezone, campaign_id=campaign_id, call_attempt_id=call_attempt_id, attendee_name=attendee_name, attendee_email=attendee_email, notes=notes, created_at=self.now())
        self.meetings[meeting.id] = meeting
        return meeting

    async def get_meeting(self, meeting_id: int) -> Meeting | None:
        self._guard()
        return self.meetings.get(meeting_id)

    async def list_meetings(self, *, prospect_id=None, from_time=None, status=MeetingStatus.BOOKED, limit=50) -> list[Meeting]:
        self._guard()
        rows = [m for m in self.meetings.values() if (prospect_id is None or m.prospect_id == prospect_id) and (from_time is None or m.start_at >= from_time) and (status is None or m.status is status)]
        return sorted(rows, key=lambda m: (m.start_at, m.id))[:limit]

    # The outbox

    def _have(self, predicate) -> bool:
        return any(predicate(e) for e in self.events.values())

    def _add_event(self, key: str, kind: str, moment: datetime, **fields: Any) -> None:
        if self._have(lambda e: e.event_key == key):
            return
        event = AutomationEvent(id=self._next("event"), event_key=key, kind=kind, created_at=moment, updated_at=moment, **fields)
        self.events[event.id] = event

    async def claim_automation_events(self, kinds, *, limit: int = 20, now: datetime | None = None, settle_secs: float = 30.0, stale_secs: float = 600.0, since: datetime | None = None) -> list[AutomationEvent]:
        self._guard()
        kinds = tuple(kinds)
        unknown = [k for k in kinds if k not in AUTOMATION_EVENT_KINDS]
        if unknown:
            raise ValueError(f"unknown automation event kind(s): {', '.join(unknown)}")
        moment = now or self.now()
        settled = moment - timedelta(seconds=settle_secs)
        for r in list(self.results.values()):
            a = self.attempts.get(r.call_attempt_id)
            occurred = a.ended_at if a and a.ended_at else r.created_at
            ids = dict(call_result_id=r.id, call_attempt_id=r.call_attempt_id, prospect_id=r.prospect_id, campaign_id=r.campaign_id)
            fresh = (r.updated_at or moment) <= settled and (since is None or (r.created_at or moment) >= since)
            if "call.completed" in kinds and fresh and not self._have(lambda e, r=r: e.kind == "call.completed" and e.call_result_id == r.id):
                self._add_event(f"call.completed:result:{r.id}", "call.completed", moment, result_updated_at=r.updated_at, occurred_at=occurred, **ids)
            if "lead.qualified" in kinds and fresh and r.qualification_status.value == "QUALIFIED" and not self._have(lambda e, r=r: e.kind == "lead.qualified" and e.call_result_id == r.id):
                self._add_event(f"lead.qualified:result:{r.id}", "lead.qualified", moment, result_updated_at=r.updated_at, occurred_at=occurred, **ids)
            if "call.updated" in kinds and (r.updated_at or moment) <= settled:
                done = next((e for e in self.events.values() if e.kind == "call.completed" and e.call_result_id == r.id and e.state in (AutomationEventState.DELIVERED, AutomationEventState.FAILED, AutomationEventState.SKIPPED)), None)
                blocked = self._have(lambda e, r=r: e.kind == "call.updated" and e.call_result_id == r.id and ((e.result_updated_at is not None and e.result_updated_at >= r.updated_at) or e.state.is_open))
                if done is not None and done.result_updated_at is not None and r.updated_at > done.result_updated_at and not blocked:
                    self._add_event(f"call.updated:result:{r.id}:{int(r.updated_at.timestamp())}", "call.updated", moment, result_updated_at=r.updated_at, occurred_at=r.updated_at, **ids)
        if "meeting.booked" in kinds:
            for m in self.meetings.values():
                if m.status is MeetingStatus.BOOKED and (since is None or (m.created_at or moment) >= since) and not self._have(lambda e, m=m: e.kind == "meeting.booked" and e.meeting_id == m.id):
                    self._add_event(f"meeting.booked:meeting:{m.id}", "meeting.booked", moment, meeting_id=m.id, call_attempt_id=m.call_attempt_id, prospect_id=m.prospect_id, campaign_id=m.campaign_id, occurred_at=m.created_at)
        if "callback.scheduled" in kinds:
            for c in self.callbacks.values():
                if c.status is CallbackStatus.PENDING and (since is None or (c.updated_at or moment) >= since):
                    self._add_event(f"callback.scheduled:callback:{c.id}:{int(c.scheduled_for.timestamp())}", "callback.scheduled", moment, callback_id=c.id, call_attempt_id=c.call_attempt_id, prospect_id=c.prospect_id, campaign_id=c.campaign_id, occurred_at=c.updated_at)
        if "campaign.completed" in kinds:
            for c in self.campaigns.values():
                if c.status is CampaignStatus.COMPLETED and c.completed_at is not None and (since is None or c.completed_at >= since):
                    self._add_event(f"campaign.completed:campaign:{c.id}:{int(c.completed_at.timestamp())}", "campaign.completed", moment, campaign_id=c.id, occurred_at=c.completed_at)

        claimable = [
            e
            for e in self.events.values()
            if e.kind in kinds
            and (
                (e.state in (AutomationEventState.PENDING, AutomationEventState.RETRY) and (e.next_attempt_at is None or e.next_attempt_at <= moment))
                or (e.state is AutomationEventState.DELIVERING and (e.started_at is None or e.started_at <= moment - timedelta(seconds=stale_secs)))
            )
        ]
        claimable.sort(key=lambda e: (e.occurred_at or moment, e.id))
        claimed = []
        for e in claimable[:limit]:
            updated = dataclasses.replace(e, state=AutomationEventState.DELIVERING, started_at=moment, attempts=e.attempts + 1, updated_at=moment)
            self.events[e.id] = updated
            claimed.append(updated)
        return claimed

    async def record_automation_event(self, event_id: int, *, state=None, error=None, status_code=None, next_attempt_at=None, delivered_at=None, payload=None, target_url=None, result_updated_at=None, attempts=None) -> AutomationEvent | None:
        self._guard()
        self.event_writes += 1
        e = self.events.get(event_id)
        if e is None:
            return None
        updated = dataclasses.replace(
            e,
            state=state if state is not None else e.state,
            last_error=e.last_error if state is None else error,
            last_status=status_code if status_code is not None else e.last_status,
            next_attempt_at=e.next_attempt_at if state is None else next_attempt_at,
            delivered_at=delivered_at or e.delivered_at,
            payload=payload if payload is not None else e.payload,
            target_url=target_url or e.target_url,
            result_updated_at=result_updated_at or e.result_updated_at,
            attempts=attempts if attempts is not None else e.attempts,
            updated_at=self.now(),
        )
        self.events[event_id] = updated
        return updated

    async def get_automation_event(self, event_id: int) -> AutomationEvent | None:
        self._guard()
        return self.events.get(event_id)

    async def find_automation_event(self, event_key: str) -> AutomationEvent | None:
        return next((e for e in self.events.values() if e.event_key == event_key), None)

    async def list_automation_events(self, *, state=None, kind=None, prospect_id=None, limit=50) -> list[AutomationEvent]:
        self._guard()
        rows = [e for e in self.events.values() if (state is None or e.state is state) and (kind is None or e.kind == kind) and (prospect_id is None or e.prospect_id == prospect_id)]
        return sorted(rows, key=lambda e: -e.id)[:limit]

    async def automation_event_counts(self) -> dict[str, int]:
        self._guard()
        counts: dict[str, int] = defaultdict(int)
        for e in self.events.values():
            counts[e.state.value] += 1
        return dict(counts)

    async def retry_automation_events(self, *, event_id=None, all_failed=False) -> int:
        self._guard()
        count = 0
        for e in list(self.events.values()):
            if (event_id is not None and e.id == event_id and e.state is not AutomationEventState.DELIVERING) or (event_id is None and all_failed and e.state is AutomationEventState.FAILED):
                self.events[e.id] = dataclasses.replace(e, state=AutomationEventState.PENDING, next_attempt_at=None, last_error=None, attempts=0)
                count += 1
        return count

    def events_of(self, kind: str) -> list[AutomationEvent]:
        return sorted((e for e in self.events.values() if e.kind == kind), key=lambda e: e.id)

    # API idempotency

    async def get_api_request(self, scope: str, idempotency_key: str) -> ApiRequestRecord | None:
        self._guard()
        return self.api_requests.get((scope, idempotency_key))

    async def save_api_request(self, *, scope: str, idempotency_key: str, fingerprint: str, status_code: int, response: dict[str, Any]) -> tuple[ApiRequestRecord, bool]:
        self._guard()
        existing = self.api_requests.get((scope, idempotency_key))
        if existing is not None:
            return existing, False
        record = ApiRequestRecord(id=self._next("request"), scope=scope, idempotency_key=idempotency_key, fingerprint=fingerprint, status_code=status_code, response=response, created_at=self.now())
        self.api_requests[(scope, idempotency_key)] = record
        return record, True

    async def purge_api_requests(self, *, older_than: datetime) -> int:
        before = len(self.api_requests)
        self.api_requests = {k: v for k, v in self.api_requests.items() if (v.created_at or older_than) >= older_than}
        return before - len(self.api_requests)


def automation_config(**overrides: Any) -> AutomationConfig:
    fields: dict[str, Any] = dict(
        api_keys=(KEY, OTHER_KEY),
        host="127.0.0.1",
        port=7890,
        webhook_url=None,
        webhook_urls={},
        webhook_secret=None,
        webhook_auth_header=None,
        webhook_auth_token=None,
        events=AUTOMATION_EVENT_KINDS,
        events_since=None,
        settle_secs=30.0,
        poll_secs=10.0,
        batch=20,
        max_attempts=3,
        retry_secs=30.0,
        max_retry_secs=1800.0,
        stale_secs=600.0,
        timeout_secs=5.0,
        idempotency_ttl_secs=86400.0,
        include_transcript=False,
    )
    fields.update(overrides)
    return AutomationConfig(**fields)


def api_settings(**overrides: Any) -> ApiSettings:
    return ApiSettings(automation=automation_config(**overrides), database_url="memory://", default_region="PK", max_attempts=3, retry_minutes=60.0, callback_max_days_ahead=60, timezone="Asia/Karachi")


class RecordingSender:
    """A sender that keeps every POST and answers from a script."""

    def __init__(self, *answers: SendResult) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []
        self.raise_next: Exception | None = None
        self.on_send = None

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> SendResult:
        self.sent.append((url, body, headers))
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if self.on_send is not None:
            await self.on_send(len(self.sent))
        return self.answers.pop(0) if self.answers else SendResult(status=200, body="ok")

    @property
    def last(self) -> tuple[str, bytes, dict[str, str]]:
        return self.sent[-1]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(body.decode("utf-8")) for _, body, _ in self.sent]


OK = SendResult(status=200, body="ok")


# --- Checks -------------------------------------------------------------------------------


def check_auth() -> None:
    print("\n=== keys and signatures ===")
    check("a bearer key is read from Authorization", extract_key({"Authorization": f"Bearer {KEY}"}) == KEY)
    check("case-insensitively", extract_key({"authorization": f"bearer {KEY}"}) == KEY)
    check("X-API-Key is read too", extract_key({"X-API-Key": KEY}) == KEY)
    check("Authorization wins when both are present", extract_key({"Authorization": f"Bearer {KEY}", "X-API-Key": OTHER_KEY}) == KEY)
    check("a Basic header is not a key", extract_key({"Authorization": "Basic abc"}) is None)
    check("no header, no key", extract_key({}) is None)
    check("the right key matches", key_matches(KEY, (KEY, OTHER_KEY)))
    check("any configured key matches", key_matches(OTHER_KEY, (KEY, OTHER_KEY)))
    check("a wrong key does not", not key_matches("wrong-key-000000000000", (KEY, OTHER_KEY)))
    check("an empty key does not", not key_matches("", (KEY,)) and not key_matches(None, (KEY,)))
    check("with no keys configured nothing matches", not key_matches(KEY, ()))

    body = b'{"event":"call.completed","event_id":"call.completed:result:1"}'
    header = sign(SECRET, body, timestamp=1_800_000_000)
    check("the signature is t=…,v1=…", header.startswith("t=1800000000,v1=") and len(header.split("v1=")[1]) == 64, header)
    ok, why = verify_signature(SECRET, header, body, now=1_800_000_100)
    check("and verifies against the same body within the tolerance", ok, why)
    check("a changed body is refused", verify_signature(SECRET, header, body + b" ", now=1_800_000_100)[0] is False)
    check("a different secret is refused", verify_signature("another-secret-0000", header, body, now=1_800_000_100)[0] is False)
    stale = verify_signature(SECRET, header, body, now=1_800_000_000 + 301)
    check("a timestamp past the tolerance is refused as a replay", stale[0] is False and "tolerance" in stale[1], stale[1])
    check("a negative tolerance turns the clock check off", verify_signature(SECRET, header, body, now=1_900_000_000, tolerance_secs=-1)[0])
    check("no header is refused", verify_signature(SECRET, None, body)[0] is False)
    check("a malformed header is refused", verify_signature(SECRET, "nonsense", body)[0] is False and verify_signature(SECRET, "t=abc,v1=00", body)[0] is False)
    check("the digest comparison is case-insensitive on the hex", verify_signature(SECRET, header.upper().replace("T=", "t=").replace("V1=", "v1="), body, now=1_800_000_100)[0])
    check("a fingerprint is stable", fingerprint("POST", "/x", "{}") == fingerprint("POST", "/x", "{}"))
    check("and differs when any part differs", fingerprint("POST", "/x", "{}") != fingerprint("POST", "/x", "{ }") and fingerprint("a", "b") != fingerprint("ab"))


def check_config() -> None:
    print("\n=== configuration ===")
    names = [n for n in os.environ if n.startswith("AUTOMATION_")]
    saved = {name: os.environ.pop(name) for name in names}

    def read(**env: str) -> tuple[AutomationConfig, list[str]]:
        for name in [n for n in os.environ if n.startswith("AUTOMATION_")]:
            del os.environ[name]
        os.environ.update(env)
        problems: list[str] = []
        return AutomationConfig.from_env(problems), problems

    try:
        config, problems = read()
        check("with nothing set: no problems, API off, delivery off", not problems and not config.api_enabled and not config.delivery_enabled, str(problems))
        check("every kind enabled by default", config.events == AUTOMATION_EVENT_KINDS)
        check("loopback and 7890 by default", config.host == "127.0.0.1" and config.port == 7890)
        check("describe says both are off", "API off" in config.describe() and "delivery off" in config.describe(), config.describe())
        try:
            config.require_api_keys()
            check("require_api_keys refuses with no key", False, "did not raise")
        except ConfigError as exc:
            check("require_api_keys refuses with no key, saying how to make one", "AUTOMATION_API_KEYS" in str(exc) and "secrets" in str(exc))

        config, problems = read(AUTOMATION_API_KEYS=f" {KEY}, {OTHER_KEY},,{KEY}")
        check("keys are split, trimmed and de-duplicated", config.api_keys == (KEY, OTHER_KEY) and config.api_enabled and not problems, str(config.api_keys))
        config, problems = read(AUTOMATION_API_KEY=KEY)
        check("the singular name works too", config.api_keys == (KEY,))
        config, problems = read(AUTOMATION_API_KEYS="short")
        check("a short key is a problem", any("16 characters" in p for p in problems), str(problems))
        check("and the key never appears in describe", KEY not in automation_config().describe())

        config, problems = read(AUTOMATION_WEBHOOK_URL=URL_A, AUTOMATION_WEBHOOK_URL_LEAD_QUALIFIED=URL_B, AUTOMATION_WEBHOOK_SECRET=SECRET, AUTOMATION_WEBHOOK_AUTH_TOKEN=TOKEN)
        check("a webhook URL enables delivery for every kind", not problems and config.delivery_enabled and set(config.targets) == set(AUTOMATION_EVENT_KINDS), str(problems))
        check("a per-kind URL overrides the default for that kind only", config.target_for("lead.qualified") == URL_B and config.target_for("call.completed") == URL_A)
        check("a token without a header name gets X-Aiva-Key", config.webhook_auth_header == "X-Aiva-Key" and config.webhook_auth_token == TOKEN)
        check("describe names the host, signed, header auth — and no secret", "n8n.example.test" in config.describe() and "signed" in config.describe() and "header auth" in config.describe() and SECRET not in config.describe() and TOKEN not in config.describe(), config.describe())

        config, problems = read(AUTOMATION_WEBHOOK_URL_LEAD_QUALIFIED=URL_B, AUTOMATION_EVENTS="lead.qualified, meeting.booked")
        check("per-kind URLs alone enable only those kinds", config.targets == {"lead.qualified": URL_B} and config.events == ("lead.qualified", "meeting.booked"), str(config.targets))
        config, problems = read(AUTOMATION_WEBHOOK_URL="n8n.example.test/webhook")
        check("a URL without a scheme is a problem", any("http(s)" in p for p in problems) and not config.delivery_enabled, str(problems))
        config, problems = read(AUTOMATION_WEBHOOK_URL_CALL_FINISHED=URL_A)
        check("a per-kind URL for an unknown kind is a problem", any("CALL_FINISHED" in p for p in problems), str(problems))
        config, problems = read(AUTOMATION_EVENTS="call.completed,call.finished")
        check("an unknown kind in AUTOMATION_EVENTS is a problem", any("call.finished" in p for p in problems), str(problems))
        config, problems = read(AUTOMATION_WEBHOOK_AUTH_HEADER="X-Key")
        check("a header name without a token is a problem", any("AUTH_TOKEN" in p for p in problems), str(problems))
        config, problems = read(AUTOMATION_EVENTS_SINCE="2026-09-07T00:00:00Z", AUTOMATION_SETTLE_SECS="5", AUTOMATION_MAX_ATTEMPTS="4", AUTOMATION_RETRY_SECS="10", AUTOMATION_MAX_RETRY_SECS="5", AUTOMATION_INCLUDE_TRANSCRIPT="true")
        check("since is parsed as an aware moment", config.events_since == datetime(2026, 9, 7, tzinfo=UTC) and not problems, str(problems))
        check("numbers are read, and the retry cap is never below the first retry", config.settle_secs == 5 and config.max_attempts == 4 and config.retry_secs == 10 and config.max_retry_secs == 10 and config.include_transcript)
        config, problems = read(AUTOMATION_EVENTS_SINCE="yesterday")
        check("an unreadable since is a problem", any("ISO 8601" in p for p in problems), str(problems))
        config, problems = read(AUTOMATION_PORT="99999")
        check("a port out of range is a problem", problems and config.port == 7890, str(problems))
    finally:
        for name in [n for n in os.environ if n.startswith("AUTOMATION_")]:
            del os.environ[name]
        os.environ.update(saved)


async def _same(store: FakeStore) -> FakeStore:
    return store


async def check_api() -> None:
    print("\n=== the API, over the fake store ===")
    from fastapi.testclient import TestClient

    clock = FakeClock(NOW)
    store = FakeStore(clock=clock)
    settings = api_settings()
    app = create_automation_app(settings, store_factory=lambda: _same(store), deliver=False, clock=clock)

    try:
        create_automation_app(api_settings(api_keys=()), store_factory=lambda: _same(store), deliver=False)
        check("the app refuses to build without a key", False, "built")
    except ConfigError as exc:
        check("the app refuses to build without a key", "AUTOMATION_API_KEYS" in str(exc))

    with TestClient(app) as client:
        v = API_PREFIX
        ping = client.get(PING_PATH)
        check("ping answers without a key", ping.status_code == 200 and ping.json()["ok"] is True, ping.text)
        check("the OpenAPI schema is served", client.get(f"{v}/openapi.json").status_code == 200)

        refused = client.get(f"{v}/status")
        check("a request without a key is 401 with a JSON error", refused.status_code == 401 and refused.json()["error"]["code"] == "unauthorized", refused.text)
        check("a wrong key is 401", client.get(f"{v}/status", headers={"Authorization": "Bearer wrong-key-0000000000"}).status_code == 401)
        check("the key is refused in the log, without the key", _logged("automation.refused") >= 2 and "wrong-key" not in "".join(LOGS))
        check("X-API-Key is accepted", client.get(f"{v}/status", headers={"X-API-Key": OTHER_KEY}).status_code == 200)
        status = client.get(f"{v}/status", headers=AUTH)
        check("status reads: nothing yet, delivery off", status.status_code == 200 and status.json()["prospects"] == 0 and status.json()["delivery"]["enabled"] is False, status.text)

        print("\n  prospects:")
        sara = {"first_name": "Sara", "last_name": "Ali", "phone": "0300 1234567", "email": "sara@example.com", "company": "Ravi Logistics", "lead_score": 87}
        created = client.post(f"{v}/prospects", json=sara, headers=AUTH)
        check("a new prospect is 201 with the normalised number", created.status_code == 201 and created.json()["created"] is True and created.json()["prospect"]["phone_normalized"] == "+923001234567", created.text)
        sara_id = created.json()["prospect"]["id"]
        check("email and company are kept, unknown fields land in custom_data", created.json()["prospect"]["email"] == "sara@example.com" and created.json()["prospect"]["custom_data"] == {"lead_score": 87})
        again = client.post(f"{v}/prospects", json={**sara, "company": "Something Else"}, headers=AUTH)
        check("the same number again is 200, the existing row, nothing overwritten", again.status_code == 200 and again.json()["created"] is False and again.json()["prospect"]["id"] == sara_id and again.json()["prospect"]["company"] == "Ravi Logistics", again.text)
        check("written in a different format, still the same person", client.post(f"{v}/prospects", json={"first_name": "S", "last_name": "A", "phone": "+92 300 123 4567"}, headers=AUTH).json()["prospect"]["id"] == sara_id)
        bad = client.post(f"{v}/prospects", json={"first_name": "No", "last_name": "Number", "phone": "call me"}, headers=AUTH)
        check("an unusable number is stored UNREACHABLE, 201, with a warning", bad.status_code == 201 and bad.json()["prospect"]["status"] == "UNREACHABLE" and bad.json()["prospect"]["dialable"] is False and bad.json()["warnings"], bad.text)
        invalid = client.post(f"{v}/prospects", json={"first_name": "Only"}, headers=AUTH)
        check("a body missing fields is 422 naming them", invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_request" and any("last_name" in str(p.get("loc")) for p in invalid.json()["error"]["details"]["problems"]), invalid.text)

        print("\n  idempotency keys:")
        key = {**AUTH, IDEMPOTENCY_HEADER: "n8n-run-42"}
        first = client.post(f"{v}/prospects", json={"first_name": "Ahmed", "last_name": "Khan", "phone": "0301 7654321"}, headers=key)
        replay = client.post(f"{v}/prospects", json={"first_name": "Ahmed", "last_name": "Khan", "phone": "0301 7654321"}, headers=key)
        check("the first request with a key is 201 and echoes the key", first.status_code == 201 and first.headers.get(IDEMPOTENCY_HEADER) == "n8n-run-42", first.text)
        check("the same request again replays the stored answer, status and body alike", replay.status_code == 201 and replay.json() == first.json() and replay.headers.get(REPLAYED_HEADER) == "true", replay.text)
        check("and wrote nothing new", store.count_prospects and len(store.prospects) == 3)
        reused = client.post(f"{v}/prospects", json={"first_name": "Different", "last_name": "Body", "phone": "0302 0000000"}, headers=key)
        check("the same key with a different body is refused", reused.status_code == 422 and reused.json()["error"]["code"] == "idempotency_key_reused", reused.text)
        check("no key: every request is answered afresh, and the natural key still protects", client.post(f"{v}/prospects", json={"first_name": "Ahmed", "last_name": "Khan", "phone": "0301 7654321"}, headers=AUTH).status_code == 200)

        print("\n  import:")
        rows = [
            {"First Name": "Sara", "Surname": "Ali", "Mobile": "0300 1234567"},
            {"First Name": "Bilal", "Surname": "Raza", "Mobile": "0333 1112222", "Company": "Raza Freight", "Lead Score": "12"},
            {"First Name": "Nobody", "Surname": "Here", "Mobile": "12"},
        ]
        imported = client.post(f"{v}/prospects/import", json={"rows": rows, "campaign": "Q1 Outreach", "create_campaign": True}, headers=AUTH)
        body = imported.json()
        check("JSON rows go through the CSV importer: one created, one already known, one rejected", imported.status_code == 200 and body["created"] == 1 and body["duplicates"] == 1 and body["rejected_count"] == 1, imported.text)
        check("the rejected row says why", body["rejected"][0]["line"] == 4 and body["rejected"][0]["errors"], str(body["rejected"]))
        check("the headers were mapped", body["mapping"]["columns"].get("first_name") == "First Name" and "Lead Score" in body["mapping"]["extras"], str(body["mapping"]))
        check("the campaign was created and both usable rows joined it", body["campaign"]["name"] == "Q1 Outreach" and body["added_to_campaign"] == 2, str(body["campaign"]))
        campaign_id = body["campaign"]["id"]
        repeat = client.post(f"{v}/prospects/import", json={"rows": rows, "campaign": campaign_id}, headers=AUTH).json()
        check("re-importing creates nothing and adds nobody twice", repeat["created"] == 0 and repeat["duplicates"] == 2 and repeat["added_to_campaign"] == 0, str(repeat))
        csv_text = "first_name,last_name,phone\nHina,Qureshi,0345 9998877\n"
        as_csv = client.post(f"{v}/prospects/import?campaign={campaign_id}", content=csv_text, headers={**AUTH, "Content-Type": "text/csv"})
        check("a text/csv body is imported as-is", as_csv.status_code == 200 and as_csv.json()["created"] == 1 and as_csv.json()["added_to_campaign"] == 1, as_csv.text)
        before = len(store.prospects)
        dry = client.post(f"{v}/prospects/import", json={"rows": [{"first_name": "Dry", "last_name": "Run", "phone": "0300 5555555"}], "dry_run": True}, headers=AUTH)
        check("a dry run reports and writes nothing", dry.status_code == 200 and dry.json()["dry_run"] is True and len(store.prospects) == before, dry.text)
        unusable = client.post(f"{v}/prospects/import", json={"rows": [{"name": "x"}]}, headers=AUTH)
        check("rows with no usable columns are 422 naming what is missing", unusable.status_code == 422 and unusable.json()["error"]["code"] == "unusable_columns" and "phone" in unusable.json()["error"]["message"], unusable.text)
        missing = client.post(f"{v}/prospects/import", json={"rows": rows[:1], "campaign": "No Such Campaign"}, headers=AUTH)
        check("an unknown campaign without create_campaign is 404", missing.status_code == 404 and missing.json()["error"]["code"] == "campaign_not_found")

        listed = client.get(f"{v}/prospects?limit=2", headers=AUTH).json()
        check("prospects list newest first, paged", listed["count"] == 2 and listed["prospects"][0]["id"] > listed["prospects"][1]["id"])
        by_phone = client.get(f"{v}/prospects?phone=%2B92%20300%201234567", headers=AUTH).json()
        check("a prospect is found by number in any format", by_phone["count"] == 1 and by_phone["prospects"][0]["id"] == sara_id, str(by_phone))
        one = client.get(f"{v}/prospects/{sara_id}", headers=AUTH).json()
        check("one prospect comes with their calls and callbacks", one["prospect"]["id"] == sara_id and one["calls"] == [] and one["callbacks"] == [])
        check("an unknown prospect is 404", client.get(f"{v}/prospects/999", headers=AUTH).status_code == 404)

        print("\n  campaigns:")
        made = client.post(f"{v}/campaigns", json={"name": "Q2 Outreach", "description": "later"}, headers=AUTH)
        check("a new campaign is 201, DRAFT, with counts", made.status_code == 201 and made.json()["created"] and made.json()["campaign"]["status"] == "DRAFT" and made.json()["campaign"]["counts"]["total"] == 0, made.text)
        same = client.post(f"{v}/campaigns", json={"name": "q2 outreach"}, headers=AUTH)
        check("the same name again (any case) is 200, the existing one", same.status_code == 200 and same.json()["created"] is False and same.json()["campaign"]["id"] == made.json()["campaign"]["id"], same.text)
        q2 = made.json()["campaign"]["id"]
        added = client.post(f"{v}/campaigns/{q2}/prospects", json={"prospect_ids": [sara_id], "phones": ["0333 1112222", "0300 0000000"]}, headers=AUTH).json()
        check("prospects are added by id and by number; an unknown number is reported, not invented", added["added"] == 2 and added["unknown_phones"] == ["0300 0000000"] and added["campaign"]["counts"]["total"] == 2, str(added))
        check("adding again adds nobody", client.post(f"{v}/campaigns/Q2%20Outreach/prospects", json={"prospect_ids": [sara_id]}, headers=AUTH).json()["added"] == 0)
        check("nobody to add is 422", client.post(f"{v}/campaigns/{q2}/prospects", json={}, headers=AUTH).status_code == 422)
        members = client.get(f"{v}/campaigns/{q2}/prospects", headers=AUTH).json()
        check("the membership list pairs each membership with its person", members["count"] == 2 and all(m["membership"]["campaign_id"] == q2 and m["prospect"]["id"] == m["membership"]["prospect_id"] for m in members["members"]))
        detail = client.get(f"{v}/campaigns/{q2}", headers=AUTH).json()["campaign"]
        check("one campaign, by id, carries its counts and its queue", detail["counts"]["pending"] == 2 and "queue" in detail and detail["queue"]["is_finished"] is False, str(detail))
        check("by name too", client.get(f"{v}/campaigns/Q2%20Outreach", headers=AUTH).json()["campaign"]["id"] == q2)

        started = client.post(f"{v}/campaigns/{q2}/start", headers=AUTH).json()
        check("start makes it ACTIVE", started["changed"] is True and started["campaign"]["status"] == "ACTIVE")
        check("start again changes nothing, and is 200", client.post(f"{v}/campaigns/{q2}/start", headers=AUTH).json()["changed"] is False)
        check("pause", client.post(f"{v}/campaigns/{q2}/pause", headers=AUTH).json()["campaign"]["status"] == "PAUSED")
        check("resume", client.post(f"{v}/campaigns/{q2}/resume", headers=AUTH).json()["campaign"]["status"] == "ACTIVE")
        check("a DRAFT cannot be paused: 409 naming the states it could move from", (r := client.post(f"{v}/campaigns/{campaign_id}/pause", headers=AUTH)).status_code == 409 and r.json()["error"]["code"] == "invalid_transition" and "ACTIVE" in r.json()["error"]["details"]["allowed_from"], r.text)
        check("an unknown verb is 422", client.post(f"{v}/campaigns/{q2}/launch", headers=AUTH).status_code == 422)
        check("the campaigns list filters by status", [c["id"] for c in client.get(f"{v}/campaigns?status=active", headers=AUTH).json()["campaigns"]] == [q2])

        print("\n  calls and callbacks — queued, never dialled:")
        check("no campaign given is 422", client.post(f"{v}/calls", json={"prospect_id": sara_id}, headers=AUTH).json()["error"]["code"] == "campaign_required")
        check("an unknown prospect is 404", client.post(f"{v}/calls", json={"prospect_id": 999, "campaign_id": q2}, headers=AUTH).status_code == 404)
        check("an unknown number is 404", client.post(f"{v}/calls", json={"phone": "0300 9999999", "campaign_id": q2}, headers=AUTH).status_code == 404)
        check("neither prospect_id nor phone is 422", client.post(f"{v}/calls", json={"campaign_id": q2}, headers=AUTH).json()["error"]["code"] == "prospect_required")
        queued = client.post(f"{v}/calls", json={"phone": "0300 1234567", "campaign": "Q2 Outreach", "note": "n8n: hot lead"}, headers=AUTH)
        body = queued.json()
        check("a call request is 202 with a PENDING callback due now", queued.status_code == 202 and body["queued"] is True and body["callback"]["status"] == "PENDING" and body["callback"]["scheduled_for"] == NOW.isoformat(), queued.text)
        check("the note is kept, the membership named, and it says who dials", body["callback"]["note"] == "n8n: hot lead" and body["membership"]["campaign_id"] == q2 and "campaign.py run" in body["dialled_by"] and body["warnings"] == [], str(body))
        callback_id = body["callback"]["id"]
        check("nothing was dialled, nothing reserved: no attempt exists", store.attempts == {} and store.reservations == 0)
        again = client.post(f"{v}/calls", json={"prospect_id": sara_id, "campaign_id": q2}, headers=AUTH).json()
        check("asking again is the same pending callback, not a second one", again["callback"]["id"] == callback_id and again["replaced"] is None and len(store.callbacks) == 1, str(again))
        later = NOW + timedelta(hours=2)
        moved = client.post(f"{v}/callbacks", json={"prospect_id": sara_id, "campaign_id": q2, "scheduled_at": later.isoformat()}, headers=AUTH).json()
        check("a scheduled callback moves the pending one and says so", moved["callback"]["id"] == callback_id and moved["callback"]["scheduled_for"] == later.isoformat() and moved["replaced"]["scheduled_for"] == NOW.isoformat() and any("moved" in w for w in moved["warnings"]), str(moved))
        naive = client.post(f"{v}/callbacks", json={"prospect_id": sara_id, "campaign_id": q2, "scheduled_at": "2026-09-07T15:00:00"}, headers=AUTH).json()
        check("a naive time is read in the campaign's timezone", naive["callback"]["scheduled_for"] == "2026-09-07T15:00:00+05:00", str(naive["callback"]))
        check("a callback without a time is 422", client.post(f"{v}/callbacks", json={"prospect_id": sara_id, "campaign_id": q2}, headers=AUTH).json()["error"]["code"] == "scheduled_at_required")
        check("in the past is 422", client.post(f"{v}/callbacks", json={"prospect_id": sara_id, "campaign_id": q2, "scheduled_at": (NOW - timedelta(days=1)).isoformat()}, headers=AUTH).json()["error"]["code"] == "scheduled_in_past")
        check("too far ahead is 422", client.post(f"{v}/callbacks", json={"prospect_id": sara_id, "campaign_id": q2, "scheduled_at": (NOW + timedelta(days=90)).isoformat()}, headers=AUTH).json()["error"]["code"] == "too_far_ahead")
        draft = client.post(f"{v}/calls", json={"prospect_id": sara_id, "campaign_id": campaign_id}, headers=AUTH).json()
        check("a call for a DRAFT campaign is queued with a warning to start it", draft["queued"] and any("ACTIVE" in w for w in draft["warnings"]), str(draft["warnings"]))
        check("and joining that campaign was part of it", draft["joined_campaign"] is False and draft["membership"]["campaign_id"] == campaign_id)
        bilal = client.get(f"{v}/prospects?phone=0333%201112222", headers=AUTH).json()["prospects"][0]
        joined = client.post(f"{v}/calls", json={"prospect_id": bilal["id"], "campaign": "Q2 Outreach"}, headers=AUTH).json()
        check("a prospect not in the campaign is added to it on the way", joined["joined_campaign"] is False and joined["membership"]["prospect_id"] == bilal["id"])
        hina = client.get(f"{v}/prospects?phone=0345%209998877", headers=AUTH).json()["prospects"][0]
        joined = client.post(f"{v}/calls", json={"prospect_id": hina["id"], "campaign": "Q2 Outreach"}, headers=AUTH).json()
        check("… and one that was in no campaign joins it", joined["joined_campaign"] is True and joined["membership"]["campaign_id"] == q2, str(joined))
        dnc = client.post(f"{v}/prospects/{hina['id']}/do-not-call", headers=AUTH).json()
        check("do-not-call marks the person and cancels their callback", dnc["changed"] is True and dnc["prospect"]["status"] == "DO_NOT_CALL" and store.callbacks[joined["callback"]["id"]].status is CallbackStatus.CANCELLED)
        check("do-not-call again changes nothing", client.post(f"{v}/prospects/{hina['id']}/do-not-call", headers=AUTH).json()["changed"] is False)
        check("a call for them is 409 do_not_call", client.post(f"{v}/calls", json={"prospect_id": hina["id"], "campaign_id": q2}, headers=AUTH).json()["error"]["code"] == "do_not_call")
        unreachable = client.get(f"{v}/prospects?status=UNREACHABLE", headers=AUTH).json()["prospects"][0]
        check("a call for somebody with no usable number is 409 not_dialable", client.post(f"{v}/calls", json={"prospect_id": unreachable["id"], "campaign_id": q2}, headers=AUTH).json()["error"]["code"] == "not_dialable")

        # The DRAFT request above moved Sara's one pending callback into the
        # draft campaign, due now. Move it back to Q2, two hours out.
        moved_back = client.post(f"{v}/callbacks", json={"prospect_id": sara_id, "campaign_id": q2, "scheduled_at": later.isoformat()}, headers=AUTH).json()
        check("a callback moved into another campaign moves with its campaign", moved_back["callback"]["campaign_id"] == q2 and moved_back["callback"]["id"] == callback_id and moved_back["replaced"]["campaign_id"] == campaign_id, str(moved_back["callback"]))
        pending = client.get(f"{v}/callbacks", headers=AUTH).json()
        check("callbacks list the pending ones, soonest first", pending["count"] == 2 and [c["prospect_id"] for c in pending["callbacks"]] == [bilal["id"], sara_id], str([(c["prospect_id"], c["scheduled_for"]) for c in pending["callbacks"]]))
        check("due=true keeps only what has fallen due", [c["prospect_id"] for c in client.get(f"{v}/callbacks?due=true", headers=AUTH).json()["callbacks"]] == [bilal["id"]])
        check("status=all shows the cancelled one too", client.get(f"{v}/callbacks?status=all", headers=AUTH).json()["count"] == 3)
        check("filtered by campaign", client.get(f"{v}/callbacks?campaign_id={campaign_id}", headers=AUTH).json()["count"] == 0 and client.get(f"{v}/callbacks?campaign_id={q2}", headers=AUTH).json()["count"] == 2)
        bilal_callback = next(c["id"] for c in pending["callbacks"] if c["prospect_id"] == bilal["id"])
        cancelled = client.delete(f"{v}/callbacks/{callback_id}", headers=AUTH).json()
        check("DELETE withdraws a pending callback", cancelled["changed"] is True and cancelled["callback"]["status"] == "CANCELLED")
        check("and again changes nothing", client.delete(f"{v}/callbacks/{callback_id}", headers=AUTH).json()["changed"] is False)
        check("an unknown callback is 404", client.delete(f"{v}/callbacks/999", headers=AUTH).status_code == 404)

        print("\n  what the scheduler does with it:")
        # Sara's callback was cancelled above; Bilal's is due now for Q2, ACTIVE.
        service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
        carrier = ScriptedCarrier(clock, script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])
        guards = CampaignGuards(window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC", clock=clock), pacing=PacingLimiter(0, clock=clock.monotonic), max_concurrent=1)
        dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://example.test", guards=guards)
        world = World(clock=clock, store=store, service=service, carrier=carrier, guards=guards, dialer=dialer, recovery=AttemptRecovery(service, carrier, min_age_secs=120.0))
        worker = world.make_worker(campaign_ids=[q2], max_calls=1)
        metrics = await worker.run()
        attempts = sorted(store.attempts.values(), key=lambda a: a.id)
        check("the worker placed exactly the call the API asked for, as a callback", metrics.queued == 1 and metrics.callbacks == 1 and len(attempts) == 1 and attempts[0].prospect_id == bilal["id"], metrics.describe())
        check("through the carrier, followed to its end", attempts[0].status is CallAttemptStatus.COMPLETED and metrics.completed == 1 and carrier.calls_to("+923331112222") == 1)
        check("the callback is PLACED", store.callbacks[bilal_callback].status is CallbackStatus.PLACED)
        check("Sara's was withdrawn above and stays so; nothing else was dialled", store.callbacks[callback_id].status is CallbackStatus.CANCELLED and carrier.calls_to("+923001234567") == 0)
        attempt_id = attempts[0].id
        check("and the API shows the call", client.get(f"{v}/calls?prospect_id={bilal['id']}", headers=AUTH).json()["calls"][0]["id"] == attempt_id)
        one_call = client.get(f"{v}/calls/{attempt_id}", headers=AUTH).json()
        check("with its carrier result, transcript omitted by default", one_call["call"]["status"] == "COMPLETED" and one_call["result"]["disposition"] == "COMPLETED" and one_call["result"]["transcript"] is None and one_call["result"]["transcript_included"] is False, str(one_call)[:300])
        check("results list it, and say there is no next page", (rs := client.get(f"{v}/results?campaign_id={q2}", headers=AUTH).json())["count"] == 1 and rs["next_before_id"] is None)
        check("since= after it finds nothing; since= before it finds it — with the offset's plus unencoded, as n8n sends it", client.get(f"{v}/results?since={(clock() + timedelta(hours=1)).isoformat()}", headers=AUTH).json()["count"] == 0 and client.get(f"{v}/results?since={NOW.isoformat()}", headers=AUTH).json()["count"] == 1)
        check("and a Z suffix, and a naive time in the campaign's zone", client.get(f"{v}/results?since={NOW:%Y-%m-%dT%H:%M:%SZ}", headers=AUTH).json()["count"] == 1 and client.get(f"{v}/results?since=2026-09-07T15:00:00", headers=AUTH).json()["count"] == 1)
        check("an unreadable since is 422", client.get(f"{v}/results?since=yesterday", headers=AUTH).status_code == 422)
        full = client.get(f"{v}/results/{attempt_id}", headers=AUTH).json()
        check("one result carries the transcript, the prospect and the campaign", full["result"]["transcript_included"] is True and full["prospect"]["id"] == bilal["id"] and full["campaign"]["id"] == q2)
        check("a result for a call with none yet is 404 result_not_ready", (nr := client.get(f"{v}/results/999", headers=AUTH)).status_code == 404 and nr.json()["error"]["code"] == "call_not_found")
        check("bad filters are 422", client.get(f"{v}/results?disposition=MAYBE", headers=AUTH).status_code == 422 and client.get(f"{v}/callbacks?status=LOST", headers=AUTH).status_code == 422)

        print("\n  events and failures:")
        deliverer = EventDeliverer(store, targets={"call.completed": URL_A}, sender=RecordingSender(SendResult(status=500, body="boom")), settle_secs=0, clock=clock, max_attempts=3)
        clock.advance(1)
        report = await deliverer.run_once()
        check("(a delivery that failed, for the list)", report.claimed == 1 and report.retried == 1, report.describe())
        events = client.get(f"{v}/events", headers=AUTH).json()
        check("the outbox lists the event, its state and the counts", events["count"] == 1 and events["events"][0]["state"] == "RETRY" and events["counts"] == {"RETRY": 1} and events["events"][0]["last_error"].startswith("HTTP 500"), str(events))
        event_id = events["events"][0]["id"]
        check("filtered by state and kind", client.get(f"{v}/events?state=retry&kind=call.completed", headers=AUTH).json()["count"] == 1 and client.get(f"{v}/events?state=failed", headers=AUTH).json()["count"] == 0)
        detail = client.get(f"{v}/events/{event_id}", headers=AUTH).json()["event"]
        check("one event carries what was sent", detail["payload"]["event"] == "call.completed" and detail["payload"]["result"]["id"] is not None, str(detail)[:200])
        retried = client.post(f"{v}/events/{event_id}/retry", headers=AUTH).json()
        check("retry reopens it, due now", retried["changed"] is True and retried["event"]["state"] == "PENDING" and retried["event"]["next_attempt_at"] is None)
        check("an unknown event is 404", client.get(f"{v}/events/999", headers=AUTH).status_code == 404)
        status = client.get(f"{v}/status", headers=AUTH).json()
        check("status counts campaigns by state and events by state", status["campaigns"].get("ACTIVE") == 1 and status["events"] == {"PENDING": 1} and status["prospects"] == len(store.prospects), str(status))

        store.fail_with = CampaignStoreError("Could not connect to the campaign database at postgresql://***")
        down = client.get(f"{v}/prospects", headers=AUTH)
        check("a database that has gone away is 503 with the reason", down.status_code == 503 and down.json()["error"]["code"] == "database_unavailable", down.text)
        check("ping says so too, without a key", client.get(PING_PATH).json()["ok"] is False)
        store.fail_with = None
        check("and the API carries on once it is back", client.get(f"{v}/prospects", headers=AUTH).status_code == 200)

    check("the store is closed with the app", state_closed := True)


async def check_deliverer() -> None:
    print("\n=== the deliverer, over the fake store ===")
    clock = FakeClock(NOW)
    store = FakeStore(clock=clock)
    service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
    campaign = await store.create_campaign(name="Events", status=CampaignStatus.ACTIVE)
    sara = await service.create_prospect(first_name="Sara", last_name="Ali", phone="0300 1234567", email="sara@example.com", company="Ravi Logistics")
    membership = await store.add_to_campaign(campaign.id, sara.id)
    attempt = await store.create_attempt(prospect_id=sara.id, campaign_id=campaign.id, campaign_prospect_id=membership.id, status=CallAttemptStatus.COMPLETED)
    result = await store.save_call_result(dataclasses.replace(rich_result(attempt_id=attempt.id, prospect_id=sara.id, campaign_id=campaign.id, decision_role=DecisionRole.DECISION_MAKER), id=None, transcript=({"role": "agent", "text": "Hello Sara"},)))
    assert result is not None
    meeting = await store.add_meeting(start_at=NOW + timedelta(days=1), end_at=NOW + timedelta(days=1, minutes=30), provider="local", prospect_id=sara.id, campaign_id=campaign.id, call_attempt_id=attempt.id, attendee_name="Sara Ali", timezone="Asia/Karachi")
    callback = await store.schedule_callback(prospect_id=sara.id, scheduled_for=NOW + timedelta(hours=3), campaign_id=campaign.id, campaign_prospect_id=membership.id, note="after lunch")
    await store.set_campaign_status(campaign.id, CampaignStatus.COMPLETED)

    sender = RecordingSender()
    targets = {kind: (URL_B if kind == "lead.qualified" else URL_A) for kind in AUTOMATION_EVENT_KINDS}
    deliverer = EventDeliverer(store, targets=targets, secret=SECRET, auth_header="X-Aiva-Key", auth_token=TOKEN, sender=sender, settle_secs=30, retry_secs=30, max_retry_secs=1800, max_attempts=3, clock=clock)

    mark = _mark()
    report = await deliverer.run_once()
    kinds = Counter(headers[EVENT_HEADER] for _, _, headers in sender.sent)
    check("the first pass delivers the meeting, the callback and the campaign — the result has not settled", report.claimed == 3 and report.delivered == 3 and kinds == {"meeting.booked": 1, "callback.scheduled": 1, "campaign.completed": 1}, f"{report.describe()} {dict(kinds)}")
    check("oldest fact first", [h[EVENT_HEADER] for _, _, h in sender.sent] == ["meeting.booked", "callback.scheduled", "campaign.completed"])
    url, body, headers = sender.sent[0]
    payload = json.loads(body)
    check("a delivery is a JSON POST to the kind's URL", url == URL_A and headers["Content-Type"].startswith("application/json"))
    check("with the event headers", headers[EVENT_ID_HEADER] == "meeting.booked:meeting:1" and headers[DELIVERY_HEADER] == "1" and headers[TIMESTAMP_HEADER].isdigit() and headers["User-Agent"].startswith("Ai-Voice-Agent"))
    check("signed over the exact bytes sent", verify_signature(SECRET, headers[SIGNATURE_HEADER], body, now=int(headers[TIMESTAMP_HEADER]))[0])
    check("and with the static auth header", headers["X-Aiva-Key"] == TOKEN)
    check("the payload names the event and carries the rows", payload["event"] == "meeting.booked" and payload["event_id"] == "meeting.booked:meeting:1" and payload["prospect"]["id"] == sara.id and payload["campaign"]["id"] == campaign.id and payload["call"]["id"] == attempt.id and payload["meeting"]["reference"] == None and payload["meeting"]["start_at"] == (NOW + timedelta(days=1)).isoformat(), str(payload)[:300])
    check("the result is there when it exists, without the transcript", payload["result"]["disposition"] == "MEETING_BOOKED" and payload["result"]["transcript"] is None and payload["result"]["summary_text"], str(payload["result"])[:200])
    cb = sender.bodies()[1]
    check("the callback event carries the promise", cb["callback"]["note"] == "after lunch" and cb["callback"]["scheduled_for"] == (NOW + timedelta(hours=3)).isoformat())
    cc = sender.bodies()[2]
    check("the campaign event carries the counts", cc["campaign"]["status"] == "COMPLETED" and cc["campaign"]["counts"]["total"] == 1 and cc["call"] is None)
    rows = store.events_of("meeting.booked")
    check("the row is DELIVERED with the status, the payload, the target and the time", rows[0].state is AutomationEventState.DELIVERED and rows[0].last_status == 200 and rows[0].payload == payload and rows[0].target_url == URL_A and rows[0].delivered_at == NOW)
    check("logged as delivered, with the kind and the host", _logged("automation.delivered", mark) == 3 and "n8n.example.test" in "".join(LOGS[mark:]))
    check("a second pass finds nothing", (await deliverer.run_once()).claimed == 0 and len(sender.sent) == 3)

    clock.advance(31)
    report = await deliverer.run_once()
    kinds = [h[EVENT_HEADER] for _, _, h in sender.sent[3:]]
    check("once the result has settled, call.completed and lead.qualified follow", report.delivered == 2 and sorted(kinds) == ["call.completed", "lead.qualified"], f"{report.describe()} {kinds}")
    qualified = next((u, b) for u, b, h in sender.sent if h[EVENT_HEADER] == "lead.qualified")
    check("lead.qualified goes to its own URL", qualified[0] == URL_B)
    completed = next(b for _, b, h in sender.sent if h[EVENT_HEADER] == "call.completed")
    check("call.completed carries the full result and the empty transfers list", json.loads(completed)["result"]["qualification_status"] == "QUALIFIED" and json.loads(completed)["transfers"] == [])
    done = store.events_of("call.completed")[0]
    check("its row remembers which version of the result it sent", done.result_updated_at == result.updated_at)
    check("nothing is created twice", (await deliverer.run_once()).claimed == 0)

    print("\n  the result changes after it was sent:")
    clock.advance(10)
    store.touch_result(attempt.id, notes=("Prefers email follow-up", "Asked for a brochure"))
    check("not before it settles", (await deliverer.run_once()).claimed == 0)
    clock.advance(31)
    report = await deliverer.run_once()
    check("then one call.updated, keyed on the new version", report.delivered == 1 and sender.last[2][EVENT_HEADER] == "call.updated" and sender.last[2][EVENT_ID_HEADER].startswith("call.updated:result:1:"), report.describe())
    check("carrying the changed result", "brochure" in " ".join(json.loads(sender.last[1])["result"]["notes"]))
    check("and not a second lead.qualified", len([1 for _, _, h in sender.sent if h[EVENT_HEADER] == "lead.qualified"]) == 1)
    clock.advance(10)
    store.touch_result(attempt.id, notes=("changed again",))
    clock.advance(31)
    check("a further change is a further update", (await deliverer.run_once()).delivered == 1 and len(store.events_of("call.updated")) == 2)

    print("\n  transcript on request, and a table that is missing:")
    store.transfers_missing = True
    payload, updated_at = await build_payload(store, done, include_transcript=True)
    check("include_transcript carries it", payload["result"]["transcript_included"] is True and payload["result"]["transcript"][0]["text"] == "Hello Sara")
    check("a database without the transfers table still builds the payload", payload["transfers"] == [] and updated_at == store.results[attempt.id].updated_at)
    store.transfers_missing = False

    print("\n  failures:")
    other = await service.create_prospect(first_name="Bilal", last_name="Raza", phone="0333 1112222")
    fail_membership = await store.add_to_campaign(campaign.id, other.id)
    fail_attempt = await store.create_attempt(prospect_id=other.id, campaign_id=campaign.id, campaign_prospect_id=fail_membership.id, status=CallAttemptStatus.NO_ANSWER)
    thin = await store.save_call_result(build_carrier_result(fail_attempt))
    assert thin is not None
    clock.advance(31)
    sender.answers = [SendResult(status=503, body="<html>down</html>")]
    mark = _mark()
    report = await deliverer.run_once()
    row = store.events_of("call.completed")[-1]
    check("a 503 schedules a retry from retry_secs, jittered", report.retried == 1 and row.state is AutomationEventState.RETRY and row.attempts == 1 and row.last_status == 503 and 24 <= (row.next_attempt_at - clock()).total_seconds() <= 36, f"{row.state} {row.next_attempt_at}")
    check("with the error kept in the receiver's words, and the payload for the operator", row.last_error.startswith("HTTP 503") and row.payload is not None and row.target_url == URL_A)
    check("logged as a retry", _logged("automation.retry_scheduled", mark) == 1)
    check("not claimed before it is due", (await deliverer.run_once()).claimed == 0)
    clock.advance(40)
    sender.answers = [SendResult(status=429, body="slow down", retry_after=120)]
    await deliverer.run_once()
    row = store.events_of("call.completed")[-1]
    check("a 429 honours Retry-After as the floor", row.attempts == 2 and (row.next_attempt_at - clock()).total_seconds() >= 120, str(row.next_attempt_at - clock()))
    check("the same event id on every redelivery", len({h[EVENT_ID_HEADER] for _, _, h in sender.sent[-2:]}) == 1 and sender.last[2][DELIVERY_HEADER] == "2")
    clock.advance(200)
    sender.answers = [SendResult(status=None, error="timed out after 5s")]
    await deliverer.run_once()
    row = store.events_of("call.completed")[-1]
    check("no response at all: the third and last attempt is spent and the row is FAILED naming the count", row.state is AutomationEventState.FAILED and row.attempts == 3 and "after 3 attempts" in row.last_error and "timed out" in row.last_error, row.last_error)
    check("logged as failed, naming the fix", _logged("automation.failed", mark) == 1 and "events-retry" in "".join(LOGS[mark:]))
    check("a failed row is not claimed again", (await deliverer.run_once()).claimed == 0)
    check("events-retry reopens it with a fresh attempt budget", await store.retry_automation_events(all_failed=True) == 1 and store.events[row.id].state is AutomationEventState.PENDING and store.events[row.id].attempts == 0)
    sender.answers = [SendResult(status=400, body='{"message":"bad payload"}')]
    await deliverer.run_once()
    row = store.events_of("call.completed")[-1]
    check("a 400 is a refusal on the merits: FAILED at once, with the body", row.state is AutomationEventState.FAILED and row.attempts == 1 and "bad payload" in row.last_error, row.last_error)
    await store.retry_automation_events(event_id=row.id)
    sender.answers = [SendResult(status=404, body="webhook not registered")]
    await deliverer.run_once()
    row = store.events_of("call.completed")[-1]
    check("a 404 — n8n's answer for a workflow that is not active — is retried, not failed", row.state is AutomationEventState.RETRY and "not registered" in row.last_error, f"{row.state} {row.last_error}")
    await store.retry_automation_events(event_id=row.id)
    sender.raise_next = RuntimeError("sender exploded")
    mark = _mark()
    await deliverer.run_once()
    row = store.events_of("call.completed")[-1]
    check("a sender that raises does not stop the pass: the row is retried with the error", row.state is AutomationEventState.RETRY and "sender exploded" in row.last_error and _logged("automation.delivery_crashed", mark) == 1, row.last_error)
    await store.retry_automation_events(event_id=row.id)
    store.fail_with = CampaignStoreError("db down")
    report = await deliverer.run_once()
    check("a database that has gone away is reported, not raised", report.claimed == 0 and report.notes and _logged("automation.claim_failed") == 1)
    store.fail_with = None

    print("\n  stop, and the run loop:")
    unsigned = EventDeliverer(store, targets={"call.completed": URL_A}, sender=(plain := RecordingSender()), settle_secs=0, clock=clock)
    await unsigned.run_once()
    check("without a secret there is no signature header, and no auth header", plain.sent and SIGNATURE_HEADER not in plain.last[2] and "X-Aiva-Key" not in plain.last[2])
    check("the row is DELIVERED", store.events_of("call.completed")[-1].state is AutomationEventState.DELIVERED)

    slept: list[float] = []

    async def sleep(secs: float) -> None:
        slept.append(secs)
        clock.advance(secs)
        if len(slept) >= 3:
            looping.request_stop()

    looping = EventDeliverer(store, targets=targets, sender=RecordingSender(), settle_secs=0, clock=clock, sleep=sleep)
    totals = await looping.run(poll_secs=10)
    check("run() polls at poll_secs when there is nothing, and stops when asked", slept == [10, 10, 10] and totals.passes == 3 and totals.claimed == 0, f"{slept} {totals.describe()}")
    once = EventDeliverer(store, targets=targets, sender=RecordingSender(), settle_secs=0, clock=clock)
    check("run(once=True) is one pass", (await once.run(once=True)).passes == 1)

    stopping_sender = RecordingSender()
    for n in range(3):
        who = await service.create_prospect(first_name="P", last_name=str(n), phone=f"0301 000000{n}")
        m = await store.add_to_campaign(campaign.id, who.id)
        a = await store.create_attempt(prospect_id=who.id, campaign_id=campaign.id, campaign_prospect_id=m.id, status=CallAttemptStatus.BUSY)
        await store.save_call_result(build_carrier_result(a))
    clock.advance(31)
    stopper = EventDeliverer(store, targets={"call.completed": URL_A}, sender=stopping_sender, settle_secs=30, clock=clock)

    async def stop_after_first(n: int) -> None:
        if n == 1:
            stopper.request_stop()

    stopping_sender.on_send = stop_after_first
    report = await stopper.run_once()
    check("a stop mid-pass delivers the one in hand and hands the rest back, attempts unspent", report.delivered == 1 and report.released == 2 and all(e.attempts == 0 and e.state is AutomationEventState.RETRY for e in store.events_of("call.completed") if e.last_error == "stopping"), report.describe())

    print("\n  the outbox never creates what it was not asked for:")
    narrow = EventDeliverer(store, targets={"meeting.booked": URL_A}, sender=RecordingSender(), settle_secs=0, clock=clock)
    before = len(store.events)
    await narrow.run_once()
    check("a deliverer with one kind creates and claims only that kind", len(store.events) == before)
    try:
        await store.claim_automation_events(("call.finished",))
        check("an unknown kind is refused before any SQL", False, "no error")
    except ValueError as exc:
        check("an unknown kind is refused before any SQL", "call.finished" in str(exc))


def check_boundary() -> None:
    print("\n=== the boundary: nothing on the call path knows about the automation ===")
    on_the_call_path = [SERVER / "bot.py", SERVER / "call.py"]
    for folder in ("conversation", "campaigns", "actions", "telephony", "crm", "scheduling", "dashboard", "reliability"):
        on_the_call_path.extend(sorted((SERVER / "src" / folder).glob("*.py")))
    on_the_call_path.extend(sorted((SERVER / "src").glob("*.py")))
    offenders = []
    for path in on_the_call_path:
        text = path.read_text(encoding="utf-8")
        if "src.automation" in text or "from ..automation" in text or "from .automation" in text or "import automation" in text:
            offenders.append(str(path.relative_to(SERVER)))
    check("no module on the call path imports src/automation", not offenders, ", ".join(offenders))
    entry = (SERVER / "automation.py").read_text(encoding="utf-8")
    check("automation.py imports no pipeline: no pipecat, no bot", "pipecat" not in entry and "import bot" not in entry and "from bot" not in entry)
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py is untouched by the phase: it mentions no automation", "automation" not in bot)
    conversation = "".join(p.read_text(encoding="utf-8") for p in (SERVER / "src" / "conversation").glob("*.py"))
    check("and neither does the conversation layer", "automation" not in conversation and "n8n" not in conversation)
    api = (SERVER / "src" / "automation" / "api.py").read_text(encoding="utf-8")
    check("the API never places a call: no dialer, no carrier, no place_call", "CampaignDialer" not in api and "place_call" not in api and "make_provider" not in api)
    workflows = sorted((SERVER.parent / "n8n" / "workflows").glob("*.json"))
    check("six importable n8n workflows exist", len(workflows) == 6, ", ".join(p.name for p in workflows))
    broken = []
    for path in workflows:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            names = {node["name"] for node in data["nodes"]}
            for source, wiring in data.get("connections", {}).items():
                if source not in names:
                    broken.append(f"{path.name}: connection from unknown node {source!r}")
                for branch in wiring.get("main", []):
                    for link in branch:
                        if link["node"] not in names:
                            broken.append(f"{path.name}: connection to unknown node {link['node']!r}")
            if not data.get("name") or not data["nodes"]:
                broken.append(f"{path.name}: no name or no nodes")
        except (ValueError, KeyError, TypeError) as exc:
            broken.append(f"{path.name}: {exc}")
    check("each is valid JSON whose connections name real nodes", not broken, "; ".join(broken))
    readme = (SERVER.parent / "n8n" / "README.md").read_text(encoding="utf-8") if (SERVER.parent / "n8n" / "README.md").exists() else ""
    routes = ("/prospects/import", "/prospects/{id}/do-not-call", "/campaigns/{id or name}/start", "/campaigns/{id or name}/prospects", "/calls", "/callbacks", "/results", "/meetings", "/events/{id}/retry", "/status", "/api/ping")
    variables = ("AUTOMATION_API_KEYS", "AUTOMATION_HOST", "AUTOMATION_PORT", "AUTOMATION_WEBHOOK_URL", "AUTOMATION_WEBHOOK_URL_<KIND>", "AUTOMATION_EVENTS", "AUTOMATION_WEBHOOK_SECRET", "AUTOMATION_WEBHOOK_AUTH_HEADER", "AUTOMATION_EVENTS_SINCE", "AUTOMATION_SETTLE_SECS", "AUTOMATION_INCLUDE_TRANSCRIPT", "AUTOMATION_POLL_SECS", "AUTOMATION_BATCH", "AUTOMATION_MAX_ATTEMPTS", "AUTOMATION_RETRY_SECS", "AUTOMATION_MAX_RETRY_SECS", "AUTOMATION_TIMEOUT_SECS", "AUTOMATION_STALE_SECS", "AUTOMATION_IDEMPOTENCY_TTL_SECS")
    missing = [r for r in routes if r not in readme] + [v for v in variables if v not in readme]
    check("the n8n README documents every endpoint and every variable", "/api/v1" in readme and not missing, ", ".join(missing))
    check("and every event kind, with the signature scheme", all(kind in readme for kind in AUTOMATION_EVENT_KINDS) and "X-Aiva-Signature" in readme and "HMAC-SHA256" in readme)


async def run_database_checks(dsn: str) -> None:
    """The two tables, the claim, the API and the deliverer, against real rows in a schema that is thrown away."""
    from fastapi.testclient import TestClient
    from test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== automation_events and api_requests, against PostgreSQL ===")
        service = CampaignService(store, default_region="PK", max_attempts=2, retry_minutes=60)
        campaign = await service.create_campaign(f"Automation {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        sara = await service.create_prospect(first_name="Sara", last_name="Ali", phone="0300 1234567", email="sara@example.com")
        membership = await store.add_to_campaign(campaign.id, sara.id)
        assert membership is not None
        check("create_schema makes both tables", await store.automation_event_counts() == {} and await store.get_api_request("x", "y") is None)

        attempt = await store.create_attempt(prospect_id=sara.id, campaign_id=campaign.id, campaign_prospect_id=membership.id, status=CallAttemptStatus.COMPLETED)
        # A decision maker: `validate_call_result` wants the evidence for QUALIFIED.
        result = await store.save_call_result(dataclasses.replace(rich_result(attempt_id=attempt.id, prospect_id=sara.id, campaign_id=campaign.id, decision_role=DecisionRole.DECISION_MAKER), id=None))
        assert result is not None and result.updated_at is not None
        meeting = await store.add_meeting(start_at=NOW + timedelta(days=30), end_at=NOW + timedelta(days=30, minutes=30), provider="local", prospect_id=sara.id, campaign_id=campaign.id, call_attempt_id=attempt.id)
        callback = await store.schedule_callback(prospect_id=sara.id, scheduled_for=datetime.now(UTC) + timedelta(hours=2), campaign_id=campaign.id, campaign_prospect_id=membership.id, note="later")
        now = result.updated_at + timedelta(seconds=1)

        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=now, settle_secs=30)
        kinds = sorted(e.kind for e in claimed)
        check("before the result settles: the meeting and the callback, not the call", kinds == ["callback.scheduled", "meeting.booked"], str(kinds))
        check("claimed as DELIVERING, attempt 1, keyed on the row", all(e.state is AutomationEventState.DELIVERING and e.attempts == 1 for e in claimed) and {e.event_key for e in claimed} == {f"meeting.booked:meeting:{meeting.id}", f"callback.scheduled:callback:{callback.id}:{int(callback.scheduled_for.timestamp())}"}, str([e.event_key for e in claimed]))
        check("with the ids the payload needs", all(e.prospect_id == sara.id and e.campaign_id == campaign.id for e in claimed) and next(e for e in claimed if e.kind == "meeting.booked").call_attempt_id == attempt.id and next(e for e in claimed if e.kind == "callback.scheduled").callback_id == callback.id)
        check("a second claim gets nothing while they are being delivered", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=now, settle_secs=30) == [])
        for e in claimed:
            delivered = await store.record_automation_event(e.id, state=AutomationEventState.DELIVERED, error=None, status_code=200, delivered_at=now, payload={"event": e.kind}, target_url=URL_A)
            assert delivered is not None
        check("the outcome is written with the payload and the target", delivered.state is AutomationEventState.DELIVERED and delivered.payload == {"event": "callback.scheduled"} and delivered.last_status == 200 and delivered.target_url == URL_A and delivered.last_error is None)

        later = now + timedelta(seconds=60)
        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=later, settle_secs=30)
        kinds = sorted(e.kind for e in claimed)
        check("once settled: call.completed and lead.qualified, oldest fact first", kinds == ["call.completed", "lead.qualified"] and claimed[0].occurred_at is not None, str(kinds))
        completed = next(e for e in claimed if e.kind == "call.completed")
        check("keyed on the result, remembering its version", completed.event_key == f"call.completed:result:{result.id}" and completed.call_result_id == result.id and completed.result_updated_at == result.updated_at)
        for e in claimed:
            await store.record_automation_event(e.id, state=AutomationEventState.DELIVERED, error=None, status_code=200, delivered_at=later, result_updated_at=result.updated_at)
        check("and nothing is created twice", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=later + timedelta(hours=1), settle_secs=30) == [])

        print("\n  the result changes after it was sent:")
        await asyncio.sleep(0.05)
        rewritten = await store.save_call_result(dataclasses.replace(result, notes=("Asked for a brochure",)))
        assert rewritten is not None and rewritten.updated_at > result.updated_at
        check("not before it settles", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=rewritten.updated_at + timedelta(seconds=1), settle_secs=30) == [])
        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=rewritten.updated_at + timedelta(seconds=60), settle_secs=30)
        check("then exactly one call.updated, keyed on the new version", [e.kind for e in claimed] == ["call.updated"] and claimed[0].event_key == f"call.updated:result:{result.id}:{int(rewritten.updated_at.timestamp())}", str([e.event_key for e in claimed]))
        pending_update = claimed[0]
        await asyncio.sleep(0.05)
        rewritten2 = await store.save_call_result(dataclasses.replace(rewritten, notes=("Asked for a brochure", "and a call next week")))
        assert rewritten2 is not None
        check("a further change while one update is open creates no second one", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=rewritten2.updated_at + timedelta(seconds=60), settle_secs=30) == [])
        await store.record_automation_event(pending_update.id, state=AutomationEventState.DELIVERED, error=None, status_code=200, delivered_at=later, result_updated_at=rewritten2.updated_at)
        check("… and none after it is delivered carrying the latest version", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=rewritten2.updated_at + timedelta(seconds=60), settle_secs=30) == [])

        print("\n  a moved callback, a finished campaign, retries and stale claims:")
        moved = await store.schedule_callback(prospect_id=sara.id, scheduled_for=datetime.now(UTC) + timedelta(hours=5), campaign_id=campaign.id)
        await service.set_status(campaign.id, CampaignStatus.COMPLETED)
        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=datetime.now(UTC) + timedelta(seconds=60), settle_secs=30)
        kinds = sorted(e.kind for e in claimed)
        check("a moved callback is a new event, and the campaign's completion one more", kinds == ["callback.scheduled", "campaign.completed"] and any(e.callback_id == moved.id and e.event_key.endswith(str(int(moved.scheduled_for.timestamp()))) for e in claimed), str([e.event_key for e in claimed]))
        retry_row = next(e for e in claimed if e.kind == "campaign.completed")
        await store.record_automation_event(retry_row.id, state=AutomationEventState.RETRY, error="HTTP 503", status_code=503, next_attempt_at=datetime.now(UTC) + timedelta(minutes=5))
        other_row = next(e for e in claimed if e.kind == "callback.scheduled")
        await store.record_automation_event(other_row.id, state=AutomationEventState.DELIVERED, error=None, status_code=200, delivered_at=datetime.now(UTC))
        check("a RETRY row is not claimed before it is due", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=datetime.now(UTC) + timedelta(minutes=1), settle_secs=30) == [])
        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=datetime.now(UTC) + timedelta(minutes=6), settle_secs=30)
        check("and is once due, with the attempt counted and the error kept", len(claimed) == 1 and claimed[0].id == retry_row.id and claimed[0].attempts == 2 and claimed[0].last_error == "HTTP 503" and claimed[0].last_status == 503)
        await admin.execute(f'UPDATE "{schema}".automation_events SET started_at = now() - interval \'2 hours\' WHERE id = $1', retry_row.id)
        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=datetime.now(UTC), settle_secs=30, stale_secs=600)
        check("a DELIVERING row older than stale_secs is claimed again — its deliverer died", len(claimed) == 1 and claimed[0].id == retry_row.id and claimed[0].attempts == 3)
        await store.record_automation_event(retry_row.id, state=AutomationEventState.FAILED, error="gave up")
        counts = await store.automation_event_counts()
        check("the counts say where every event is", counts.get("DELIVERED") == 6 and counts.get("FAILED") == 1 and sum(counts.values()) == 7, str(counts))
        check("a FAILED row is listed by state, and by kind", [r.id for r in await store.list_automation_events(state=AutomationEventState.FAILED)] == [retry_row.id] and len(await store.list_automation_events(kind="call.completed")) == 1)
        check("found by its key", (await store.find_automation_event(f"call.completed:result:{result.id}")).id == completed.id)
        check("events-retry reopens it", await store.retry_automation_events(all_failed=True) == 1 and (await store.get_automation_event(retry_row.id)).state is AutomationEventState.PENDING)
        check("and reopens nothing twice", await store.retry_automation_events(all_failed=True) == 0)
        claimed = await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=datetime.now(UTC), settle_secs=30)
        check("a row being delivered cannot be reopened by id", len(claimed) == 1 and await store.retry_automation_events(event_id=claimed[0].id) == 0)
        await store.record_automation_event(claimed[0].id, state=AutomationEventState.DELIVERED, error=None, status_code=200, delivered_at=datetime.now(UTC))
        check("since bounds what becomes an event", await store.claim_automation_events(AUTOMATION_EVENT_KINDS, limit=10, now=datetime.now(UTC) + timedelta(days=1), settle_secs=0, since=datetime.now(UTC) + timedelta(days=2)) == [])
        try:
            await store.claim_automation_events(("call.finished",), now=datetime.now(UTC))
            check("an unknown kind is refused", False, "no error")
        except ValueError:
            check("an unknown kind is refused", True)

        print("\n  concurrent claims:")
        others = [await service.create_prospect(first_name="P", last_name=str(n), phone=f"0301 000000{n}") for n in range(7)]
        for who in others:
            row = await store.create_attempt(prospect_id=who.id, campaign_id=campaign.id, status=CallAttemptStatus.NO_ANSWER)
            await store.save_call_result(build_carrier_result(row))
        moment = datetime.now(UTC) + timedelta(minutes=5)
        batches = await asyncio.gather(*(store.claim_automation_events(("call.completed",), limit=2, now=moment, settle_secs=30) for _ in range(4)))
        ids = [e.id for batch in batches for e in batch]
        check("four simultaneous claims hand out disjoint rows", len(ids) == len(set(ids)) and len(ids) == 7, f"{[len(b) for b in batches]}")
        for batch in batches:
            for e in batch:
                await admin.execute(f'UPDATE "{schema}".automation_events SET state = \'PENDING\', attempts = 0 WHERE id = $1', e.id)

        print("\n  the idempotency ledger:")
        record, inserted = await store.save_api_request(scope="prospects.create", idempotency_key="n8n-1", fingerprint="abc", status_code=201, response={"prospect": {"id": 1}})
        check("a stored answer is inserted once", inserted and record.status_code == 201 and record.response == {"prospect": {"id": 1}})
        again, inserted = await store.save_api_request(scope="prospects.create", idempotency_key="n8n-1", fingerprint="abc", status_code=200, response={"other": True})
        check("a second save with the same key loses to the first, and returns it", not inserted and again.id == record.id and again.status_code == 201)
        check("read back by scope and key; another scope is another record", (await store.get_api_request("prospects.create", "n8n-1")).id == record.id and await store.get_api_request("campaigns.create", "n8n-1") is None)
        check("purge removes what is older than the moment", await store.purge_api_requests(older_than=datetime.now(UTC) + timedelta(seconds=1)) == 1 and await store.get_api_request("prospects.create", "n8n-1") is None)

        print("\n  the read extensions:")
        page = await store.list_call_results(campaign_id=campaign.id, limit=3)
        check("results page down by id", len(page) == 3 and [r.id for r in await store.list_call_results(campaign_id=campaign.id, limit=3, before_id=page[-1].id)][0] < page[-1].id)
        check("and by since", len(await store.list_call_results(campaign_id=campaign.id, since=datetime.now(UTC) + timedelta(hours=1))) == 0 and len(await store.list_call_results(campaign_id=campaign.id, since=result.created_at, limit=100)) == 8)
        check("get_meeting reads a booking by its row", (await store.get_meeting(meeting.id)).id == meeting.id and await store.get_meeting(999_999) is None)
        check("callbacks filter by campaign", len(await store.list_callbacks(campaign_id=campaign.id, status=None)) == 1 and await store.list_callbacks(campaign_id=campaign.id + 1000, status=None) == [])

        print("\n  the API over the real store:")
        import asyncpg

        # The test client runs the app in a thread with an event loop of its
        # own, and an asyncpg pool belongs to the loop that opened it — so
        # the app opens its own pool, on the same throwaway schema, inside
        # its lifespan, and closes it on shutdown.
        async def real_store() -> Any:
            async def use_schema(connection: Any) -> None:
                await connection.execute(f'SET search_path TO "{schema}"')

            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, setup=use_schema)
            return CampaignStore(pool)

        app = create_automation_app(api_settings(), store_factory=real_store, deliver=False)
        with TestClient(app) as client:
            v = API_PREFIX
            made = client.post(f"{v}/prospects", json={"first_name": "Hina", "last_name": "Qureshi", "phone": "0345 9998877"}, headers={**AUTH, IDEMPOTENCY_HEADER: "sql-1"})
            check("a prospect lands in SQL, 201", made.status_code == 201 and (await store.find_prospect_by_phone("+923459998877")) is not None, made.text)
            replay = client.post(f"{v}/prospects", json={"first_name": "Hina", "last_name": "Qureshi", "phone": "0345 9998877"}, headers={**AUTH, IDEMPOTENCY_HEADER: "sql-1"})
            check("the stored answer is replayed from the ledger", replay.headers.get(REPLAYED_HEADER) == "true" and replay.json() == made.json())
            hina_id = made.json()["prospect"]["id"]
            fresh = client.post(f"{v}/campaigns", json={"name": f"API {uuid.uuid4().hex[:6]}"}, headers=AUTH).json()["campaign"]
            check("a campaign lands in SQL", (await store.get_campaign(fresh["id"])) is not None)
            check("and starts", client.post(f"{v}/campaigns/{fresh['id']}/start", headers=AUTH).json()["campaign"]["status"] == "ACTIVE")
            queued = client.post(f"{v}/calls", json={"prospect_id": hina_id, "campaign_id": fresh["id"]}, headers=AUTH).json()
            row = await store.get_callback(queued["callback"]["id"])
            check("a call request is a real callbacks row, due now, on a real membership", row is not None and row.status is CallbackStatus.PENDING and row.campaign_prospect_id == queued["membership"]["id"] and (await store.find_membership(fresh["id"], hina_id)) is not None, str(row))
            check("and nothing was dialled", await store.list_attempts(prospect_id=hina_id) == [])
            check("results are listed from SQL, transcript omitted", client.get(f"{v}/results?campaign_id={campaign.id}&limit=2", headers=AUTH).json()["count"] == 2)
            check("the outbox is listed from SQL", client.get(f"{v}/events?kind=call.completed", headers=AUTH).json()["count"] == 8)

        print("\n  the deliverer over the real store:")
        sender = RecordingSender()
        deliverer = EventDeliverer(store, targets={kind: URL_A for kind in AUTOMATION_EVENT_KINDS}, secret=SECRET, sender=sender, settle_secs=0, batch=10)
        report = await deliverer.run_once()
        check("every open event is delivered — the seven concurrent ones and the new callback", report.claimed == 8 and report.delivered == 8 and len(sender.sent) == 8, report.describe())
        check("each signed, each with the same event id in the header and the body", all(verify_signature(SECRET, h[SIGNATURE_HEADER], b, now=int(h[TIMESTAMP_HEADER]))[0] and json.loads(b)["event_id"] == h[EVENT_ID_HEADER] for _, b, h in sender.sent))
        check("a second pass finds nothing left", (await deliverer.run_once()).claimed == 0)
        rows = await store.list_automation_events(state=AutomationEventState.DELIVERED, limit=100)
        # Seven delivered by hand above (meeting, callback, call.completed,
        # lead.qualified, call.updated, the moved callback, the campaign),
        # seven concurrent ones, and the callback the API queued.
        sent = [r for r in rows if r.target_url == URL_A]
        check("every row the deliverer sent carries its payload, its target and the time; the hand-recorded ones stand", len(rows) == 15 and len(sent) == 10 and all(r.payload and r.delivered_at for r in sent), f"{len(rows)} rows, {len(sent)} sent")
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("Automation checks — keys and signatures, configuration, the API over a fake store, the scheduler placing what the API asked for, the deliverer, the boundary, and the rows.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_auth()
        check_config()
        await check_api()
        await check_deliverer()
        check_boundary()

        from dotenv import load_dotenv

        load_dotenv(override=True)
        dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
        if not dsn:
            _skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
        else:
            import asyncpg

            # Only a database that cannot be reached is a skip. A statement
            # PostgreSQL rejects is a failure, and must read as one: the
            # first run of this script hid a type-inference bug in the
            # claim SQL behind "cannot reach PostgreSQL".
            try:
                await run_database_checks(dsn)
            except (
                OSError,
                asyncpg.exceptions.PostgresConnectionError,
                asyncpg.exceptions.InvalidAuthorizationSpecificationError,
                asyncpg.exceptions.InvalidCatalogNameError,
            ) as exc:
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
