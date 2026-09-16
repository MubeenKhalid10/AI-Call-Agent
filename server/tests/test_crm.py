#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the CRM integration. Phase 15. No keys, no CRM, no phone, no audio.

Run it from the `server/` directory::

    uv run python tests/test_crm.py

**What this is for.** Filing a call with a CRM is a write to somebody else's
system of record, so the checks are arranged around the four things a sync can
be — successful, failed, duplicated and retried — and assert on both sides:
what the CRM was asked to do (every request, in order) and what the `crm_sync`
row says afterwards.

**Stubs, not mocks — and the real code in the middle.** `MockCrm` is a real
`CrmProvider` that keeps contacts and activities in memory, can be told to
fail a call once, permanently, or to perform it and then lose the answer;
`FakeStore` is the store's surface with the claim rules written out. Between
them run the real `CrmSyncer` and the real `mapping`. The HubSpot adapter is
the real one over a stub HTTP session, so the endpoints, property names,
disposition ids and association it sends are pinned. The SQL — the table, the
claim under `SKIP LOCKED`, the re-sync on a changed result — is checked at the
end against PostgreSQL in a throwaway schema, skipped when none is reachable.

A plain script rather than a pytest suite, like the other fourteen. Exit
status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from test_worker import NOW, FakeClock  # noqa: E402

from src.campaigns import (  # noqa: E402
    CallAttempt,
    CallAttemptStatus,
    CallbackOutcome,
    CallResult,
    CallSummary,
    Campaign,
    CampaignService,
    CampaignStatus,
    CampaignStoreError,
    CrmSyncRecord,
    CrmSyncState,
    Disposition,
    MeetingOutcome,
    Prospect,
    ResultSource,
    build_carrier_result,
)
from src.config import Config, ConfigError  # noqa: E402
from src.conversation.qualification import (  # noqa: E402
    InterestLevel,
    NextAction,
    QualificationStatus,
)
from src.crm import (  # noqa: E402
    CallActivity,
    CallOutcome,
    CrmAuthError,
    CrmContact,
    CrmProvider,
    CrmRejectedError,
    CrmSyncer,
    CrmUnavailableError,
    build_call_sync,
    make_crm_provider,
    sync_key,
)
from src.crm.hubspot import (  # noqa: E402
    CALL_TO_CONTACT,
    CONTACT_PROPERTIES,
    DISPOSITIONS,
    HubSpotProvider,
)

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _logged(name: str, since: int = 0) -> int:
    return sum(1 for line in LOGS[since:] if name in line)


def _mark() -> int:
    return len(LOGS)


# --- Fixtures -----------------------------------------------------------------------


def prospect(prospect_id: int = 7, **overrides: Any) -> Prospect:
    fields = dict(
        id=prospect_id,
        first_name="Sara",
        last_name="Ali",
        phone="+923001234567",
        phone_normalized="+923001234567",
        email="sara@example.com",
        company="Ravi Logistics",
        job_title="Operations Director",
    )
    fields.update(overrides)
    return Prospect(**fields)


def attempt(attempt_id: int = 34, prospect_id: int = 7, **overrides: Any) -> CallAttempt:
    fields = dict(
        id=attempt_id,
        prospect_id=prospect_id,
        campaign_id=1,
        campaign_prospect_id=11,
        attempt_number=1,
        status=CallAttemptStatus.COMPLETED,
        telephony_call_id=f"CA{attempt_id:04d}",
        started_at=NOW - timedelta(minutes=5),
        connected_at=NOW - timedelta(minutes=4, seconds=50),
        ended_at=NOW,
        duration_seconds=290,
    )
    fields.update(overrides)
    return CallAttempt(**fields)


def rich_result(result_id: int = 12, attempt_id: int = 34, prospect_id: int = 7, **overrides: Any) -> CallResult:
    """A conversation result with everything a CRM could want filled in."""
    fields: dict[str, Any] = dict(
        id=result_id,
        call_attempt_id=attempt_id,
        prospect_id=prospect_id,
        campaign_id=1,
        source=ResultSource.CONVERSATION,
        call_status=CallAttemptStatus.COMPLETED,
        disposition=Disposition.MEETING_BOOKED,
        summary=CallSummary(
            what_happened="Reached Sara Ali and booked a meeting.",
            prospect_needs="Fuel spend is out of control across forty trucks.",
            objections="Raised price; handled by quantifying the saving.",
            interest="High interest.",
            qualification="Qualified: need, interest and authority established.",
            next_step="Meeting booked for Tuesday.",
        ),
        duration_seconds=290,
        qualification_status=QualificationStatus.QUALIFIED,
        interest_level=InterestLevel.INTERESTED,
        next_action=NextAction.MEETING_BOOKED,
        meeting_status=MeetingOutcome.BOOKED,
        meeting_start=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        meeting_reference="bk_abc123",
        callback_status=CallbackOutcome.UNKNOWN,
        pain_points=("fuel spend", "no visibility of idling"),
        objections=({"kind": "PRICE", "detail": "sounds expensive", "handled": True},),
        questions=("How long does installation take?",),
        existing_provider="spreadsheets",
        notes=("Prefers email follow-up",),
        human_requested=False,
        transferred=False,
        agent_ended_call=True,
        caller_turns=9,
        agent_turns=10,
        final_state="CLOSING",
        timezone="Asia/Karachi",
        tool_actions=({"name": "book_meeting", "success": True, "detail": "Tue 08 Sep 15:00"},),
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return CallResult(**fields)


def thin_result(result_id: int = 13, attempt_id: int = 35, prospect_id: int = 7, disposition: Disposition = Disposition.NO_ANSWER) -> CallResult:
    """A carrier result: nobody answered."""
    status = {
        Disposition.NO_ANSWER: CallAttemptStatus.NO_ANSWER,
        Disposition.BUSY: CallAttemptStatus.BUSY,
        Disposition.FAILED: CallAttemptStatus.FAILED,
        Disposition.VOICEMAIL: CallAttemptStatus.VOICEMAIL,
    }[disposition]
    return CallResult(
        id=result_id,
        call_attempt_id=attempt_id,
        prospect_id=prospect_id,
        campaign_id=1,
        source=ResultSource.CARRIER,
        call_status=status,
        disposition=disposition,
        summary=CallSummary(what_happened="Nobody answered.", prospect_needs="", objections="", interest="", qualification="", next_step="Retry later."),
        failure_reason="SIP 503" if disposition is Disposition.FAILED else None,
        created_at=NOW,
        updated_at=NOW,
    )


class MockCrm(CrmProvider):
    """A CRM in memory that records every request and can be told to fail.

    `failures[method]` is a queue of exceptions raised, one per call, before
    the method does anything; `lose_answer` names methods that *perform* the
    write and then raise, the way a timeout after a committed create looks
    from outside.
    """

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.contacts: dict[str, dict[str, Any]] = {}
        self.activities: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, list[Exception]] = defaultdict(list)
        self.lose_answer: set[str] = set()
        self.schema_error: Exception | None = None
        self.schema_calls = 0
        self.custom = True
        self._next = 0

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    def _fail(self, method: str) -> None:
        queue = self.failures.get(method)
        if queue:
            raise queue.pop(0)

    def _lost(self, method: str) -> None:
        if method in self.lose_answer:
            self.lose_answer.discard(method)
            raise CrmUnavailableError(f"{method}: the CRM did not answer within 30s")

    async def ensure_schema(self) -> None:
        self.schema_calls += 1
        if self.schema_error is not None:
            raise self.schema_error

    def disable_custom_properties(self) -> None:
        self.custom = False

    async def find_contact(self, contact: CrmContact) -> str | None:
        self.calls.append(("find_contact", contact))
        self._fail("find_contact")
        for contact_id, known in self.contacts.items():
            if (contact.email and known["email"] == contact.email) or (contact.phone and known["phone"] == contact.phone):
                return contact_id
        return None

    async def create_contact(self, contact: CrmContact) -> str:
        self.calls.append(("create_contact", contact))
        self._fail("create_contact")
        self._next += 1
        contact_id = f"C{self._next}"
        self.contacts[contact_id] = {"email": contact.email, "phone": contact.phone, "name": contact.display_name}
        self._lost("create_contact")
        return contact_id

    async def update_contact(self, contact_id: str, contact: CrmContact, activity: CallActivity) -> None:
        self.calls.append(("update_contact", (contact_id, dict(activity.fields))))
        self._fail("update_contact")
        self.contacts[contact_id]["latest"] = dict(activity.fields)

    async def find_activity(self, key: str) -> str | None:
        self.calls.append(("find_activity", key))
        self._fail("find_activity")
        return next((activity_id for activity_id, a in self.activities.items() if a["key"] == key), None)

    async def create_activity(self, contact_id: str, activity: CallActivity) -> str:
        self.calls.append(("create_activity", (contact_id, activity.key)))
        self._fail("create_activity")
        self._next += 1
        activity_id = f"A{self._next}"
        self.activities[activity_id] = {"key": activity.key, "contact": contact_id, "title": activity.title, "body": activity.body, "outcome": activity.outcome, "updates": 0}
        self._lost("create_activity")
        return activity_id

    async def update_activity(self, activity_id: str, contact_id: str, activity: CallActivity) -> None:
        self.calls.append(("update_activity", (activity_id, activity.key)))
        self._fail("update_activity")
        stored = self.activities[activity_id]
        stored.update(title=activity.title, body=activity.body, outcome=activity.outcome, updates=stored["updates"] + 1)

    async def check_credentials(self) -> str:
        return "mock account"


