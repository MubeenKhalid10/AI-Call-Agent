#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for production booking and transfer. Phase 16. No keys, no phone, no audio.

Run it from the `server/` directory::

    uv run python tests/test_booking_transfer.py

**What this is for.** Phase 7 built the booking and transfer actions; Phase 16
makes them safe against the world: a calendar that does not answer, a slot
taken between the offer and the write, a colleague who does not pick up. So
the checks are arranged around what can go wrong *after* the agent has done
the right thing, and assert on both sides — what the vendor was asked, and
what the rows say afterwards.

**Stubs, not mocks — and the real code in the middle.** The Cal.com client is
the real one over a stub HTTP session that can time out; the transfer TwiML
and the `<Dial action>` report are read by the real `TwilioProvider` and
applied by the real `WebhookProcessor`; the real `ActionService` is driven
directly, with a fake carrier and a fake store, because the conversation
above it is unchanged and has its own script. The SQL — the no-double-booking
constraint and the transfers table — is checked against PostgreSQL in a
throwaway schema, skipped with a message when none is reachable.

A plain script rather than a pytest suite, like the other fifteen. Exit
status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from test_telephony import StubResponse as TwilioStubResponse  # noqa: E402
from test_telephony import StubSession as TwilioStubSession  # noqa: E402
from test_telephony import call_resource  # noqa: E402
from test_webhooks import ACCOUNT, TOKEN, URL, LedgerStore, signed, twilio_fields  # noqa: E402
from test_worker import NOW, FakeClock, ScriptedCarrier  # noqa: E402

from src.actions import ActionService  # noqa: E402
from src.campaigns import (  # noqa: E402
    CallAttemptStatus,
    CallTransfer,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    CampaignStoreError,
    MeetingConflictError,
    TransferStatus,
    WebhookOutcome,
    WebhookProcessor,
    create_webhook_router,
)
from src.config import Config  # noqa: E402
from src.conversation import AttendeeDetails, CallBrief, CampaignBrief, ProspectBrief  # noqa: E402
from src.conversation.results import EXTERNAL_ERROR, SLOT_TAKEN, TRANSFER_FAILED  # noqa: E402
from src.reliability import HealthChecker, Status  # noqa: E402
from src.scheduling import (  # noqa: E402
    Attendee,
    Booking,
    CalendarError,
    CalendarProvider,
    CalendarUnavailableError,
    Slot,
    SlotUnavailableError,
)
from src.scheduling.calcom import CalComProvider  # noqa: E402
from src.telephony import (  # noqa: E402
    WEBHOOK_TRANSFER,
    CallSession,
    ProviderUnavailableError,
    TelephonyProvider,
    TransferError,
    build_transfer_twiml,
    transfer_response_twiml,
)
from src.telephony.session import DIRECTION_OUTBOUND  # noqa: E402
from src.telephony.twilio import TwilioProvider  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []

KARACHI = ZoneInfo("Asia/Karachi")
MONDAY = datetime(2026, 9, 7, 0, 0, tzinfo=KARACHI)


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _logged(name: str, since: int = 0) -> int:
    return sum(1 for line in LOGS[since:] if name in line)


def _mark() -> int:
    return len(LOGS)


async def _araises(coroutine, exception_type, contains: str = "") -> Any:
    try:
        await coroutine
    except exception_type as exc:
        return exc if contains in str(exc) else None
    except Exception:  # noqa: BLE001
        return None
    return None


# --- Transfer: the TwiML and the carrier -------------------------------------


def check_transfer_twiml() -> None:
    """What the carrier is told when a call is handed over, with and without a receiver."""
    print("\n=== transfer TwiML ===")
    inline = build_transfer_twiml("+923009999999", caller_id="+15550001111", timeout_secs=25)
    check("without a receiver, the fallback is inline after the Dial", "<Say>" in inline and "<Hangup />" in inline and 'timeout="25"' in inline and "action=" not in inline)

    tracked = build_transfer_twiml("+923009999999", caller_id="+15550001111", timeout_secs=20, action_url=URL)
    check("with a receiver, the Dial reports to it", f'action="{URL}"' in tracked and 'method="POST"' in tracked and 'timeout="20"' in tracked and 'callerId="+15550001111"' in tracked, tracked)
    check("and nothing follows the Dial — the receiver decides what the caller hears", "<Say>" not in tracked and "<Hangup" not in tracked)
    check("the destination is the Dial's text", ">+923009999999</Dial>" in tracked)
    hostile = build_transfer_twiml("+923009999999", action_url="https://h/x?a=1&b=2")
    check("the action URL is escaped", 'action="https://h/x?a=1&amp;b=2"' in hostile)

    answered = transfer_response_twiml(True)
    missed = transfer_response_twiml(False)
    check("after an answered leg the caller is simply hung up", "<Hangup />" in answered and "<Say>" not in answered)
    check("after a missed one they hear the fallback, then hang up", "<Say>" in missed and "nobody is available" in missed and missed.index("<Say>") < missed.index("<Hangup />"))
    check("a custom fallback is escaped", "&amp;" in transfer_response_twiml(False, fallback_message="A & B"))


async def check_transfer_provider() -> None:
    """The live-call update the carrier receives."""
    print("\n=== transfer through the carrier ===")
    session = TwilioStubSession([TwilioStubResponse(200, call_resource(status="in-progress"))])
    provider = TwilioProvider(ACCOUNT, TOKEN, session=session)
    await provider.transfer_call("CA1", "+923009999999", caller_id="+15550001111", action_url=URL, timeout_secs=20)
    method, url, sent = session.requests[0]
    twiml = sent["data"].get("Twiml", "")
    check("POSTs new TwiML to the call resource", method == "POST" and url.endswith("/Calls/CA1.json"))
    check("with the receiver as the Dial's action and the configured ring time", f'action="{URL}"' in twiml and 'timeout="20"' in twiml)
    check("and the call is not hung up", "Status" not in sent["data"])
    session = TwilioStubSession([TwilioStubResponse(200, call_resource(status="in-progress"))])
    provider = TwilioProvider(ACCOUNT, TOKEN, session=session)
    await provider.transfer_call("CA1", "+923009999999")
    check("without a receiver, the Phase 7 TwiML is sent unchanged", "<Say>" in session.requests[0][2]["data"]["Twiml"] and "action=" not in session.requests[0][2]["data"]["Twiml"])

    print("\n=== reading the carrier's report ===")
    provider = TwilioProvider(ACCOUNT, TOKEN)
    fields = twilio_fields("CA1", "in-progress", DialCallStatus="completed", DialCallSid="CAdial1", DialCallDuration="95")
    parsed = provider.parse_webhook(signed(fields))
    check("a Dial report is a transfer event, whatever the parent call's status says", parsed.kind == WEBHOOK_TRANSFER and parsed.status is None)
    check("with the leg's ending, id and duration", parsed.raw_status == "completed" and parsed.dial_call_id == "CAdial1" and parsed.duration_secs == 95.0)
    check("keyed on the leg's id", parsed.key == "twilio:CA1:transfer:CAdial1", parsed.key)
    check("and it knows the colleague answered", parsed.transfer_answered)
    missed = provider.parse_webhook(signed(twilio_fields("CA1", "in-progress", DialCallStatus="no-answer")))
    check("a missed leg says so", missed.raw_status == "no-answer" and not missed.transfer_answered and missed.key == "twilio:CA1:transfer:no-answer")


# --- Transfer: the receiver and the rows --------------------------------------


@dataclass
class TransferStore(LedgerStore):
    """Phase 14's in-memory store, plus the transfers table."""

    transfers: list[CallTransfer] = field(default_factory=list)

    async def add_transfer(self, **fields: Any) -> CallTransfer:
        self._guard()
        row = CallTransfer(id=self._next("transfer"), requested_at=self.now(), updated_at=self.now(), **fields)
        self.transfers.append(row)
        return row

    async def complete_transfer(self, telephony_call_id: str, *, status: TransferStatus, provider: str, dial_call_id=None, duration_seconds=None, error=None, to_number=None, call_attempt_id=None, prospect_id=None) -> CallTransfer | None:
        self._guard()
        for index in range(len(self.transfers) - 1, -1, -1):
            row = self.transfers[index]
            if row.telephony_call_id != telephony_call_id:
                continue
            if row.status is not TransferStatus.REQUESTED:
                return row
            done = dataclasses.replace(row, status=status, dial_call_id=dial_call_id or row.dial_call_id, duration_seconds=duration_seconds if duration_seconds is not None else row.duration_seconds, error=error, completed_at=self.now(), updated_at=self.now())
            self.transfers[index] = done
            return done
        orphan = CallTransfer(id=self._next("transfer"), telephony_call_id=telephony_call_id, provider=provider, to_number=to_number or "unknown", status=status, call_attempt_id=call_attempt_id, prospect_id=prospect_id, dial_call_id=dial_call_id, duration_seconds=duration_seconds, error=error, requested_at=self.now(), completed_at=self.now())
        self.transfers.append(orphan)
        return orphan

    async def list_transfers(self, *, call_attempt_id=None, limit=50) -> list[CallTransfer]:
        rows = [t for t in self.transfers if call_attempt_id is None or t.call_attempt_id == call_attempt_id]
        return sorted(rows, key=lambda t: -t.id)[:limit]

    def for_call(self, call_id: str) -> CallTransfer | None:
        return next((t for t in reversed(self.transfers) if t.telephony_call_id == call_id), None)