@dataclass
class FakeStore:
    """The store's CRM surface in memory, with the claim rules written out."""

    clock: FakeClock
    results: dict[int, CallResult] = field(default_factory=dict)
    prospects: dict[int, Prospect] = field(default_factory=dict)
    campaigns: dict[int, Campaign] = field(default_factory=dict)
    attempts: dict[int, CallAttempt] = field(default_factory=dict)
    sync: dict[int, CrmSyncRecord] = field(default_factory=dict)
    fail_with: Exception | None = None
    writes: int = 0
    _ids: int = 0

    def _guard(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def add(self, result: CallResult, *, prospect_row: Prospect | None = None, attempt_row: CallAttempt | None = None) -> CallResult:
        self.results[result.id] = result
        if prospect_row is not None:
            self.prospects[prospect_row.id] = prospect_row
        if attempt_row is not None:
            self.attempts[attempt_row.id] = attempt_row
        self.campaigns.setdefault(1, Campaign(id=1, name="Q1 Outreach"))
        return result

    def touch(self, result_id: int, **changes: Any) -> CallResult:
        """The result changed after it was filed — the conversation's result replacing the carrier's."""
        updated = dataclasses.replace(self.results[result_id], updated_at=self.clock() + timedelta(seconds=1), **changes)
        self.results[result_id] = updated
        return updated

    def row(self, result_id: int) -> CrmSyncRecord:
        return self.sync[result_id]

    async def claim_results_for_sync(self, provider: str, *, limit: int, now: datetime, stale_secs: float):
        self._guard()
        for result in self.results.values():
            if result.id not in self.sync:
                self._ids += 1
                self.sync[result.id] = CrmSyncRecord(
                    id=self._ids, call_result_id=result.id, call_attempt_id=result.call_attempt_id,
                    prospect_id=result.prospect_id, provider=provider, sync_key=sync_key(result.id, result.call_attempt_id),
                    created_at=now, updated_at=now,
                )
        claimable = []
        for record in sorted(self.sync.values(), key=lambda r: r.id):
            if record.provider != provider:
                continue
            result = self.results[record.call_result_id]
            due = record.next_attempt_at is None or record.next_attempt_at <= now
            stale = record.started_at is None or record.started_at <= now - timedelta(seconds=stale_secs)
            changed = record.result_updated_at is None or (result.updated_at or now) > record.result_updated_at
            if (record.state in (CrmSyncState.PENDING, CrmSyncState.RETRY) and due) or (record.state is CrmSyncState.SYNCING and stale) or (record.state is CrmSyncState.SYNCED and changed):
                claimable.append(record)
        claimed = []
        for record in claimable[:limit]:
            updated = dataclasses.replace(record, state=CrmSyncState.SYNCING, started_at=now, attempts=record.attempts + 1, updated_at=now)
            self.sync[record.call_result_id] = updated
            claimed.append((self.results[record.call_result_id], updated))
        return claimed

    async def record_crm_sync(self, sync_id: int, *, state=None, external_contact_id=None, external_activity_id=None, error=None, next_attempt_at=None, synced_at=None, result_updated_at=None, attempts=None):
        self._guard()
        self.writes += 1
        for result_id, record in self.sync.items():
            if record.id == sync_id:
                updated = dataclasses.replace(
                    record,
                    state=state if state is not None else record.state,
                    external_contact_id=external_contact_id or record.external_contact_id,
                    external_activity_id=external_activity_id or record.external_activity_id,
                    last_error=record.last_error if state is None else error,
                    next_attempt_at=record.next_attempt_at if state is None else next_attempt_at,
                    synced_at=synced_at or record.synced_at,
                    result_updated_at=result_updated_at or record.result_updated_at,
                    attempts=attempts if attempts is not None else record.attempts,
                    updated_at=self.clock(),
                )
                self.sync[result_id] = updated
                return updated
        return None

    async def retry_crm_sync(self, *, call_result_id=None, all_failed=False) -> int:
        count = 0
        for result_id, record in list(self.sync.items()):
            if (call_result_id is not None and result_id == call_result_id) or (call_result_id is None and all_failed and record.state is CrmSyncState.FAILED):
                self.sync[result_id] = dataclasses.replace(record, state=CrmSyncState.PENDING, next_attempt_at=None, last_error=None)
                count += 1
        return count

    async def list_crm_sync(self, *, state=None, limit=50):
        rows = [r for r in self.sync.values() if state is None or r.state is state]
        return sorted(rows, key=lambda r: -r.id)[:limit]

    async def crm_sync_counts(self):
        counts: dict[str, int] = defaultdict(int)
        for record in self.sync.values():
            counts[record.state.value] += 1
        counts["UNSEEN"] = sum(1 for r in self.results if r not in self.sync)
        return dict(counts)

    async def get_prospect(self, prospect_id: int):
        self._guard()
        return self.prospects.get(prospect_id)

    async def get_campaign(self, campaign_id: int):
        return self.campaigns.get(campaign_id)

    async def get_attempt(self, attempt_id: int):
        return self.attempts.get(attempt_id)


@dataclass
class Setup:
    clock: FakeClock
    store: FakeStore
    crm: MockCrm
    syncer: CrmSyncer


def build(**kwargs: Any) -> Setup:
    clock = FakeClock(NOW)
    store = FakeStore(clock=clock)
    crm = MockCrm()
    settings: dict[str, Any] = dict(from_number="+15550001111", max_attempts=8, retry_secs=60.0, max_retry_secs=3600.0, sync_unanswered=True, batch=20, stale_secs=900.0, clock=clock)
    settings.update(kwargs)
    syncer = CrmSyncer(store, crm, **settings)
    return Setup(clock=clock, store=store, crm=crm, syncer=syncer)


# --- Checks ---------------------------------------------------------------------------


def check_mapping() -> None:
    """From the result row to what the CRM is sent. Nothing invented, nothing dropped."""
    print("\n=== the mapping ===")
    result = rich_result()
    sync = build_call_sync(result, prospect(), campaign=Campaign(id=1, name="Q1 Outreach"), attempt=attempt(), from_number="+15550001111")
    contact, activity = sync.contact, sync.activity

    check("the key is derived from the result and the attempt", activity.key == "aiva12x34" and sync_key(12, 34) == activity.key)
    check("and is alphanumeric, so a search tokeniser keeps it whole", re.fullmatch(r"[a-z0-9]+", activity.key) is not None)
    check("the contact carries the prospect's identity", contact.email == "sara@example.com" and contact.phone == "+923001234567" and contact.has_identity)
    check("and the rest of the row", contact.company == "Ravi Logistics" and contact.job_title == "Operations Director" and contact.display_name == "Sara Ali")
    check("the title says who and what", activity.title == "AI call — meeting booked — Sara Ali (Q1 Outreach)", activity.title)
    check("a meeting booked is a connected call", activity.outcome is CallOutcome.CONNECTED)
    check("the call's time is the attempt's start", activity.occurred_at == NOW - timedelta(minutes=5))
    check("the duration and both numbers are carried", activity.duration_seconds == 290 and activity.from_number == "+15550001111" and activity.to_number == "+923001234567")

    body = activity.body
    for needle, label in (
        ("Reached Sara Ali and booked a meeting.", "the summary"),
        ("- fuel spend", "each pain point"),
        ("price: sounds expensive (handled)", "each objection with its kind and whether it was handled"),
        ("- How long does installation take?", "the questions they asked"),
        ("Booked for Tue 08 Sep 2026, 15:00 PKT (ref bk_abc123)", "the meeting, in the prospect's own timezone"),
        ("Callback\nNot discussed, or not recorded.", "a field nobody recorded is named as such"),
        ("Next action\nmeeting booked", "the next action"),
        ("Status: qualified", "the qualification"),
        ("Existing provider: spreadsheets", "discovery facts"),
        ("book_meeting: ok — Tue 08 Sep 15:00", "the actions taken"),
        ("- Prefers email follow-up", "the notes"),
        ("4 min 50 s", "the duration in words"),
        ("Campaign: Q1 Outreach", "the campaign"),
        ("ref aiva12x34", "the key"),
    ):
        check(f"the body carries {label}", needle in body, body[:200] if needle not in body else "")

    fields = activity.fields
    check("every structured fact is a string field", all(isinstance(v, str) and v for v in fields.values()))
    check("with the qualification, interest and next action", fields["qualification_status"] == "QUALIFIED" and fields["interest_level"] == "INTERESTED" and fields["next_action"] == "MEETING_BOOKED")
    check("the meeting time as ISO 8601", fields["meeting_start"] == "2026-09-08T10:00+00:00" and fields["meeting_status"] == "BOOKED")
    check("the pain points and objections as lines", fields["pain_points"] == "fuel spend\nno visibility of idling" and fields["objections"] == "price: sounds expensive (handled)")
    check("the summary text", fields["summary"].startswith("What happened:") or "Reached Sara Ali" in fields["summary"])
    check("the ids, for tracing back", fields["call_attempt_id"] == "34" and fields["call_result_id"] == "12" and fields["campaign"] == "Q1 Outreach")
    check("and nothing empty", "callback_scheduled_for" not in fields and "failure_reason" not in fields)

    print("\n=== every disposition has a home ===")
    for disposition, expected in (
        (Disposition.NO_ANSWER, CallOutcome.NO_ANSWER),
        (Disposition.BUSY, CallOutcome.BUSY),
        (Disposition.FAILED, CallOutcome.FAILED),
        (Disposition.VOICEMAIL, CallOutcome.VOICEMAIL),
    ):
        thin = build_call_sync(thin_result(disposition=disposition), prospect(), attempt=attempt(35, status=thin_result(disposition=disposition).call_status, connected_at=None, duration_seconds=None))
        check(f"{disposition.value:<10} -> {expected.value}", thin.activity.outcome is expected)
    for disposition in (Disposition.DO_NOT_CALL, Disposition.OPTED_OUT, Disposition.TRANSFERRED, Disposition.CALLBACK_REQUESTED, Disposition.NOT_INTERESTED, Disposition.QUALIFIED, Disposition.UNQUALIFIED, Disposition.COMPLETED):
        rich = build_call_sync(rich_result(disposition=disposition), prospect())
        check(f"{disposition.value:<18} -> CONNECTED", rich.activity.outcome is CallOutcome.CONNECTED)
    failed = build_call_sync(thin_result(disposition=Disposition.FAILED), prospect())
    check("a failed call carries the carrier's reason", "Reason: SIP 503" in failed.activity.body and failed.activity.fields["failure_reason"] == "SIP 503")
    check("an unanswered call has no qualification section", "Qualification\nStatus:" not in build_call_sync(thin_result(), prospect()).activity.body and "Pain points" not in build_call_sync(thin_result(), prospect()).activity.body)

    callback = rich_result(disposition=Disposition.CALLBACK_REQUESTED, meeting_status=MeetingOutcome.UNKNOWN, meeting_start=None, callback_status=CallbackOutcome.SCHEDULED, callback_scheduled_for=datetime(2026, 9, 9, 5, 0, tzinfo=UTC))
    body = build_call_sync(callback, prospect()).activity.body
    check("a scheduled callback is stated with its time", "Callback\nScheduled for Wed 09 Sep 2026, 10:00 PKT." in body, body[-300:])
    requested = rich_result(callback_status=CallbackOutcome.REQUESTED, callback_when="next Thursday afternoon")
    check("a requested one quotes what they said", 'they said "next Thursday afternoon"' in build_call_sync(requested, prospect()).activity.body)
    agreed = rich_result(meeting_status=MeetingOutcome.AGREED, meeting_start=None, meeting_when="Monday morning")
    check("an agreed meeting says a person must arrange it", "Agreed, not yet booked" in build_call_sync(agreed, prospect()).activity.body)

    anonymous = build_call_sync(rich_result(), prospect(email=None, phone_normalized=None, phone="not a number"))
    check("a prospect with no email and no number has no identity to match on", not anonymous.contact.has_identity)
    try:
        build_call_sync(rich_result(id=None), prospect())
        check("a result that is not stored cannot be synced", False, "no error")
    except ValueError as exc:
        check("a result that is not stored cannot be synced", "id" in str(exc))
    check("without an attempt the result's own time is used", build_call_sync(rich_result(), prospect()).activity.occurred_at == NOW)


class StubResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class StubSession:
    """An `aiohttp.ClientSession` stand-in for the HubSpot client's call shape."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "json": json, "params": dict(params or {})})
        if not self._responses:
            raise AssertionError(f"stub had no response left for {method} {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


def hubspot(responses: list[Any], **kwargs: Any) -> tuple[HubSpotProvider, StubSession]:
    session = StubSession(responses)
    return HubSpotProvider("pat-na1-secret-token-0000", session=session, **kwargs), session


async def _araises(coroutine, exception_type, contains: str = "") -> Any:
    try:
        await coroutine
    except exception_type as exc:
        return exc if contains in str(exc) else None
    except Exception:  # noqa: BLE001
        return None
    return None


async def check_hubspot() -> None:
    """The HubSpot adapter, request by request, against a stub session."""
    print("\n=== hubspot: contacts ===")
    contact = CrmContact(first_name="Sara", last_name="Ali", phone="+923001234567", email="sara@example.com", company="Ravi Logistics", job_title="Ops")

    provider, session = hubspot([StubResponse(200, {"total": 1, "results": [{"id": "501", "properties": {"email": "sara@example.com", "phone": "+923001234567"}}]})])
    found = await provider.find_contact(contact)
    sent = session.requests[0]
    check("searches contacts by email first", sent["method"] == "POST" and sent["url"].endswith("/crm/v3/objects/contacts/search") and sent["json"]["filterGroups"][0]["filters"][0] == {"propertyName": "email", "operator": "EQ", "value": "sara@example.com"})
    check("and returns the id", found == "501")
    check("with the token as a bearer header", sent["headers"]["Authorization"] == "Bearer pat-na1-secret-token-0000")
    check("the description hides the token", "secret" not in provider.describe() and "0000" in provider.describe())

    provider, session = hubspot([
        StubResponse(200, {"results": []}),
        StubResponse(200, {"results": [{"id": "77", "properties": {"phone": "0300 1234567", "email": None}}, {"id": "78", "properties": {"phone": "+923009999999"}}]}),
    ])
    found = await provider.find_contact(contact)
    check("falls back to a phone search", session.requests[1]["json"]["filterGroups"][0]["filters"][0]["propertyName"] == "phone")
    check("and checks the candidates' digits, allowing a missing country code", found == "77")
    provider, _ = hubspot([StubResponse(200, {"results": []}), StubResponse(200, {"results": [{"id": "78", "properties": {"phone": "+923009999999"}}]})])
    check("a candidate with another number is not the person", await provider.find_contact(contact) is None)
    provider, _ = hubspot([StubResponse(200, {"results": [{"id": "9", "properties": {"email": "other@example.com"}}]}), StubResponse(200, {"results": []})])
    check("an email search hit with a different email is not trusted", await provider.find_contact(contact) is None)

    provider, session = hubspot([StubResponse(201, {"id": "600"})])
    created = await provider.create_contact(contact)
    sent = session.requests[0]
    check("creates the contact with the standard properties", sent["url"].endswith("/crm/v3/objects/contacts") and sent["json"]["properties"] == {"firstname": "Sara", "lastname": "Ali", "phone": "+923001234567", "email": "sara@example.com", "company": "Ravi Logistics", "jobtitle": "Ops"} and created == "600")
    provider, _ = hubspot([StubResponse(409, {"status": "error", "message": "Contact already exists. Existing ID: 777", "category": "CONFLICT"})])
    check("a create that collides on email returns the existing id", await provider.create_contact(contact) == "777")
    provider, _ = hubspot([StubResponse(400, {"message": "Property values were not valid", "category": "VALIDATION_ERROR", "errors": [{"message": "email is invalid"}]})])
    rejected = await _araises(provider.create_contact(contact), CrmRejectedError, "email is invalid")
    check("a refusal is a CrmRejectedError with HubSpot's words and the status", rejected is not None and rejected.status == 400)

    activity = build_call_sync(rich_result(), prospect(), campaign=Campaign(id=1, name="Q1"), attempt=attempt(), from_number="+15550001111").activity
    provider, session = hubspot([StubResponse(200, {"id": "600"})])
    await provider.update_contact("600", contact, activity)
    props = session.requests[0]["json"]["properties"]
    check("writes the ai_* properties onto the contact", session.requests[0]["method"] == "PATCH" and session.requests[0]["url"].endswith("/crm/v3/objects/contacts/600") and props["ai_qualification_status"] == "QUALIFIED" and props["ai_next_action"] == "MEETING_BOOKED" and props["ai_last_call_disposition"] == "MEETING_BOOKED")
    check("dates as epoch milliseconds", props["ai_last_call_at"] == str(int((NOW - timedelta(minutes=5)).timestamp() * 1000)) and props["ai_meeting_at"] == str(int(datetime(2026, 9, 8, 10, 0, tzinfo=UTC).timestamp() * 1000)))
    check("the pain points, objections and summary as text", "fuel spend" in props["ai_pain_points"] and "price" in props["ai_objections"] and props["ai_last_call_summary"])
    provider, session = hubspot([], custom_properties=False)
    await provider.update_contact("600", contact, activity)
    check("with custom properties off, the contact is left alone", not session.requests)

    print("\n=== hubspot: the schema ===")
    responses: list[Any] = []
    for index, _ in enumerate(CONTACT_PROPERTIES):
        responses.append(StubResponse(404, {"message": "Property not found"}) if index % 2 else StubResponse(200, {"name": "x"}))
        if index % 2:
            responses.append(StubResponse(201, {"name": "x"}))
    provider, session = hubspot(responses)
    await provider.ensure_schema()
    creates = [r for r in session.requests if r["method"] == "POST"]
    check("reads each property and creates only the missing ones", len(creates) == len(CONTACT_PROPERTIES) // 2 and all(r["url"].endswith("/crm/v3/properties/contacts") for r in creates))
    check("in the contact information group, with a type and a field type", all(r["json"]["groupName"] == "contactinformation" and r["json"]["type"] and r["json"]["fieldType"] for r in creates))
    before = len(session.requests)
    await provider.ensure_schema()
    check("a second call does nothing", len(session.requests) == before)
    provider, _ = hubspot([StubResponse(403, {"message": "This app hasn't been granted all required scopes", "category": "MISSING_SCOPES"})])
    check("a token without the scope raises an auth error the syncer can act on", await _araises(provider.ensure_schema(), CrmAuthError, "scopes") is not None)
    provider, session = hubspot([], custom_properties=False)
    await provider.ensure_schema()
    check("with custom properties off, the properties API is never touched", not session.requests)

    print("\n=== hubspot: calls ===")
    provider, session = hubspot([StubResponse(201, {"id": "9001"})])
    activity_id = await provider.create_activity("600", activity)
    sent = session.requests[0]
    props = sent["json"]["properties"]
    check("creates a call engagement", sent["method"] == "POST" and sent["url"].endswith("/crm/v3/objects/calls") and activity_id == "9001")
    check("associated to the contact with the HubSpot-defined call→contact type", sent["json"]["associations"] == [{"to": {"id": "600"}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": CALL_TO_CONTACT}]}] and CALL_TO_CONTACT == 194)
    check("timestamped in epoch milliseconds", props["hs_timestamp"] == str(int((NOW - timedelta(minutes=5)).timestamp() * 1000)))
    check("outbound, completed, with the Connected disposition", props["hs_call_direction"] == "OUTBOUND" and props["hs_call_status"] == "COMPLETED" and props["hs_call_disposition"] == DISPOSITIONS[CallOutcome.CONNECTED] == "f240bbac-87c9-4f6e-bf70-924b57d47db7")
    check("the duration in milliseconds", props["hs_call_duration"] == "290000")
    check("the title and both numbers", props["hs_call_title"].startswith("AI call — meeting booked") and props["hs_call_from_number"] == "+15550001111" and props["hs_call_to_number"] == "+923001234567")
    check("the body carries the account and ends with the key", "Pain points" in props["hs_call_body"] and props["hs_call_body"].rstrip().endswith("ref aiva12x34"))

    for disposition, guid, status in (
        (Disposition.NO_ANSWER, "73a0d17f-1163-4015-bdd5-ec830791da20", "NO_ANSWER"),
        (Disposition.BUSY, "9d9162e7-6cf3-4944-bf63-4dff82258764", "BUSY"),
        (Disposition.VOICEMAIL, "b2cf5968-551e-4856-9783-52b3da59a7d0", "COMPLETED"),
    ):
        thin = build_call_sync(thin_result(disposition=disposition), prospect()).activity
        provider, session = hubspot([StubResponse(201, {"id": "1"})])
        await provider.create_activity("600", thin)
        props = session.requests[0]["json"]["properties"]
        check(f"{disposition.value:<10} -> HubSpot's built-in disposition and status {status}", props["hs_call_disposition"] == guid and props["hs_call_status"] == status)
    provider, session = hubspot([StubResponse(201, {"id": "1"})])
    await provider.create_activity("600", build_call_sync(thin_result(disposition=Disposition.FAILED), prospect()).activity)
    props = session.requests[0]["json"]["properties"]
    check("a failed call has the FAILED status and no disposition", props["hs_call_status"] == "FAILED" and "hs_call_disposition" not in props and "hs_call_duration" not in props)

    provider, session = hubspot([StubResponse(200, {"id": "9001"})])
    await provider.update_activity("9001", "600", activity)
    check("updates an engagement in place, without re-associating", session.requests[0]["method"] == "PATCH" and session.requests[0]["url"].endswith("/crm/v3/objects/calls/9001") and "associations" not in session.requests[0]["json"])
    provider, session = hubspot([StubResponse(200, {"total": 1, "results": [{"id": "9001", "properties": {"hs_call_title": "x"}}]})])
    check("finds an engagement by the key in its body", await provider.find_activity("aiva12x34") == "9001" and session.requests[0]["json"]["filterGroups"][0]["filters"][0] == {"propertyName": "hs_call_body", "operator": "CONTAINS_TOKEN", "value": "aiva12x34"})
    provider, _ = hubspot([StubResponse(200, {"total": 0, "results": []})])
    check("and None when there is none", await provider.find_activity("aiva12x34") is None)

    print("\n=== hubspot: when it says no ===")
    provider, _ = hubspot([StubResponse(401, {"message": "Authentication credentials not found", "category": "INVALID_AUTHENTICATION"})])
    check("401 is an auth error naming the setting", await _araises(provider.find_activity("k"), CrmAuthError, "HUBSPOT_ACCESS_TOKEN") is not None)
    provider, _ = hubspot([StubResponse(429, {"message": "You have reached your ten secondly limit", "category": "RATE_LIMITS"}, headers={"Retry-After": "7"})])
    limited = await _araises(provider.find_activity("k"), CrmUnavailableError, "rate limit")
    check("429 is unavailable, carrying Retry-After", limited is not None and limited.retry_after_secs == 7.0 and limited.retryable)
    provider, _ = hubspot([StubResponse(502, {"message": "Bad gateway"})])
    check("5xx is unavailable", await _araises(provider.find_activity("k"), CrmUnavailableError, "502") is not None)
    provider, _ = hubspot([TimeoutError()])
    timed = await _araises(provider.create_activity("600", activity), CrmUnavailableError, "did not answer")
    check("a timeout is unavailable and says the outcome is unknown", timed is not None and "unknown" in str(timed))
    provider, session = hubspot([StubResponse(200, {"results": [{"id": "1", "properties": {}}]})])
    check("the credential check reads one contact and writes nothing", await provider.check_credentials() == "contacts readable" and session.requests[0]["method"] == "GET" and session.requests[0]["params"] == {"limit": "1"})
    await provider.close()
    check("a caller-owned session is left open", session.closed is False)


async def check_syncer() -> None:
    """Successful, failed, duplicate and retried — against the rows and the CRM's request log."""
    print("\n=== a successful sync ===")
    setup = build()
    result = setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    mark = _mark()
    report = await setup.syncer.run_once()
    row = setup.store.row(result.id)
    check("one result claimed and synced", report.claimed == 1 and report.synced == 1 and report.describe() == "1 claimed, 1 synced", report.describe())
    check("the CRM was asked, in order: find contact, create contact, create activity, update contact", [name for name, _ in setup.crm.calls] == ["find_contact", "create_contact", "create_activity", "update_contact"], str([n for n, _ in setup.crm.calls]))
    check("the row is SYNCED with both ids", row.state is CrmSyncState.SYNCED and row.external_contact_id == "C1" and row.external_activity_id == "A2")
    check("stamped with when, and with the result version it filed", row.synced_at == NOW and row.result_updated_at == NOW and row.last_error is None and row.attempts == 1)
    check("the activity in the CRM is the mapped one", setup.crm.activities["A2"]["key"] == "aiva12x34" and setup.crm.activities["A2"]["outcome"] is CallOutcome.CONNECTED and "Pain points" in setup.crm.activities["A2"]["body"])
    check("and the contact carries the latest call's facts", setup.crm.contacts["C1"]["latest"]["qualification_status"] == "QUALIFIED")
    check("the schema was asked for once", setup.crm.schema_calls == 1)
    check("logged as synced", _logged("crm.synced", mark) == 1 and _logged("crm.contact_created", mark) == 1)

    print("\n=== a duplicate: the same result again ===")
    again = await setup.syncer.run_once()
    check("a second pass claims nothing", again.claimed == 0 and again.describe() == "nothing to sync")
    fresh = CrmSyncer(setup.store, setup.crm, clock=setup.clock)
    check("a fresh syncer over the same rows claims nothing either", (await fresh.run_once()).claimed == 0)
    check("so the CRM has one activity, created once", len(setup.crm.activities) == 1 and setup.crm.count("create_activity") == 1)

    print("\n=== the result changed after it was filed ===")
    setup.store.touch(result.id, disposition=Disposition.QUALIFIED, meeting_status=MeetingOutcome.AGREED, meeting_start=None)
    mark = _mark()
    report = await setup.syncer.run_once()
    row = setup.store.row(result.id)
    check("the changed result is claimed again", report.claimed == 1 and report.synced == 1)
    check("and the existing activity is updated, not created again", setup.crm.count("create_activity") == 1 and setup.crm.count("update_activity") == 1 and len(setup.crm.activities) == 1)
    check("with the new content", "qualified" in setup.crm.activities["A2"]["title"] and "Agreed, not yet booked" in setup.crm.activities["A2"]["body"])
    check("the contact was found by its recorded id, not searched for again", setup.crm.count("find_contact") == 1 and setup.crm.count("create_contact") == 1)
    check("the row records the new version", row.state is CrmSyncState.SYNCED and row.result_updated_at == setup.store.results[result.id].updated_at and row.attempts == 2)
    check("logged as an update", any("updated activity" in line for line in LOGS[mark:]))

    print("\n=== an existing contact ===")
    setup = build()
    setup.crm.contacts["C9"] = {"email": "sara@example.com", "phone": "+923001234567", "name": "Sara Ali"}
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    await setup.syncer.run_once()
    check("a contact the CRM already has is used, not duplicated", setup.crm.count("create_contact") == 0 and setup.store.row(12).external_contact_id == "C9" and setup.crm.activities["A1"]["contact"] == "C9")

    print("\n=== a transient failure, then a retry ===")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.failures["create_activity"].append(CrmUnavailableError("HubSpot returned 503"))
    mark = _mark()
    report = await setup.syncer.run_once()
    row = setup.store.row(12)
    check("the failure schedules a retry, with the CRM's words and that the outcome is unknown", report.retried == 1 and row.state is CrmSyncState.RETRY and "HubSpot returned 503" in (row.last_error or "") and "unknown" in (row.last_error or ""), str(row.last_error)[:120])
    wait = (row.next_attempt_at - NOW).total_seconds()
    check("due after the base interval, jittered", 54 <= wait <= 66, f"{wait:.1f}s")
    check("the contact id learned before the failure is kept", row.external_contact_id == "C1" and row.external_activity_id is None)
    check("logged with the attempt count", _logged("crm.retry_scheduled", mark) == 1 and "attempt 1/8" in "".join(LOGS[mark:]))
    check("before it is due, it is not claimed", (await setup.syncer.run_once()).claimed == 0)
    setup.clock.advance(70)
    report = await setup.syncer.run_once()
    row = setup.store.row(12)
    check("once due, it is claimed and synced", report.claimed == 1 and report.synced == 1 and row.state is CrmSyncState.SYNCED)
    check("the second attempt searched before creating, found nothing, and created", setup.crm.count("find_activity") == 1 and setup.crm.count("create_activity") == 2 and len(setup.crm.activities) == 1)
    check("without touching the contact again", setup.crm.count("create_contact") == 1 and setup.crm.count("find_contact") == 1)
    check("the row shows two attempts and no error", row.attempts == 2 and row.last_error is None)

    print("\n=== a create whose answer was lost ===")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.lose_answer.add("create_activity")
    report = await setup.syncer.run_once()
    check("the CRM has the activity, and we do not know it", len(setup.crm.activities) == 1 and setup.store.row(12).state is CrmSyncState.RETRY and setup.store.row(12).external_activity_id is None)
    setup.clock.advance(70)
    mark = _mark()
    report = await setup.syncer.run_once()
    row = setup.store.row(12)
    check("the retry finds it by its key and adopts it", report.synced == 1 and row.external_activity_id == "A2" and _logged("crm.activity_found", mark) == 1)
    check("so the CRM still has exactly one activity for the call", len(setup.crm.activities) == 1 and setup.crm.count("create_activity") == 1 and setup.crm.count("update_activity") == 1)

    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.lose_answer.add("create_contact")
    await setup.syncer.run_once()
    check("a contact create whose answer was lost is retried", setup.store.row(12).state is CrmSyncState.RETRY and setup.store.row(12).external_contact_id is None)
    setup.clock.advance(70)
    await setup.syncer.run_once()
    check("and the retry finds the contact the CRM already made", setup.store.row(12).state is CrmSyncState.SYNCED and len(setup.crm.contacts) == 1 and setup.crm.count("create_contact") == 1)

    print("\n=== a permanent failure ===")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.failures["create_activity"].append(CrmRejectedError("HubSpot refused POST /crm/v3/objects/calls (HTTP 400): Property hs_call_disposition is invalid", status=400))
    mark = _mark()
    report = await setup.syncer.run_once()
    row = setup.store.row(12)
    check("a refusal on the merits is FAILED at once, with the CRM's words", report.failed == 1 and row.state is CrmSyncState.FAILED and "hs_call_disposition" in (row.last_error or ""))
    check("and not claimed again", (await setup.syncer.run_once()).claimed == 0)
    check("logged as failed, naming the way back", _logged("crm.failed", mark) == 1 and "crm-retry" in "".join(LOGS[mark:]))
    reopened = await setup.store.retry_crm_sync(call_result_id=12)
    report = await setup.syncer.run_once()
    check("crm-retry reopens it and the next pass files it", reopened == 1 and report.synced == 1 and setup.store.row(12).state is CrmSyncState.SYNCED)

    print("\n=== out of attempts ===")
    setup = build(max_attempts=3, retry_secs=10.0)
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    # A write, not a read: reads are retried three times inside a pass, so a
    # scripted read failure would be spent before the second pass.
    setup.crm.failures["create_activity"] = [CrmUnavailableError("down")] * 5
    outcomes = []
    for _ in range(3):
        report = await setup.syncer.run_once()
        outcomes.append((report.retried, report.failed))
        setup.clock.advance(60)
    row = setup.store.row(12)
    check("two transient failures retry, the third closes it", outcomes == [(1, 0), (1, 0), (0, 1)] and row.state is CrmSyncState.FAILED, str(outcomes))
    check("with the attempt count in the reason", "after 3 attempts" in (row.last_error or ""))
    check("the contact was created once and kept across the attempts", setup.crm.count("create_contact") == 1 and row.external_contact_id == "C1")
    setup = build(retry_secs=60.0)
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.failures["create_activity"] = [CrmUnavailableError("down")] * 2
    await setup.syncer.run_once()
    first = (setup.store.row(12).next_attempt_at - setup.clock()).total_seconds()
    setup.clock.advance(70)
    await setup.syncer.run_once()
    second = (setup.store.row(12).next_attempt_at - setup.clock()).total_seconds()
    check("the second wait is twice the first", 54 <= first <= 66 and 108 <= second <= 132, f"{first:.0f}s then {second:.0f}s")
    setup = build(retry_secs=10.0)
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.failures["create_activity"].append(CrmUnavailableError("rate limited", retry_after_secs=120.0))
    await setup.syncer.run_once()
    wait = (setup.store.row(12).next_attempt_at - setup.clock()).total_seconds()
    check("the CRM's own Retry-After is honoured when it is longer", 108 <= wait <= 132, f"{wait:.0f}s")

    print("\n=== a rejected token ===")
    setup = build()
    setup.store.add(rich_result(12, 34), prospect_row=prospect(), attempt_row=attempt())
    setup.store.add(rich_result(13, 35, disposition=Disposition.COMPLETED), attempt_row=attempt(35))
    setup.crm.failures["find_contact"].append(CrmAuthError("HubSpot rejected the token (HTTP 401)"))
    mark = _mark()
    report = await setup.syncer.run_once()
    check("the pass stops at the first row", report.claimed == 2 and report.stopped is not None and "401" in report.stopped and report.synced == 0)
    check("nothing else is sent to the CRM", setup.crm.count("find_contact") == 1 and len(setup.crm.calls) == 1)
    rows = [setup.store.row(12), setup.store.row(13)]
    check("both rows are handed back for a later retry, no attempt spent", all(r.state is CrmSyncState.RETRY and r.attempts == 0 and r.next_attempt_at is not None for r in rows))
    check("and the reason is on them", all("401" in (r.last_error or "") for r in rows))
    check("logged once as an auth failure", _logged("crm.auth_rejected", mark) == 1)
    setup.clock.advance(70)
    report = await setup.syncer.run_once()
    check("once the token is fixed, the next pass files both", report.synced == 2 and len(setup.crm.activities) == 2)

    print("\n=== policy: unanswered calls ===")
    setup = build(sync_unanswered=False)
    setup.store.add(thin_result(), prospect_row=prospect(), attempt_row=attempt(35, status=CallAttemptStatus.NO_ANSWER))
    report = await setup.syncer.run_once()
    check("with CRM_SYNC_UNANSWERED off, a no-answer is skipped and recorded so", report.skipped == 1 and setup.store.row(13).state is CrmSyncState.SKIPPED and not setup.crm.calls)
    check("and not claimed again", (await setup.syncer.run_once()).claimed == 0)
    setup = build(sync_unanswered=True)
    setup.store.add(thin_result(), prospect_row=prospect(), attempt_row=attempt(35, status=CallAttemptStatus.NO_ANSWER))
    report = await setup.syncer.run_once()
    check("with it on, the no-answer is filed as such", report.synced == 1 and setup.crm.activities["A2"]["outcome"] is CallOutcome.NO_ANSWER)

    print("\n=== what cannot be filed ===")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(email=None, phone_normalized=None), attempt_row=attempt())
    report = await setup.syncer.run_once()
    check("a prospect with nothing to match on is FAILED with the reason", report.failed == 1 and "neither a phone number nor an email" in (setup.store.row(12).last_error or "") and not setup.crm.calls)
    setup = build()
    setup.store.add(rich_result(), attempt_row=attempt())
    report = await setup.syncer.run_once()
    check("a prospect row that is gone likewise", report.failed == 1 and "prospect row is gone" in (setup.store.row(12).last_error or ""))

    print("\n=== the CRM's schema cannot be created ===")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.crm.schema_error = CrmAuthError("This app hasn't been granted all required scopes")
    mark = _mark()
    report = await setup.syncer.run_once()
    check("the sync carries on with standard fields, warning once", report.synced == 1 and not setup.crm.custom and _logged("crm.schema_unavailable", mark) == 1)
    setup.store.add(rich_result(13, 35), attempt_row=attempt(35))
    await setup.syncer.run_once()
    check("and does not ask again in the same run", setup.crm.schema_calls == 1)

    print("\n=== the database is not there ===")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    setup.store.fail_with = CampaignStoreError("the database went away")
    mark = _mark()
    report = await setup.syncer.run_once()
    check("a claim that fails is reported, not raised", report.claimed == 0 and report.notes and _logged("crm.claim_failed", mark) == 1)
    setup.store.fail_with = None
    check("and the next pass works", (await setup.syncer.run_once()).synced == 1)

    print("\n=== a bug in one row ===")
    setup = build()
    setup.store.add(rich_result(12, 34), prospect_row=prospect(), attempt_row=attempt())
    setup.store.add(rich_result(13, 35), attempt_row=attempt(35))
    setup.crm.failures["create_activity"].append(RuntimeError("something unexpected"))
    mark = _mark()
    report = await setup.syncer.run_once()
    check("an unexpected error retries that row and files the other", report.retried == 1 and report.synced == 1 and _logged("crm.sync_crashed", mark) == 1)

    print("\n=== the run loop ===")
    slept: list[float] = []
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())

    async def fake_sleep(secs: float) -> None:
        slept.append(secs)
        if len(slept) >= 2:
            setup.syncer.request_stop()

    setup.syncer._sleep = fake_sleep  # noqa: SLF001 - the check's clock
    totals = await setup.syncer.run(poll_secs=15.0)
    check("a pass that found work is followed at once by another; an idle one waits the poll interval", slept == [15.0, 15.0] and totals.passes == 3 and totals.synced == 1, f"slept={slept} {totals.describe()}")
    setup = build()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=attempt())
    totals = await setup.syncer.run(once=True)
    check("--once runs exactly one pass", totals.passes == 1 and totals.synced == 1)

    print("\n=== the boundary ===")
    offenders = []
    for path in [SERVER / "bot.py", *sorted((SERVER / "src" / "conversation").glob("*.py")), *sorted((SERVER / "src" / "campaigns").glob("*.py")), *sorted(p for p in (SERVER / "src" / "reliability").glob("*.py") if p.name != "health.py"), *sorted((SERVER / "src" / "telephony").glob("*.py"))]:
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+(src\.crm|\.\.crm|\.crm)\b", text, re.MULTILINE):
            offenders.append(path.name)
    check("nothing on the call path imports the CRM package: not the bot, the conversation, the campaigns, the telephony", not offenders, str(offenders))
    check("except the health check, lazily, inside its own function", "from ..crm import" in (SERVER / "src" / "reliability" / "health.py").read_text(encoding="utf-8"))
    crm_text = "".join(p.read_text(encoding="utf-8") for p in (SERVER / "src" / "crm").glob("*.py"))
    check("and the CRM package imports no Pipecat", "pipecat" not in crm_text)

    print("\n=== configuration ===")
    names = ("CRM_PROVIDER", "HUBSPOT_ACCESS_TOKEN", "CRM_SYNC_UNANSWERED", "CRM_SYNC_MAX_ATTEMPTS", "CRM_SYNC_RETRY_SECS", "CRM_SYNC_MAX_RETRY_SECS", "CRM_CUSTOM_PROPERTIES")
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        config = Config.from_env()
        check("with nothing set, the CRM is off and the startup line says so", not config.crm.enabled and "CRM=off" in config.describe())
        try:
            make_crm_provider(config.crm)
            check("and a provider cannot be built", False, "built")
        except ConfigError as exc:
            check("and a provider cannot be built", "CRM_PROVIDER" in str(exc))
        os.environ["CRM_PROVIDER"] = "hubspot"
        try:
            Config.from_env()
            check("hubspot without a token is a config problem naming the scopes", False, "accepted")
        except ConfigError as exc:
            check("hubspot without a token is a config problem naming the scopes", "HUBSPOT_ACCESS_TOKEN" in str(exc) and "crm.objects.calls" in str(exc))
        os.environ["HUBSPOT_ACCESS_TOKEN"] = "pat-na1-secret-token-0000"
        os.environ["CRM_SYNC_MAX_ATTEMPTS"] = "5"
        os.environ["CRM_SYNC_RETRY_SECS"] = "30"
        os.environ["CRM_SYNC_UNANSWERED"] = "false"
        config = Config.from_env()
        check("the settings are read", config.crm.enabled and config.crm.sync_max_attempts == 5 and config.crm.sync_retry_secs == 30.0 and not config.crm.sync_unanswered)
        check("and described without the token", "hubspot" in config.crm.describe() and "answered calls only" in config.crm.describe() and "secret" not in config.describe())
        provider = make_crm_provider(config.crm)
        check("the provider is built from them", isinstance(provider, HubSpotProvider) and provider.custom_properties)
        os.environ["CRM_PROVIDER"] = "salesforce"
        try:
            Config.from_env()
            check("an unknown CRM is a config problem", False, "accepted")
        except ConfigError as exc:
            check("an unknown CRM is a config problem", "CRM_PROVIDER" in str(exc))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def run_database_checks(dsn: str) -> None:
    """The table, the claim, and the syncer, against real rows in a schema that is thrown away."""
    from test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== crm_sync, against PostgreSQL ===")
        service = CampaignService(store, default_region="PK", max_attempts=2, retry_minutes=60)
        campaign = await service.create_campaign(f"CRM {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        person = await service.create_prospect(first_name="Sara", last_name="Ali", phone="0300 1234567", email="sara@example.com")

        async def finished(status: CallAttemptStatus = CallAttemptStatus.NO_ANSWER, who: Prospect = person) -> CallResult:
            row = await store.create_attempt(prospect_id=who.id, campaign_id=campaign.id, status=status)
            stored = await store.save_call_result(build_carrier_result(row))
            assert stored is not None
            return stored

        result = await finished()
        now = datetime.now(UTC)
        check("create_schema makes the table", (await store.crm_sync_counts()) == {"UNSEEN": 1})
        claimed = await store.claim_results_for_sync("mock", limit=10, now=now)
        check("a result the CRM has not seen is claimed", len(claimed) == 1 and claimed[0][0].id == result.id)
        record = claimed[0][1]
        check("as SYNCING, attempt 1, keyed from the result and the attempt", record.state is CrmSyncState.SYNCING and record.attempts == 1 and record.sync_key == sync_key(result.id, result.call_attempt_id) and record.started_at is not None)
        check("a second claim gets nothing while it is being synced", await store.claim_results_for_sync("mock", limit=10, now=now) == [])
        check("nor a claim for another provider", await store.claim_results_for_sync("other", limit=10, now=now) == [])
        synced = await store.record_crm_sync(record.id, state=CrmSyncState.SYNCED, external_contact_id="C1", external_activity_id="A1", synced_at=now, result_updated_at=result.updated_at, error=None)
        check("the outcome is written", synced.state is CrmSyncState.SYNCED and synced.external_activity_id == "A1" and synced.last_error is None)
        check("and the row is not claimed again", await store.claim_results_for_sync("mock", limit=10, now=now + timedelta(hours=1)) == [])
        check("get_crm_sync finds it by the result", (await store.get_crm_sync(result.id)).id == record.id)

        print("\n  the result changes after it was filed:")
        attempt_row = await store.get_attempt(result.call_attempt_id)
        await asyncio.sleep(0.05)
        rewritten = await store.save_call_result(build_carrier_result(dataclasses.replace(attempt_row, status=CallAttemptStatus.BUSY)))
        check("the rewrite advances updated_at", rewritten is not None and rewritten.updated_at > result.updated_at)
        claimed = await store.claim_results_for_sync("mock", limit=10, now=now + timedelta(hours=1))
        check("so the row is claimed again, keeping its ids", len(claimed) == 1 and claimed[0][1].external_activity_id == "A1" and claimed[0][1].attempts == 2)
        await store.record_crm_sync(claimed[0][1].id, state=CrmSyncState.SYNCED, synced_at=now, result_updated_at=rewritten.updated_at, error=None)

        print("\n  retry timing and stale claims:")
        later = await finished()
        claimed = await store.claim_results_for_sync("mock", limit=10, now=now)
        retry_row = claimed[0][1]
        await store.record_crm_sync(retry_row.id, state=CrmSyncState.RETRY, error="503", next_attempt_at=now + timedelta(minutes=5))
        check("a RETRY row is not claimed before it is due", await store.claim_results_for_sync("mock", limit=10, now=now + timedelta(minutes=1)) == [])
        claimed = await store.claim_results_for_sync("mock", limit=10, now=now + timedelta(minutes=6))
        check("and is once due, with the attempt counted", len(claimed) == 1 and claimed[0][0].id == later.id and claimed[0][1].attempts == 2 and claimed[0][1].last_error == "503")
        await admin.execute(f'UPDATE "{schema}".crm_sync SET started_at = now() - interval \'2 hours\' WHERE id = $1', retry_row.id)
        claimed = await store.claim_results_for_sync("mock", limit=10, now=datetime.now(UTC), stale_secs=900)
        check("a SYNCING row older than stale_secs is claimed again — its syncer died", len(claimed) == 1 and claimed[0][1].id == retry_row.id and claimed[0][1].attempts == 3)
        await store.record_crm_sync(retry_row.id, state=CrmSyncState.FAILED, error="gave up")
        counts = await store.crm_sync_counts()
        check("the counts say where every result is", counts.get("SYNCED") == 1 and counts.get("FAILED") == 1 and counts.get("UNSEEN") == 0, str(counts))
        check("a FAILED row is listed by state", [r.id for r in await store.list_crm_sync(state=CrmSyncState.FAILED)] == [retry_row.id])
        check("crm-retry reopens it", await store.retry_crm_sync(all_failed=True) == 1 and (await store.get_crm_sync(later.id)).state is CrmSyncState.PENDING)
        check("and reopens nothing twice", await store.retry_crm_sync(all_failed=True) == 0)

        print("\n  concurrent claims:")
        others = [await service.create_prospect(first_name="P", last_name=str(n), phone=f"0301 000000{n}") for n in range(6)]
        for who in others:
            await finished(who=who)
        batches = await asyncio.gather(*(store.claim_results_for_sync("mock", limit=2, now=datetime.now(UTC)) for _ in range(4)))
        ids = [record.id for batch in batches for _, record in batch]
        check("four simultaneous claims hand out disjoint rows", len(ids) == len(set(ids)) and len(ids) == 7, f"{[len(b) for b in batches]}")

        print("\n  the syncer over the real store:")
        for batch in batches:
            for _, record in batch:
                await admin.execute(f'UPDATE "{schema}".crm_sync SET state = \'PENDING\', attempts = 0 WHERE id = $1', record.id)
        crm = MockCrm()
        syncer = CrmSyncer(store, crm, from_number="+15550001111", batch=10)
        report = await syncer.run_once()
        check("every open row is filed", report.claimed == 7 and report.synced == 7 and len(crm.activities) == 7, report.describe())
        check("one contact per person", len(crm.contacts) == 7)
        check("and a second pass finds nothing left", (await syncer.run_once()).claimed == 0)
        rows = await store.list_crm_sync(state=CrmSyncState.SYNCED, limit=20)
        check("every row carries its CRM ids and the result version it filed", len(rows) == 8 and all(r.external_activity_id and r.external_contact_id and r.result_updated_at for r in rows))
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("CRM checks — the mapping, the HubSpot adapter over a stub, the syncer over a fake store, and the rows.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_mapping()
        await check_hubspot()
        await check_syncer()

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