@dataclass
class World:
    clock: FakeClock
    store: TransferStore
    service: CampaignService
    carrier: ScriptedCarrier
    dialer: CampaignDialer
    processor: WebhookProcessor
    call_id: str = ""
    attempt_id: int = 0

    async def place(self) -> None:
        campaign = await self.store.create_campaign(name="Transfers", status=CampaignStatus.ACTIVE)
        prospect = await self.store.add_prospect(first_name="Sara", last_name="Ali", phone="+923001234567", phone_normalized="+923001234567")
        await self.store.add_to_campaign(campaign.id, prospect.id)
        result = await self.dialer.dial_next(campaign.id)
        assert result.placed, result.describe()
        self.call_id = result.snapshot.call_id
        self.attempt_id = result.attempt.id
        await self.store.apply_call_event(attempt_id=self.attempt_id, status=CallAttemptStatus.CONNECTED)

    async def requested(self, reason: str = "wants a person") -> CallTransfer:
        return await self.store.add_transfer(telephony_call_id=self.call_id, provider="twilio", to_number="+923009999999", call_attempt_id=self.attempt_id, prospect_id=1, reason=reason)

    async def report(self, dial_status: str, **extra: str):
        fields = twilio_fields(self.call_id, "in-progress", DialCallStatus=dial_status, **extra)
        return await self.processor.receive(signed(fields))


def build_world() -> World:
    clock = FakeClock(NOW)
    store = TransferStore(clock=clock)
    service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
    carrier = ScriptedCarrier(clock, script=[CallStatus_RINGING(), CallStatus_ANSWERED()])
    dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://abc123.ngrok.app", status_callback_url=URL)
    processor = WebhookProcessor(service, TwilioProvider(ACCOUNT, TOKEN), expected_url=URL)
    return World(clock=clock, store=store, service=service, carrier=carrier, dialer=dialer, processor=processor)


def CallStatus_RINGING():  # noqa: N802 - keeps the world builder readable
    from src.telephony import CallStatus

    return CallStatus.RINGING


def CallStatus_ANSWERED():  # noqa: N802
    from src.telephony import CallStatus

    return CallStatus.ANSWERED


async def check_transfer_receiver() -> None:
    """The carrier's report, recorded and answered."""
    print("\n=== a transfer that was answered ===")
    world = build_world()
    await world.place()
    await world.requested()
    mark = _mark()
    receipt = await world.report("completed", DialCallSid="CAdial1", DialCallDuration="95")
    row = world.store.for_call(world.call_id)
    check("the report is accepted as a transfer event", receipt.accepted and receipt.outcome == WebhookOutcome.TRANSFER, receipt.detail)
    check("the transfer row is ANSWERED with the leg's id and duration", row.status is TransferStatus.ANSWERED and row.dial_call_id == "CAdial1" and row.duration_seconds == 95 and row.error is None and row.completed_at is not None)
    check("the carrier is answered with TwiML that hangs up", receipt.media_type == "application/xml" and receipt.body is not None and "<Hangup />" in receipt.body and "<Say>" not in receipt.body)
    check("the prospect's own attempt is untouched", (await world.store.get_attempt(world.attempt_id)).status is CallAttemptStatus.CONNECTED)
    check("logged as completed", _logged("transfer.completed", mark) == 1 and "ANSWERED" in "".join(LOGS[mark:]))
    check("on the ledger", world.store.rows_for(world.call_id)[-1].outcome == "transfer")

    print("\n=== a transfer nobody answered ===")
    world = build_world()
    await world.place()
    await world.requested()
    receipt = await world.report("no-answer", DialCallSid="CAdial2")
    row = world.store.for_call(world.call_id)
    check("NO_ANSWER, with the reason on the row", row.status is TransferStatus.NO_ANSWER and "no-answer" in (row.error or "") and row.duration_seconds is None)
    check("and the caller hears the fallback before the hang-up", receipt.body is not None and "<Say>" in receipt.body and "nobody is available" in receipt.body)
    for dial_status, expected in (("busy", TransferStatus.BUSY), ("failed", TransferStatus.FAILED), ("canceled", TransferStatus.CANCELED)):
        world = build_world()
        await world.place()
        await world.requested()
        await world.report(dial_status, DialCallSid=f"CA{dial_status}")
        check(f"{dial_status:<9} -> {expected.value}", world.store.for_call(world.call_id).status is expected)

    print("\n=== reports that could go wrong ===")
    world = build_world()
    await world.place()
    await world.requested()
    await world.report("completed", DialCallSid="CAdial3", DialCallDuration="10")
    again = await world.report("completed", DialCallSid="CAdial3", DialCallDuration="10")
    check("a redelivered report is a duplicate that still answers with TwiML", again.outcome == WebhookOutcome.DUPLICATE and again.body is not None and "<Hangup />" in again.body)
    late = await world.report("no-answer", DialCallSid="CAdial4")
    check("a second, different report does not rewrite a finished transfer", world.store.for_call(world.call_id).status is TransferStatus.ANSWERED and late.accepted)

    world = build_world()
    await world.place()
    receipt = await world.report("no-answer", DialCallSid="CAdial5")
    row = world.store.for_call(world.call_id)
    check("a report with no requested row still records the outcome", row is not None and row.status is TransferStatus.NO_ANSWER and row.call_attempt_id == world.attempt_id)
    stranger = await world.processor.receive(signed(twilio_fields("CAnobody", "in-progress", DialCallStatus="completed", DialCallSid="CAdial6")))
    check("a report for a call this database never placed is recorded and answered too", stranger.accepted and stranger.body is not None and world.store.for_call("CAnobody").status is TransferStatus.ANSWERED and world.store.for_call("CAnobody").call_attempt_id is None)
    odd = await world.report("teleported", DialCallSid="CAdial7")
    check("an unknown dial status is ignored, and the caller still hears the fallback", odd.outcome == WebhookOutcome.IGNORED and odd.body is not None and "<Say>" in odd.body)
    forged = await world.processor.receive(signed(twilio_fields(world.call_id, "in-progress", DialCallStatus="completed"), secret="attacker"))
    check("a forged report is refused and answers no TwiML", forged.http_status == 403 and forged.body is None)

    print("\n=== through the HTTP route ===")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    world = build_world()
    await world.place()
    await world.requested()

    async def get_processor() -> WebhookProcessor:
        return world.processor

    app = FastAPI()
    app.include_router(create_webhook_router(get_processor, path="/webhooks/telephony"))
    with TestClient(app) as client:
        fields = twilio_fields(world.call_id, "in-progress", DialCallStatus="busy", DialCallSid="CAdial8")
        from test_webhooks import reference_signature

        response = client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature(TOKEN, URL, fields)})
        check("the route answers a Dial report with XML", response.status_code == 200 and response.headers["content-type"].startswith("application/xml") and "<Say>" in response.text)
        status = client.post("/webhooks/telephony", data=twilio_fields(world.call_id, "completed", 3, CallDuration="120"), headers={"X-Twilio-Signature": reference_signature(TOKEN, URL, twilio_fields(world.call_id, "completed", 3, CallDuration="120"))})
        check("and an ordinary status event with plain text, as before", status.status_code == 200 and status.text == "applied")
    check("the transfer row shows BUSY", world.store.for_call(world.call_id).status is TransferStatus.BUSY)


# --- The action service ----------------------------------------------------------


class FakeCarrier(TelephonyProvider):
    name = "fake"
    transports = ("twilio",)

    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode
        self.transfers: list[dict[str, Any]] = []

    async def place_call(self, request):  # pragma: no cover
        raise NotImplementedError

    async def fetch_call(self, call_id):  # pragma: no cover
        raise NotImplementedError

    async def hang_up(self, call_id):  # pragma: no cover
        raise NotImplementedError

    def make_serializer(self, call_data):  # pragma: no cover
        raise NotImplementedError

    async def transfer_call(self, call_id, to_number, *, caller_id=None, action_url=None, timeout_secs=30):
        if self.mode == "refuse":
            raise TransferError("the call has already ended, so it cannot be transferred")
        if self.mode == "unreachable":
            raise ProviderUnavailableError("could not reach the carrier")
        self.transfers.append({"call_id": call_id, "to": to_number, "caller_id": caller_id, "action_url": action_url, "timeout": timeout_secs})


class FakeActionStore:
    def __init__(self, *, failing: bool = False, conflict: bool = False, slow: bool = False) -> None:
        self.transfers: list[dict[str, Any]] = []
        self.meetings: list[dict[str, Any]] = []
        self.failing = failing
        self.conflict = conflict
        self.slow = slow

    async def add_transfer(self, **fields: Any) -> Any:
        if self.failing:
            raise CampaignStoreError("the database went away")
        if self.slow:
            await asyncio.sleep(10)
        self.transfers.append(fields)
        return type("Transfer", (), {"id": len(self.transfers)})()

    async def add_meeting(self, **fields: Any) -> Any:
        if self.conflict:
            raise MeetingConflictError("that time has just been taken (2026-09-07 10:00 PKT overlaps a booking)")
        self.meetings.append(fields)
        return type("Meeting", (), {"id": len(self.meetings)})()


class LocalLikeCalendar(CalendarProvider):
    """A calendar whose booking is the row — like the local provider."""

    name = "local"
    requires_email = False

    async def available_slots(self, start, end):
        return [Slot(start=MONDAY.replace(hour=10), end=MONDAY.replace(hour=10, minute=30))]

    async def book(self, start, attendee, *, notes=""):
        return Booking(provider=self.name, start=start, end=start + timedelta(minutes=30), reference=None)


def brief() -> CallBrief:
    return CallBrief(
        prospect=ProspectBrief(prospect_id=7, first_name="Sara", last_name="Ali", company="Ravi", phone="+923001234567", email="sara@example.com"),
        campaign=CampaignBrief(agent_name="Alex", company_name="Meridian", meeting_ask="a short call"),
        campaign_id=3,
        call_attempt_id=11,
        source="campaign",
    )


def service(*, store: Any = "default", carrier: FakeCarrier | None = None, calendar: CalendarProvider | None = None, call: bool = True, action_url: str | None = URL, timeout: int = 20) -> ActionService:
    return ActionService(
        brief=brief(),
        tz=KARACHI,
        timezone_name="Asia/Karachi",
        store=FakeActionStore() if store == "default" else store,  # type: ignore[arg-type]
        calendar=calendar,
        telephony=carrier,
        call=CallSession(provider="twilio", call_id="CA123", direction=DIRECTION_OUTBOUND) if call else None,
        transfer_number="+923009999999",
        caller_id="+15550001111",
        transfer_action_url=action_url,
        transfer_timeout_secs=timeout,
        now=lambda: datetime(2026, 9, 6, 9, 0, tzinfo=KARACHI),
    )


async def check_action_service() -> None:
    """The backend: what it asks the carrier and the calendar, and what it records."""
    print("\n=== the transfer action ===")
    carrier = FakeCarrier()
    svc = service(carrier=carrier)
    outcome = await svc.transfer_to_human("wants a person")
    check("the transfer succeeds", outcome.ok and outcome.data["destination"] == "+92…999")
    check("the carrier is given the receiver and the ring time", carrier.transfers == [{"call_id": "CA123", "to": "+923009999999", "caller_id": "+15550001111", "action_url": URL, "timeout": 20}], str(carrier.transfers))
    check("and the result says the outcome will be tracked", outcome.data["outcome_tracked"] is True and outcome.data["ring_secs"] == 20)
    recorded = svc._store.transfers  # type: ignore[union-attr]
    check("the REQUESTED row is written with the call, attempt, prospect and reason", recorded == [{"telephony_call_id": "CA123", "provider": "fake", "to_number": "+923009999999", "call_attempt_id": 11, "prospect_id": 7, "reason": "wants a person"}], str(recorded))

    svc = service(carrier=FakeCarrier(), action_url=None)
    outcome = await svc.transfer_to_human("")
    check("without a receiver, no action URL is sent and the result says so", svc._telephony.transfers[0]["action_url"] is None and outcome.data["outcome_tracked"] is False)  # type: ignore[union-attr]

    mark = _mark()
    svc = service(carrier=FakeCarrier(), store=FakeActionStore(failing=True))
    outcome = await svc.transfer_to_human("x")
    check("a database that cannot take the row does not fail the transfer", outcome.ok and _logged("record the transfer", mark) == 1)
    svc = service(carrier=FakeCarrier(), store=FakeActionStore(slow=True))
    started = asyncio.get_running_loop().time()
    outcome = await svc.transfer_to_human("x")
    check("nor hold the call: the write is bounded", outcome.ok and asyncio.get_running_loop().time() - started < 8)
    svc = service(carrier=FakeCarrier(), store=None)
    check("with no database, the transfer still goes ahead", (await svc.transfer_to_human("x")).ok)

    refused = service(carrier=FakeCarrier(mode="refuse"))
    outcome = await refused.transfer_to_human("x")
    check("a refusal is transfer_failed and records nothing", not outcome.ok and outcome.error_code == TRANSFER_FAILED and not refused._store.transfers)  # type: ignore[union-attr]
    down = service(carrier=FakeCarrier(mode="unreachable"))
    outcome = await down.transfer_to_human("x")
    check("an unreachable carrier is an external error, nothing recorded", not outcome.ok and outcome.error_code == EXTERNAL_ERROR and not down._store.transfers)  # type: ignore[union-attr]
    twice = service(carrier=FakeCarrier())
    first_try = await twice.transfer_to_human("a")
    second_try = await twice.transfer_to_human("b")
    check("the second transfer on one call is refused", first_try.ok and not second_try.ok and second_try.error_code == TRANSFER_FAILED)

    print("\n=== booking against the diary's own constraint ===")
    svc = service(calendar=LocalLikeCalendar(), store=FakeActionStore())
    outcome = await svc.book_meeting(MONDAY.replace(hour=10), AttendeeDetails(name="Sara Ali", email="sara@example.com", phone="+923001234567"))
    check("a local booking is the row, and succeeds", outcome.ok and outcome.data["reference"] == "meeting-1" and outcome.data["provider"] == "local")
    svc = service(calendar=LocalLikeCalendar(), store=FakeActionStore(conflict=True))
    outcome = await svc.book_meeting(MONDAY.replace(hour=10), AttendeeDetails(name="Sara Ali"))
    check("a write the constraint refuses is slot_taken, not an error", not outcome.ok and outcome.error_code == SLOT_TAKEN and "just been taken" in (outcome.message or ""))


# --- Cal.com ---------------------------------------------------------------------


class CalStubResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class CalStubSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "params": dict(params or {}), "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError(f"stub had no response left for {method} {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


def calcom(responses: list[Any], **kwargs: Any) -> tuple[CalComProvider, CalStubSession]:
    session = CalStubSession(responses)
    return CalComProvider("cal_live_secret", 4242, timezone="Asia/Karachi", slot_minutes=30, session=session, **kwargs), session


BOOKED = {"uid": "bk_lost", "start": "2026-09-07T05:00:00.000Z", "end": "2026-09-07T05:30:00.000Z", "status": "accepted", "attendees": [{"email": "sara@example.com"}]}


async def check_calcom() -> None:
    """Booking made once, a lost answer looked up, the event type read back."""
    print("\n=== Cal.com: a booking whose answer was lost ===")
    attendee = Attendee(name="Sara Ali", email="sara@example.com", phone="+923001234567", timezone="Asia/Karachi")
    start = datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI)

    provider, session = calcom([TimeoutError(), CalStubResponse(200, {"status": "success", "data": [BOOKED]})], timeout_secs=7.5)
    mark = _mark()
    booking = await provider.book(start, attendee, notes="fuel numbers")
    check("every request carries the timeout", all(r["timeout"] is not None and r["timeout"].total == 7.5 for r in session.requests))
    check("a booking that times out is looked up, not repeated", [r["method"] for r in session.requests] == ["POST", "GET"] and session.requests[1]["url"].endswith("/bookings"))
    lookup = session.requests[1]["params"]
    check("by attendee email, event type and a window around the start", lookup["attendeeEmail"] == "sara@example.com" and lookup["eventTypeId"] == "4242" and lookup["afterStart"] == "2026-09-07T04:59:00Z" and lookup["beforeEnd"] == "2026-09-07T05:31:00Z", str(lookup))
    check("with the bookings API version", session.requests[1]["headers"]["cal-api-version"] == "2024-08-13")
    check("and the booking Cal.com made is adopted, with its uid", booking.reference == "bk_lost" and booking.start == datetime(2026, 9, 7, 5, 0, tzinfo=UTC))
    check("saying so in the log", _logged("had booked it after all", mark) == 1)

    provider, session = calcom([TimeoutError(), CalStubResponse(200, {"status": "success", "data": []})])
    lost = await _araises(provider.book(start, attendee), CalendarUnavailableError, "nothing was booked")
    check("a timeout with no booking found is unavailable, and says nothing was booked", lost is not None and "did not answer within" in str(lost))
    check("and never POSTs twice", [r["method"] for r in session.requests] == ["POST", "GET"])

    provider, session = calcom([CalStubResponse(502, {"message": "bad gateway"}), CalStubResponse(200, {"status": "success", "data": [BOOKED]})])
    booking = await provider.book(start, attendee)
    check("a 5xx after the POST is treated the same way", booking.reference == "bk_lost" and len(session.requests) == 2)

    provider, session = calcom([TimeoutError(), CalStubResponse(200, {"status": "success", "data": [dict(BOOKED, status="cancelled"), dict(BOOKED, uid="bk_other", start="2026-09-07T06:00:00.000Z")]})])
    check("a cancelled booking, or one at another time, is not the one asked for", await _araises(provider.book(start, attendee), CalendarUnavailableError) is not None)

    provider, session = calcom([TimeoutError(), CalStubResponse(503, {"message": "down"})])
    mark = _mark()
    check("a lookup that fails too is unavailable, with both failures logged", await _araises(provider.book(start, attendee), CalendarUnavailableError) is not None and _logged("could not be asked either", mark) == 1)

    provider, session = calcom([])
    check("with no email there is nothing to look up by", await provider.find_booking(start, Attendee(name="X")) is None and not session.requests)

    print("\n=== Cal.com: the slot is gone ===")
    for message in ("no_available_users_found_error", "booking_time_out_of_bounds_error", "User either already has booking at this time or is not available", "The requested slot is unavailable"):
        provider, _ = calcom([CalStubResponse(400, {"status": "error", "error": {"message": message}})])
        check(f"{message[:44]:<44} is SlotUnavailableError", await _araises(provider.book(start, attendee), SlotUnavailableError) is not None)
    provider, _ = calcom([CalStubResponse(400, {"status": "error", "error": {"message": "eventTypeId must be a number"}})])
    check("a real refusal is still a CalendarError, not a taken slot", await _araises(provider.book(start, attendee), SlotUnavailableError) is None and await _araises(calcom([CalStubResponse(400, {"message": "eventTypeId must be a number"})])[0].book(start, attendee), CalendarError) is not None)

    print("\n=== Cal.com: the event type read back ===")
    provider, session = calcom([CalStubResponse(200, {"status": "success", "data": {"id": 4242, "title": "Discovery call", "slug": "discovery", "lengthInMinutes": 30}})])
    described = await provider.check_credentials()
    check("reads the event type", session.requests[0]["method"] == "GET" and session.requests[0]["url"].endswith("/event-types/4242") and session.requests[0]["headers"]["cal-api-version"] == "2024-06-14")
    check("and describes it", described == "event type 4242 'Discovery call', 30 min", described)
    provider, _ = calcom([CalStubResponse(200, {"status": "success", "data": {"id": 4242, "title": "Demo", "lengthInMinutes": 60}})])
    check("a length that differs from the slot setting is said", "differs from CALENDAR_SLOT_MINUTES=30" in await provider.check_credentials())
    provider, _ = calcom([CalStubResponse(401, {"message": "Unauthorized"})])
    check("a rejected key is a CalendarError naming the setting", await _araises(provider.check_credentials(), CalendarError, "CALCOM_API_KEY") is not None)
    provider, _ = calcom([CalStubResponse(404, {"message": "Event type not found"})])
    check("a missing event type names its setting", await _araises(provider.check_credentials(), CalendarError, "CALCOM_EVENT_TYPE_ID") is not None)


async def check_health_and_config() -> None:
    """The settings, and the calendar component of the health check."""
    print("\n=== configuration ===")
    names = ("CALENDAR_PROVIDER", "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEOUT_SECS", "TELEPHONY_TRANSFER_NUMBER", "TELEPHONY_TRANSFER_TIMEOUT_SECS", "TELEPHONY_PROVIDER", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TELEPHONY_FROM_NUMBER", "TELEPHONY_PUBLIC_URL", "DATABASE_URL", "KB_DATABASE_URL")
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        config = Config.from_env()
        check("the defaults: 15 s Cal.com timeout, 30 s transfer ring", config.calendar.calcom_timeout_secs == 15.0 and config.telephony.transfer_timeout_secs == 30)
        os.environ.update({"CALCOM_TIMEOUT_SECS": "8", "TELEPHONY_TRANSFER_TIMEOUT_SECS": "45", "TELEPHONY_TRANSFER_NUMBER": "+923009999999", "TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_AUTH_TOKEN": TOKEN, "TELEPHONY_FROM_NUMBER": "+15550001111", "TELEPHONY_PUBLIC_URL": "https://abc123.ngrok.app"})
        config = Config.from_env()
        check("the settings are read", config.calendar.calcom_timeout_secs == 8.0 and config.telephony.transfer_timeout_secs == 45)
        check("and the ring time is on the startup line", "(45s ring)" in config.telephony.describe())
        os.environ["TELEPHONY_TRANSFER_TIMEOUT_SECS"] = "2"
        try:
            Config.from_env()
            check("a ring time under five seconds is a config problem", False, "accepted")
        except Exception as exc:  # noqa: BLE001
            check("a ring time under five seconds is a config problem", "TELEPHONY_TRANSFER_TIMEOUT_SECS" in str(exc))
        del os.environ["TELEPHONY_TRANSFER_TIMEOUT_SECS"]

        print("\n=== the calendar health component ===")
        os.environ["CALENDAR_PROVIDER"] = "none"
        report = await HealthChecker(Config.from_env(), timeout_secs=2.0).run(only=("calendar",))
        check("no calendar is skipped", report.get("calendar").status is Status.SKIPPED)
        os.environ["CALENDAR_PROVIDER"] = "local"
        report = await HealthChecker(Config.from_env(), timeout_secs=2.0).run(only=("calendar",))
        check("the local calendar without a database is degraded, saying why", report.get("calendar").status is Status.DEGRADED and "DATABASE_URL" in report.get("calendar").detail)
        os.environ["DATABASE_URL"] = "postgresql://nobody@localhost/never-opened"
        report = await HealthChecker(Config.from_env(), timeout_secs=2.0).run(only=("calendar",))
        check("and with one it is fine, needing no account", report.get("calendar").status is Status.OK and "no account" in report.get("calendar").detail)
        os.environ["CALENDAR_PROVIDER"] = "calcom"
        os.environ["CALCOM_API_KEY"] = "cal_live_x"
        os.environ["CALCOM_EVENT_TYPE_ID"] = "4242"
        os.environ["CALCOM_API_BASE"] = "http://127.0.0.1:9/v2"
        report = await HealthChecker(Config.from_env(), timeout_secs=2.0).run(only=("calendar",))
        component = report.get("calendar")
        check("Cal.com that cannot be reached is failed, naming Cal.com", component.status is Status.FAILED and "Cal.com" in component.detail, component.detail)
        del os.environ["CALCOM_API_BASE"]
        source = (SERVER / "health.py").read_text(encoding="utf-8")
        check("health.py offers the component", '"calendar"' in source)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        os.environ.pop("CALCOM_API_BASE", None)


async def run_database_checks(dsn: str) -> None:
    """The no-double-booking constraint and the transfers table, against real rows."""
    from test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== the diary refuses a double booking, in SQL ===")
        ten = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
        first = await store.add_meeting(start_at=ten, end_at=ten + timedelta(minutes=30), provider="local", attendee_name="A")
        check("a local booking is written", first.id > 0)
        clash = await _araises(store.add_meeting(start_at=ten + timedelta(minutes=15), end_at=ten + timedelta(minutes=45), provider="local", attendee_name="B"), MeetingConflictError, "just been taken")
        check("an overlapping local booking is refused by the write itself", clash is not None)
        check("as a CampaignStoreError the service already handles", isinstance(clash, CampaignStoreError))
        adjacent = await store.add_meeting(start_at=ten + timedelta(minutes=30), end_at=ten + timedelta(minutes=60), provider="local", attendee_name="C")
        check("a booking that starts when the other ends is allowed", adjacent.id > 0)
        mirror = await store.add_meeting(start_at=ten, end_at=ten + timedelta(minutes=30), provider="calcom", reference="bk_x", attendee_name="D")
        check("a Cal.com mirror is outside the rule — Cal.com's diary is Cal.com's", mirror.id > 0)
        await admin.execute(f'UPDATE "{schema}".meetings SET status = \'CANCELLED\' WHERE id = $1', first.id)
        freed = await store.add_meeting(start_at=ten, end_at=ten + timedelta(minutes=30), provider="local", attendee_name="E")
        check("a cancelled booking frees its slot", freed.id > 0)
        check("busy_between sees the live ones", len(await store.busy_between(ten, ten + timedelta(hours=1))) == 3)

        results = await asyncio.gather(*(store.add_meeting(start_at=ten + timedelta(hours=2), end_at=ten + timedelta(hours=2, minutes=30), provider="local", attendee_name=f"R{n}") for n in range(5)), return_exceptions=True)
        wins = [r for r in results if not isinstance(r, Exception)]
        check("five simultaneous bookings of one slot: exactly one row, four refusals", len(wins) == 1 and all(isinstance(r, MeetingConflictError) for r in results if isinstance(r, Exception)), str([type(r).__name__ for r in results]))
        check("create_schema is idempotent with the constraint in place", await store.create_schema() is None)

        print("\n=== the transfers table ===")
        service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60)
        campaign = await service.create_campaign("Transfers")
        prospect = await service.create_prospect(first_name="Sara", last_name="Ali", phone="0300 1234567")
        attempt = await store.create_attempt(prospect_id=prospect.id, campaign_id=campaign.id)
        await store.mark_attempt_placed(attempt.id, telephony_call_id="CAtransfer1", provider="twilio")
        requested = await store.add_transfer(telephony_call_id="CAtransfer1", provider="twilio", to_number="+923009999999", call_attempt_id=attempt.id, prospect_id=prospect.id, reason="wants a person")
        check("the request is recorded", requested.status is TransferStatus.REQUESTED and requested.call_attempt_id == attempt.id)
        done = await store.complete_transfer("CAtransfer1", status=TransferStatus.ANSWERED, provider="twilio", dial_call_id="CAdial1", duration_seconds=95)
        check("the report completes it", done.id == requested.id and done.status is TransferStatus.ANSWERED and done.duration_seconds == 95 and done.completed_at is not None)
        again = await store.complete_transfer("CAtransfer1", status=TransferStatus.NO_ANSWER, provider="twilio", error="late")
        check("a second report changes nothing", again.status is TransferStatus.ANSWERED and again.error is None)
        orphan = await store.complete_transfer("CAnobody", status=TransferStatus.BUSY, provider="twilio", error="busy", to_number="+923009999999")
        check("a report with no request row is kept as its own row", orphan.status is TransferStatus.BUSY and orphan.call_attempt_id is None)
        rows = await store.list_transfers(call_attempt_id=attempt.id)
        check("listed by attempt", [r.id for r in rows] == [requested.id])
        check("and counted", (await store.transfer_counts()) == {"ANSWERED": 1, "BUSY": 1})

        print("\n=== the receiver over the real store ===")
        await store.add_transfer(telephony_call_id="CAtransfer1", provider="twilio", to_number="+923009999999", call_attempt_id=attempt.id, prospect_id=prospect.id)
        processor = WebhookProcessor(service, TwilioProvider(ACCOUNT, TOKEN), expected_url=URL)
        receipt = await processor.receive(signed(twilio_fields("CAtransfer1", "in-progress", DialCallStatus="no-answer", DialCallSid="CAdial2")))
        latest = (await store.list_transfers(call_attempt_id=attempt.id))[0]
        check("a Dial report lands on the open transfer", receipt.outcome == WebhookOutcome.TRANSFER and latest.status is TransferStatus.NO_ANSWER and "no-answer" in (latest.error or ""))
        check("with the fallback TwiML for the caller", receipt.body is not None and "<Say>" in receipt.body)
        check("and the attempt untouched", (await store.get_attempt(attempt.id)).status is CallAttemptStatus.QUEUED)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("Booking and transfer checks — Cal.com over a stub, the carrier's Dial report, the diary's constraint, and the rows.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_transfer_twiml()
        await check_transfer_provider()
        await check_transfer_receiver()
        await check_action_service()
        await check_calcom()
        await check_health_and_config()

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
