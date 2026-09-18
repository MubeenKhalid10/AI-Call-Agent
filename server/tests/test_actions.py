#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 7: the tools that act, and the boundary they cross.

Run it from the `server/` directory::

    uv run python tests/test_actions.py

**What is real here and what is a stub.** The tools, the argument validation
and guard in `toolkit.strict_tool`, the conversation's rules, and the
`ActionService` that validates and authorises every action are all the real
code, driven exactly as Pipecat drives them — a `FunctionSchema` handler called
with a `FunctionCallParams`. What is stubbed is the world behind the service:
the calendar provider, the campaign store, the knowledge retriever and the
carrier. Each stub can be told to succeed, refuse, or blow up, which is how the
failure paths the phase requires are exercised without a Cal.com account, a
PostgreSQL server, or a phone.

No real calendar booking and no real transfer happens in this file. That is a
requirement, not a convenience, and it is why the stubs exist.

The cases the phase asks for are each a `=== ... ===` section below:
successful calendar lookup, unavailable calendar, successful booking, booking
failure, callback scheduling, DNC, duplicate DNC, end call, transfer success
and failure, RAG search, malformed arguments, unauthorized actions, external
API failure — plus the logging and the result shape that every one of them
depends on.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper  # noqa: E402
from pipecat.frames.frames import EndWorkerFrame  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.services.llm_service import FunctionCallParams  # noqa: E402

from src.actions.service import ActionService  # noqa: E402
from src.campaigns.models import CallbackStatus, Meeting, ScheduledCallback  # noqa: E402
from src.campaigns.store import CampaignStoreError  # noqa: E402
from src.conversation import (  # noqa: E402
    AuditContext,
    CallBrief,
    CampaignBrief,
    Capabilities,
    ConversationState,
    Intent,
    NextAction,
    NullActionBackend,
    ProspectBrief,
    SalesConversation,
    Signal,
    ToolResult,
    strict_tool,
    validate_arguments,
)
from src.conversation.results import (  # noqa: E402
    EMAIL_REQUIRED,
    EXTERNAL_ERROR,
    INTERNAL_ERROR,
    INVALID_ARGUMENTS,
    INVALID_TIME,
    NOT_AUTHORIZED,
    NOT_STORED,
    PAST_TIME,
    SLOT_NOT_OFFERED,
    SLOT_TAKEN,
    TOO_FAR_AHEAD,
    TRANSFER_FAILED,
    TRANSFER_UNAVAILABLE,
    UNAVAILABLE,
)
from src.conversation.timeparse import (  # noqa: E402
    parse_clock,
    parse_day,
    parse_when,
    resolve_timezone,
)
from src.conversation.toolkit import _render_arguments  # noqa: E402
from src.conversation.tools import build_tools  # noqa: E402
from src.knowledge_store import Match  # noqa: E402
from src.scheduling import (  # noqa: E402
    Attendee,
    Booking,
    CalendarError,
    CalendarProvider,
    CalendarUnavailableError,
    Slot,
    SlotUnavailableError,
)
from src.telephony import (  # noqa: E402
    CallRequest,
    CallSnapshot,
    CallStatus,
    ProviderUnavailableError,
    TelephonyProvider,
    TransferError,
)
from src.telephony.session import DIRECTION_OUTBOUND, CallSession  # noqa: E402

_failures: list[str] = []

KARACHI = ZoneInfo("Asia/Karachi")
# Friday 4 September 2026, ten in the morning in Karachi. Fixed, so every date
# below is a fact and not a function of when the checks are run.
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=KARACHI)
MONDAY = date(2026, 9, 7)


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def now() -> datetime:
    return NOW


# --- The world, stubbed ---------------------------------------------------------


class FakeCalendar(CalendarProvider):
    """A `CalendarProvider` with dials for every way a calendar can behave."""

    name = "fake"

    def __init__(self, *, slots: list[Slot] | None = None, requires_email: bool = False) -> None:
        self.slots = slots if slots is not None else default_slots()
        self.requires_email = requires_email
        self.mode = "ok"  # ok | taken | refuse | unreachable | crash
        self.booked: list[tuple[datetime, Attendee, str]] = []
        self.availability_calls: list[tuple[datetime, datetime]] = []
        self.closed = False

    async def available_slots(self, start: datetime, end: datetime) -> list[Slot]:
        self.availability_calls.append((start, end))
        if self.mode == "unreachable":
            raise CalendarUnavailableError("calendar API timed out")
        if self.mode == "refuse":
            raise CalendarError("event type not found")
        if self.mode == "crash":
            raise RuntimeError("bug in the provider")
        return [slot for slot in self.slots if start <= slot.start < end]

    async def book(self, start: datetime, attendee: Attendee, *, notes: str = "") -> Booking:
        if self.mode == "taken":
            raise SlotUnavailableError("that slot has just been taken")
        if self.mode == "refuse":
            raise CalendarError("the calendar refused")
        if self.mode == "unreachable":
            raise CalendarUnavailableError("calendar API timed out")
        if self.mode == "crash":
            raise RuntimeError("bug in the provider")
        self.booked.append((start, attendee, notes))
        return Booking(
            provider=self.name,
            start=start,
            end=start + timedelta(minutes=30),
            reference=f"ext-{len(self.booked)}",
        )

    async def close(self) -> None:
        self.closed = True


def default_slots() -> list[Slot]:
    """Three slots on Monday morning and afternoon, Karachi time."""
    return [
        Slot(
            start=datetime.combine(MONDAY, time(hour, minute), tzinfo=KARACHI),
            end=datetime.combine(MONDAY, time(hour, minute), tzinfo=KARACHI) + timedelta(minutes=30),
        )
        for hour, minute in ((10, 0), (10, 30), (14, 0))
    ]


class FakeStore:
    """The slice of `CampaignStore` the action service uses, in memory."""

    def __init__(self, *, failing: bool = False, membership_id: int | None = 5) -> None:
        self.callbacks: list[ScheduledCallback] = []
        self.meetings: list[Meeting] = []
        self.failing = failing
        self.membership_id = membership_id
        self.placed: list[int] = []
        self.transfers: list[dict[str, Any]] = []

    async def add_transfer(self, **fields: Any) -> Any:
        """Phase 16: the `REQUESTED` transfer row the service writes."""
        if self.failing:
            raise CampaignStoreError("the database went away")
        self.transfers.append(dict(fields))
        return type("Transfer", (), {"id": len(self.transfers)})()

    async def find_membership(self, campaign_id: int, prospect_id: int):
        if self.membership_id is None:
            return None
        return type("Membership", (), {"id": self.membership_id})()

    async def schedule_callback(self, **fields: Any) -> ScheduledCallback:
        if self.failing:
            raise CampaignStoreError("the database went away")
        for index, existing in enumerate(self.callbacks):
            if existing.prospect_id == fields["prospect_id"] and existing.status is CallbackStatus.PENDING:
                moved = ScheduledCallback(
                    id=existing.id,
                    prospect_id=existing.prospect_id,
                    scheduled_for=fields["scheduled_for"],
                    campaign_id=fields.get("campaign_id") or existing.campaign_id,
                    call_attempt_id=fields.get("call_attempt_id") or existing.call_attempt_id,
                    campaign_prospect_id=fields.get("campaign_prospect_id") or existing.campaign_prospect_id,
                    note=fields.get("note") or existing.note,
                )
                self.callbacks[index] = moved
                return moved
        created = ScheduledCallback(
            id=len(self.callbacks) + 1,
            prospect_id=fields["prospect_id"],
            scheduled_for=fields["scheduled_for"],
            campaign_id=fields.get("campaign_id"),
            call_attempt_id=fields.get("call_attempt_id"),
            campaign_prospect_id=fields.get("campaign_prospect_id"),
            note=fields.get("note"),
        )
        self.callbacks.append(created)
        return created

    async def add_meeting(self, **fields: Any) -> Meeting:
        if self.failing:
            raise CampaignStoreError("the database went away")
        meeting = Meeting(id=len(self.meetings) + 1, **fields)
        self.meetings.append(meeting)
        return meeting

    async def busy_between(self, start: datetime, end: datetime):
        return [(m.start_at, m.end_at) for m in self.meetings if m.start_at < end and m.end_at > start]

    async def set_callbacks_status(self, prospect_id: int, status: CallbackStatus, *, only=None) -> int:
        self.placed.append(prospect_id)
        return 1


class FakeKnowledge:
    """Stands in for `KnowledgeRetriever.search`."""

    def __init__(self, matches: list[Match] | None | Exception) -> None:
        self.matches = matches
        self.queries: list[str] = []

    async def search(self, query: str):
        self.queries.append(query)
        if isinstance(self.matches, Exception):
            raise self.matches
        return self.matches


class FakeTelephony(TelephonyProvider):
    """A carrier that records transfer requests and can refuse them."""

    name = "fake"
    transports = ("twilio",)

    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode  # ok | refuse | unreachable
        self.transfers: list[tuple[str, str, str | None]] = []
        self.closed = False

    async def place_call(self, request: CallRequest) -> CallSnapshot:  # pragma: no cover
        raise NotImplementedError

    async def fetch_call(self, call_id: str) -> CallSnapshot:  # pragma: no cover
        raise NotImplementedError

    async def hang_up(self, call_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def make_serializer(self, call_data):  # pragma: no cover
        raise NotImplementedError

    async def transfer_call(
        self,
        call_id: str,
        to_number: str,
        *,
        caller_id: str | None = None,
        action_url: str | None = None,
        timeout_secs: int = 30,
    ) -> None:
        if self.mode == "refuse":
            raise TransferError("the call has already ended, so it cannot be transferred")
        if self.mode == "unreachable":
            raise ProviderUnavailableError("could not reach the carrier")
        self.transfers.append((call_id, to_number, caller_id))
        self.last_action_url = action_url
        self.last_timeout = timeout_secs

    async def close(self) -> None:
        self.closed = True


class RecordingSink:
    def __init__(self, *, stored: bool = True) -> None:
        self.dnc: list[tuple[int | None, str]] = []
        self.outcomes: list[dict[str, Any]] = []
        self._stored = stored

    async def on_do_not_call(self, brief: CallBrief, reason: str) -> bool:
        self.dnc.append((brief.prospect_id, reason))
        return self._stored

    async def on_call_finished(self, brief: CallBrief, outcome: dict[str, Any]) -> bool:
        self.outcomes.append(outcome)
        return self._stored

    async def close(self) -> None:
        return None


class FakeLLM:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def push_frame(self, frame: Any) -> None:
        self.frames.append(frame)


def brief_for(*, prospect_id: int | None = 7, email: str | None = None) -> CallBrief:
    return CallBrief(
        prospect=ProspectBrief(
            prospect_id=prospect_id,
            first_name="Sarah",
            last_name="Khan",
            company="Meridian",
            phone="+923001234567",
            email=email,
        ),
        campaign=CampaignBrief(agent_name="Alex", company_name="Northwind", meeting_ask="a short call"),
        campaign_id=3,
        call_attempt_id=11,
        source="campaign",
    )


def phone_call() -> CallSession:
    return CallSession(provider="twilio", call_id="CA123", direction=DIRECTION_OUTBOUND)


class World:
    """One session: stubs, the real service, the real conversation, the real tools."""

    def __init__(
        self,
        *,
        brief: CallBrief | None = None,
        calendar: FakeCalendar | None | str = "default",
        store: FakeStore | None | str = "default",
        knowledge: FakeKnowledge | None = None,
        telephony: FakeTelephony | None = None,
        call: CallSession | None = None,
        transfer_number: str | None = None,
        sink: RecordingSink | None = None,
    ) -> None:
        self.brief = brief or brief_for()
        self.calendar = FakeCalendar() if calendar == "default" else calendar
        self.store = FakeStore() if store == "default" else store
        self.knowledge = knowledge
        self.telephony = telephony
        self.sink = sink or RecordingSink()
        self.service = ActionService(
            brief=self.brief,
            tz=KARACHI,
            timezone_name="Asia/Karachi",
            store=self.store,  # type: ignore[arg-type]
            calendar=self.calendar,
            knowledge=self.knowledge,  # type: ignore[arg-type]
            telephony=self.telephony,
            call=call,
            transfer_number=transfer_number,
            caller_id="+15550001111",
            calendar_max_days_ahead=30,
            callback_max_days_ahead=60,
            now=now,
        )
        self.audit = AuditContext(session_id="sess-1", call_id=call.call_id if call else None,
                                  prospect_id=self.brief.prospect_id, call_attempt_id=self.brief.call_attempt_id)
        self.conversation = SalesConversation(
            self.brief,
            sink=self.sink,
            actions=self.service,
            timezone=KARACHI,
            now=now,
            audit=self.audit,
        )
        self.llm = FakeLLM()
        self.tools = {tool.name: tool for tool in build_tools(self.conversation)}
        self.results: list[dict[str, Any]] = []

    async def says(self, text: str):
        return await self.conversation.note_user_turn(text)

    async def calls(self, name: str, arguments: Any = None, **kwargs: Any) -> dict[str, Any]:
        """One tool call, through the real handler, as Pipecat makes it."""
        captured: dict[str, Any] = {}

        async def result_callback(result: Any, *args: Any, **kw: Any) -> None:
            captured.update(result if isinstance(result, dict) else {"result": result})

        params = FunctionCallParams(
            function_name=name,
            tool_call_id=f"call-{len(self.results)}",
            arguments=kwargs if arguments is None else arguments,
            llm=self.llm,
            pipeline_worker=None,
            context=LLMContext(),
            result_callback=result_callback,
        )
        await self.tools[name].handler(params)
        self.results.append(captured)
        return captured

    @property
    def record(self):
        return self.conversation.record

    @property
    def state(self):
        return self.conversation.state


async def _guidance_after(world: World, text: str) -> str:
    """The block the director would attach after the prospect says `text`."""
    await world.says(text)
    return world.conversation.guidance()


def shaped(result: dict[str, Any]) -> bool:
    """Every result has exactly the five keys, and the success/error invariant holds."""
    if set(result) != {"success", "data", "error_code", "message", "guidance"}:
        return False
    if result["success"]:
        return result["error_code"] is None and isinstance(result["data"], dict)
    return isinstance(result["error_code"], str) and isinstance(result["message"], str)


# --- The checks -------------------------------------------------------------------


async def check_capabilities() -> None:
    """The prompt promises only what the session can do."""
    print("\n=== capabilities drive the prompt ===")

    everything = World(
        knowledge=FakeKnowledge([]),
        telephony=FakeTelephony(),
        call=phone_call(),
        transfer_number="+923009999999",
    )
    caps = everything.conversation.capabilities
    check("a fully wired session can do everything", all((
        caps.can_search_knowledge, caps.can_check_calendar, caps.can_book_meeting,
        caps.can_schedule_callback, caps.can_transfer,
    )))
    check("and is in the configured timezone", caps.timezone == "Asia/Karachi")
    instruction = everything.conversation.system_instruction()
    check("it is told the time in that zone", "10:00 in Asia/Karachi" in instruction)
    check("it is told to check the calendar before offering times", "check_calendar_availability" in instruction)
    check("and to book only after a choice", "book_meeting" in instruction)
    check("and to schedule callbacks with an exact time", "schedule_callback with" in instruction and "YYYY-MM-DDTHH:MM" in instruction)
    check("and that it may connect them to a colleague", "connect them to a colleague right now" in instruction)
    check("and to search the knowledge base", "search_knowledge_base" in instruction)

    await everything.calls("move_to_stage", stage="meeting")
    hint = everything.conversation.guidance()
    check("the meeting stage names the calendar tool", "check_calendar_availability" in hint and "book_meeting" in hint)
    check("and forbids claiming a booking early", "Do not say it is booked until book_meeting answers success true" in hint)

    nothing = World(calendar=None, store=None)
    caps = nothing.conversation.capabilities
    check("a bare session can do none of it", not any((
        caps.can_search_knowledge, caps.can_check_calendar, caps.can_book_meeting,
        caps.can_schedule_callback, caps.can_transfer,
    )))
    instruction = nothing.conversation.system_instruction()
    check("it is told it cannot transfer", "You cannot transfer this call" in instruction)
    check("and never hears of the calendar tool", "check_calendar_availability" not in instruction)
    await nothing.calls("move_to_stage", stage="meeting")
    check("the meeting stage points at request_meeting instead", "request_meeting" in nothing.conversation.guidance())

    browser = World(call=None, transfer_number="+923009999999", telephony=FakeTelephony())
    check("a browser session cannot transfer, whatever is configured", not browser.conversation.capabilities.can_transfer)
    anonymous = World(brief=brief_for(prospect_id=None))
    check("a session with no prospect cannot schedule a callback", not anonymous.conversation.capabilities.can_schedule_callback)
    check("but can still book a meeting", anonymous.conversation.capabilities.can_book_meeting)

    # The request for a person is answered differently depending on the session.
    await everything.says("Can I speak to a real person please?")
    check("on a transferable call the override offers to connect them", "offer to connect them" in everything.conversation.guidance())
    await nothing.says("Can I speak to a real person please?")
    check("otherwise it offers a callback", "have a colleague call them back" in nothing.conversation.guidance())

    # A named day, with a calendar behind the session, is an order to check it.
    await everything.says("Would Monday morning work on your side?")
    block = everything.conversation.guidance()
    check("a named day tells the agent to check the calendar now", "call check_calendar_availability with that day as YYYY-MM-DD NOW" in block)
    check("and not to say it will", "do not say you will check" in block)
    check("the override is consumed by that turn", "NAMED A DAY" not in everything.conversation.guidance())
    await nothing.says("Would Monday morning work on your side?")
    check("with nothing to act on, a named day raises no override", "NAMED A DAY" not in nothing.conversation.guidance())
    callbacks_only = World(calendar=None)
    await callbacks_only.says("Thursday at ten would suit me")
    check("with only callbacks, it points at schedule_callback and request_meeting", "schedule_callback" in callbacks_only.conversation.guidance() and "request_meeting" in (await _guidance_after(callbacks_only, "Thursday at ten would suit me")))

    null = SalesConversation(CallBrief(), actions=NullActionBackend("UTC"))
    check("the null backend fails every action plainly", (await null.search_knowledge("pricing"))["success"] is False
          if isinstance(await null.search_knowledge("pricing"), dict) else (await null.search_knowledge("pricing")).success is False)
    check("the service describes itself for the log", "calendar" in everything.service.describe() and "transfer=+92" in everything.service.describe())


async def check_calendar_lookup() -> None:
    """Successful calendar lookup, and the ways it is unavailable."""
    print("\n=== calendar lookup ===")

    world = World()
    result = await world.calls("check_calendar_availability", day="2026-09-07", preferred_time="2pm")
    check("a free day returns its slots", result["success"] and len(result["data"]["slots"]) == 3)
    check("with ISO starts in the session's zone", result["data"]["slots"][0]["start"].startswith("2026-09-07T"))
    check("and spoken labels", "Monday 7 September at" in result["data"]["slots"][0]["label"])
    check("sorted towards the preferred time", result["data"]["slots"][0]["start"] == "2026-09-07T14:00")
    check("the window asked of the calendar is that day, in that zone",
          world.calendar.availability_calls[0][0] == datetime(2026, 9, 7, 0, 0, tzinfo=KARACHI))
    check("the slots are remembered as the only bookable ones", len(world.conversation.offered_slots) == 3)
    check("the call moves into asking for the meeting", world.state is ConversationState.MEETING_REQUEST)
    check("with the intent recorded as requested, not accepted", world.record.meeting_intent is Intent.REQUESTED)
    check("the guidance names the two times to offer and forbids saying booked",
          "Monday 7 September at 14:00 or Monday 7 September at 10:30" in result["guidance"]
          and "Nothing is booked yet" in result["guidance"])
    check("and the guidance leads the result, before the data", list(result)[0] == "guidance")
    check("a full timestamp is read as its day", (await world.calls("check_calendar_availability", day="2026-09-07T09:00"))["success"])

    empty = World(calendar=FakeCalendar(slots=[]))
    result = await empty.calls("check_calendar_availability", day="2026-09-07")
    check("a full day is a success with no slots", result["success"] and result["data"]["slots"] == [])
    check("and the guidance is to ask about another day", "another day" in result["guidance"])

    for label, mode, code in (
        ("an unreachable calendar", "unreachable", EXTERNAL_ERROR),
        ("a calendar that refuses", "refuse", EXTERNAL_ERROR),
        ("a calendar provider that crashes", "crash", EXTERNAL_ERROR),
    ):
        broken = World()
        broken.calendar.mode = mode
        result = await broken.calls("check_calendar_availability", day="2026-09-07")
        check(f"{label} is a failure, not a crash", result["success"] is False and result["error_code"] == code, str(result["message"]))
        check(f"{label}: nothing is offered", not broken.conversation.offered_slots)
        check(f"{label}: the guidance says not to offer a time", "do not offer or confirm any time" in result["guidance"])

    none = World(calendar=None)
    result = await none.calls("check_calendar_availability", day="2026-09-07")
    check("no calendar configured is `unavailable`", result["success"] is False and result["error_code"] == UNAVAILABLE)

    world = World()
    result = await world.calls("check_calendar_availability", day="next Tuesday")
    check("a vague day is refused with the format", result["error_code"] == INVALID_TIME and "YYYY-MM-DD" in result["message"])
    result = await world.calls("check_calendar_availability", day="2026-09-03")
    check("yesterday is refused as past", result["error_code"] == PAST_TIME)
    result = await world.calls("check_calendar_availability", day="2026-12-01")
    check("beyond the horizon is refused as too far ahead", result["error_code"] == TOO_FAR_AHEAD)
    check("and the guidance offers something sooner", "sooner" in result["guidance"])


async def check_booking() -> None:
    """A successful booking, and every way one fails."""
    print("\n=== booking ===")

    world = World()
    await world.calls("check_calendar_availability", day="2026-09-07")
    result = await world.calls("book_meeting", start="2026-09-07T10:30", notes="bring the fuel numbers")
    check("an offered slot books", result["success"] is True, str(result))
    check("the calendar was asked to book that slot", world.calendar.booked[0][0] == datetime(2026, 9, 7, 10, 30, tzinfo=KARACHI))
    check("with the prospect as attendee", world.calendar.booked[0][1].name == "Sarah Khan" and world.calendar.booked[0][1].phone == "+923001234567")
    check("and the notes", world.calendar.booked[0][2] == "bring the fuel numbers")
    check("the meeting is recorded in the store", len(world.store.meetings) == 1 and world.store.meetings[0].reference == "ext-1")
    check("keyed to the prospect and the attempt", world.store.meetings[0].prospect_id == 7 and world.store.meetings[0].call_attempt_id == 11)
    check("the record says booked, only now", world.record.meeting_booked and world.record.next_action is NextAction.MEETING_BOOKED)
    check("with the start and the reference", world.record.meeting_start == "2026-09-07T10:30" and world.record.meeting_reference == "ext-1")
    check("the result carries a spoken label", result["data"]["label"] == "Monday 7 September at 10:30")
    check("and the guidance confirms it", "The meeting is booked" in result["guidance"])
    check("the stage hint stops further booking", "Do not book another" in world.conversation.guidance())

    world = World()
    result = await world.calls("book_meeting", start="2026-09-07T10:00")
    check("booking before checking is refused", result["error_code"] == SLOT_NOT_OFFERED and "nothing has been offered" in result["message"])
    check("and nothing was booked", not world.calendar.booked and not world.record.meeting_booked)
    await world.calls("check_calendar_availability", day="2026-09-07")
    result = await world.calls("book_meeting", start="2026-09-07T11:00")
    check("a time that was not offered is refused", result["error_code"] == SLOT_NOT_OFFERED)
    check("naming what was offered", "10:00" in result["message"] and result["data"]["offered"])
    result = await world.calls("book_meeting", start="Monday at ten")
    check("a vague start is refused with the format", result["error_code"] == INVALID_TIME)
    result = await world.calls("book_meeting", start="2026-09-07T05:00Z")
    check("an offset is honoured — 05:00Z is 10:00 in Karachi", result["success"] is True, str(result["message"]))

    taken = World()
    await taken.calls("check_calendar_availability", day="2026-09-07")
    taken.calendar.mode = "taken"
    result = await taken.calls("book_meeting", start="2026-09-07T10:00")
    check("a slot taken meanwhile is `slot_taken`", result["error_code"] == SLOT_TAKEN)
    check("and is no longer offered", all(s["start"] != "2026-09-07T10:00" for s in taken.conversation.offered_slots))
    check("the guidance offers another time", "one of the other free times" in result["guidance"])
    check("nothing says booked", not taken.record.meeting_booked and taken.record.next_action is NextAction.MEETING_REQUESTED)
    check("but the agreement is on the record for a person to act on", taken.record.meeting_intent is Intent.ACCEPTED)

    for label, mode in (("a refusing calendar", "refuse"), ("an unreachable calendar", "unreachable"), ("a crashing provider", "crash")):
        broken = World()
        await broken.calls("check_calendar_availability", day="2026-09-07")
        broken.calendar.mode = mode
        result = await broken.calls("book_meeting", start="2026-09-07T10:00")
        check(f"{label} fails the booking explicitly", result["success"] is False and result["error_code"] == EXTERNAL_ERROR)
        check(f"{label}: the guidance says do NOT say it is booked", "did NOT go through" in result["guidance"])
        check(f"{label}: no meeting row", not broken.store.meetings)

    needs_email = World(calendar=FakeCalendar(requires_email=True))
    await needs_email.calls("check_calendar_availability", day="2026-09-07")
    result = await needs_email.calls("book_meeting", start="2026-09-07T10:00")
    check("a provider needing an email asks for it first", result["error_code"] == EMAIL_REQUIRED and "email address" in result["guidance"])
    check("and the prompt warned the agent", needs_email.conversation.capabilities.booking_requires_email)
    result = await needs_email.calls("book_meeting", start="2026-09-07T10:00", attendee_email="sarah@meridian.example")
    check("with an email it books", result["success"] and needs_email.calendar.booked[0][1].email == "sarah@meridian.example")

    known = World(brief=brief_for(email="sk@meridian.example"), calendar=FakeCalendar(requires_email=True))
    await known.calls("check_calendar_availability", day="2026-09-07")
    result = await known.calls("book_meeting", start="2026-09-07T10:00")
    check("a known email from the prospect record is used", result["success"] and known.calendar.booked[0][1].email == "sk@meridian.example")

    # Phase 40: the calendar gets an address, however the model wrote it down.
    spoken = World(calendar=FakeCalendar(requires_email=True))
    await spoken.calls("check_calendar_availability", day="2026-09-07")
    result = await spoken.calls("book_meeting", start="2026-09-07T10:00", attendee_email="John dot Smith two at Gmail dot com")
    check("an email passed as it was spoken is booked as an address", result["success"] and spoken.calendar.booked[0][1].email == "john.smith2@gmail.com", repr(spoken.calendar.booked))

    dictated = World(brief=brief_for(email="sk@meridian.example"), calendar=FakeCalendar(requires_email=True))
    await dictated.conversation.note_user_turn("Use my other email, it's sarah underscore k at outlook dot com.")
    await dictated.calls("check_calendar_availability", day="2026-09-07")
    result = await dictated.calls("book_meeting", start="2026-09-07T10:00")
    check("an address dictated on the call beats the one on file", result["success"] and dictated.calendar.booked[0][1].email == "sarah_k@outlook.com", repr(dictated.calendar.booked))

    # A local booking *is* the row: if it cannot be written, nothing was booked.
    local = World(store=FakeStore(failing=True))
    local.calendar.name = "local"

    async def local_book(start, attendee, *, notes=""):
        return Booking(provider="local", start=start, end=start + timedelta(minutes=30), reference=None)

    local.calendar.book = local_book  # type: ignore[method-assign]
    await local.calls("check_calendar_availability", day="2026-09-07")
    result = await local.calls("book_meeting", start="2026-09-07T10:00")
    check("a local booking that cannot be saved is reported as NOT booked", result["success"] is False and result["error_code"] == EXTERNAL_ERROR)
    check("and the record agrees", not local.record.meeting_booked)


async def check_callbacks() -> None:
    """Callback scheduling: valid, invalid, past, far, duplicate, no prospect."""
    print("\n=== callback scheduling ===")

    world = World()
    report = await world.says("Can you call me back on Wednesday morning?")
    check("the request is detected as advisory", Signal.CALLBACK in report and world.state is ConversationState.GREETING)
    result = await world.calls("schedule_callback", when="2026-09-09T10:30", note="prefers mornings")
    check("an exact future time schedules", result["success"] is True, str(result))
    check("a callback row was created", len(world.store.callbacks) == 1)
    row = world.store.callbacks[0]
    check("at that moment, timezone-aware", row.scheduled_for == datetime(2026, 9, 9, 10, 30, tzinfo=KARACHI) and row.scheduled_for.tzinfo is not None)
    check("keyed to the prospect, campaign, attempt and membership", (row.prospect_id, row.campaign_id, row.call_attempt_id, row.campaign_prospect_id) == (7, 3, 11, 5))
    check("with the note", row.note == "prefers mornings")
    check("the record says scheduled, only now", world.record.callback_scheduled_for == "2026-09-09T10:30+05:00")
    check("and the intent and next action", world.record.callback_intent is Intent.ACCEPTED and world.record.next_action is NextAction.CALLBACK_REQUESTED)
    check("the call moves to CALLBACK", world.state is ConversationState.CALLBACK)
    check("the result carries a spoken label", result["data"]["label"] == "Wednesday 9 September at 10:30")
    check("and the guidance confirms and closes", "The callback is scheduled" in result["guidance"] and "Do not sell" in result["guidance"])
    check("the stage hint then says to close", "The callback is scheduled" in world.conversation.guidance())

    result = await world.calls("schedule_callback", when="2026-09-10T15:00")
    check("asking again moves the time", result["success"] and len(world.store.callbacks) == 1 and world.store.callbacks[0].scheduled_for.hour == 15)

    world = World()
    for label, when, code in (
        ("a vague time", "next Wednesday", INVALID_TIME),
        ("a bare date", "2026-09-09", INVALID_TIME),
        ("a past time", "2026-09-01T10:00", PAST_TIME),
        ("right now", "2026-09-04T10:00", PAST_TIME),
        ("too far ahead", "2027-01-15T10:00", TOO_FAR_AHEAD),
    ):
        result = await world.calls("schedule_callback", when=when)
        check(f"{label} is refused as {code}", result["success"] is False and result["error_code"] == code, str(result["message"]))
    check("and none of those created a row", not world.store.callbacks)
    check("nor recorded a schedule", world.record.callback_scheduled_for is None)
    result = await world.calls("schedule_callback", when="next Wednesday")
    check("the vague-time message shows the format with a real example", "YYYY-MM-DDTHH:MM" in result["message"] and "2026-09-05T" in result["message"])
    check("the past-time guidance says to check the date", "Check the date against today" in (await world.calls("schedule_callback", when="2026-09-01T10:00"))["guidance"])

    failing = World(store=FakeStore(failing=True))
    result = await failing.calls("schedule_callback", when="2026-09-09T10:30")
    check("a database failure is an explicit failure", result["success"] is False and result["error_code"] == EXTERNAL_ERROR)
    check("the intent is still recorded", failing.record.callback_intent is Intent.ACCEPTED and failing.record.callback_when == "Wednesday 9 September at 10:30")
    check("but nothing says scheduled", failing.record.callback_scheduled_for is None)
    check("and the guidance says not to claim it", "could NOT be scheduled" in result["guidance"])

    anonymous = World(brief=brief_for(prospect_id=None))
    result = await anonymous.calls("schedule_callback", when="Wednesday morning")
    check("with no prospect the intent is recorded in their words", result["success"] is False and anonymous.record.callback_when == "Wednesday morning")
    check("as unavailable, with a colleague to arrange it", result["error_code"] == UNAVAILABLE and "colleague" in result["guidance"])

    # The backend validates on its own too, in case the conversation's check is
    # ever bypassed.
    outcome = await world.service.schedule_callback(datetime(2026, 9, 1, 10, 0, tzinfo=KARACHI))
    check("the service itself refuses the past", not outcome.ok and outcome.error_code == PAST_TIME)


async def check_dnc() -> None:
    """DNC, duplicate DNC, and DNC with nothing to write to."""
    print("\n=== do not call ===")

    world = World()
    result = await world.calls("mark_do_not_call", reason="asked to be removed")
    check("the tool marks them", result["success"] and result["data"]["stored"] is True)
    check("through the sink, once", len(world.sink.dnc) == 1 and world.sink.dnc[0][0] == 7)
    check("the state is forced", world.state is ConversationState.DO_NOT_CALL)
    check("the record says do not contact", world.record.next_action is NextAction.DO_NOT_CONTACT)

    result = await world.calls("mark_do_not_call", reason="said it again")
    check("a duplicate is idempotent", result["success"] and result["data"]["already_marked"] is True and len(world.sink.dnc) == 1)
    await world.says("Seriously, take me off your list.")
    check("and the detector firing afterwards adds nothing", len(world.sink.dnc) == 1)

    anonymous = World(brief=brief_for(prospect_id=None), sink=RecordingSink(stored=False))
    result = await anonymous.calls("mark_do_not_call", reason="please")
    check("with no row to mark the tool reports NOT_STORED", result["success"] is False and result["error_code"] == NOT_STORED)
    check("but the call still honours it", anonymous.state is ConversationState.DO_NOT_CALL)
    check("and the guidance still confirms it to them", "will not be called again" in result["guidance"])


async def check_end_call() -> None:
    """Ending the call: the result first, then the end frame."""
    print("\n=== end call ===")

    world = World()
    result = await world.calls("end_call", reason="all done")
    check("the result is reported", result["success"] and result["data"]["ending"] is True)
    check("the guidance is one goodbye", "goodbye" in result["guidance"])
    check("the state is ENDING", world.state is ConversationState.ENDING)
    check("an end frame is pushed downstream", len(world.llm.frames) == 1 and isinstance(world.llm.frames[0], EndWorkerFrame))
    check("and the agent is recorded as having ended it", world.conversation.end_requested)
    check("the audit log has it", world.audit.actions[-1].tool == "end_call" and world.audit.actions[-1].success)


async def check_transfer() -> None:
    """Transfer success and every failure."""
    print("\n=== transfer to a human ===")

    world = World(telephony=FakeTelephony(), call=phone_call(), transfer_number="+923009999999")
    await world.says("Can I talk to a real person?")
    result = await world.calls("transfer_to_human", reason="wants a person")
    check("a phone call with a destination transfers", result["success"] is True, str(result))
    check("through the carrier, with the caller id", world.telephony.transfers == [("CA123", "+923009999999", "+15550001111")])
    check("the destination is masked in the result", result["data"]["destination"] == "+92…999")
    check("the record says transferred", world.record.transferred and world.record.next_action is NextAction.TRANSFERRED)
    check("the call is ending, by the agent", world.state is ConversationState.ENDING and world.conversation.end_requested)
    check("but NO end frame is pushed — the carrier closes the stream", not world.llm.frames)
    check("the guidance is to say nothing more", "Do not say anything else" in result["guidance"])
    result = await world.calls("transfer_to_human")
    check("a second transfer fails", result["success"] is False and result["error_code"] == TRANSFER_FAILED)

    refused = World(telephony=FakeTelephony(mode="refuse"), call=phone_call(), transfer_number="+923009999999")
    result = await refused.calls("transfer_to_human")
    check("a carrier refusal is `transfer_failed`", result["success"] is False and result["error_code"] == TRANSFER_FAILED)
    check("saying the call has ended", "already ended" in result["message"])
    check("the agent is told it is still on the call", "you are still the one on the call" in result["guidance"])
    check("and the record routes a follow-up instead", not refused.record.transferred and refused.record.next_action is NextAction.HUMAN_FOLLOW_UP)
    check("the state has not moved to ENDING", refused.state is not ConversationState.ENDING)

    unreachable = World(telephony=FakeTelephony(mode="unreachable"), call=phone_call(), transfer_number="+923009999999")
    result = await unreachable.calls("transfer_to_human")
    check("an unreachable carrier is an external error", result["error_code"] == EXTERNAL_ERROR and result["success"] is False)

    browser = World(telephony=FakeTelephony(), call=None, transfer_number="+923009999999")
    result = await browser.calls("transfer_to_human")
    check("a browser session cannot transfer", result["error_code"] == TRANSFER_UNAVAILABLE and not browser.telephony.transfers)
    check("and offers a callback", "colleague will call them back" in result["guidance"])

    no_destination = World(telephony=FakeTelephony(), call=phone_call(), transfer_number=None)
    result = await no_destination.calls("transfer_to_human")
    check("a call with no destination cannot transfer", result["error_code"] == TRANSFER_UNAVAILABLE)
    no_credentials = World(telephony=None, call=phone_call(), transfer_number="+923009999999")
    result = await no_credentials.calls("transfer_to_human")
    check("nor one with no carrier credentials", result["error_code"] == TRANSFER_UNAVAILABLE)

    # The service level, for the message a person would read in the log.
    outcome = await no_credentials.service.transfer_to_human("x")
    check("the service names the missing piece", "credentials" in (outcome.message or ""))


async def check_knowledge() -> None:
    """RAG through a tool: found, nothing, empty, unavailable, broken."""
    print("\n=== knowledge base search ===")

    matches = [
        Match(content="The premium plan is 49 dollars a month.", source="pricing.pdf", title="Pricing", ordinal=1, score=0.81),
        Match(content="Annual billing saves two months.", source="pricing.pdf", title="Pricing", ordinal=2, score=0.66),
    ]
    world = World(knowledge=FakeKnowledge(matches))
    result = await world.calls("search_knowledge_base", query="premium plan price")
    check("a hit returns the passages", result["success"] and result["data"]["found"] and len(result["data"]["passages"]) == 2)
    check("through the retriever, with the query", world.knowledge.queries == ["premium plan price"])
    check("passages carry title and content", result["data"]["passages"][0]["title"] == "Pricing" and "49 dollars" in result["data"]["passages"][0]["content"])
    check("the guidance is to answer from them only", "from these passages only" in result["guidance"])
    check("the state is untouched by a lookup", world.state is ConversationState.GREETING)

    nothing = World(knowledge=FakeKnowledge([]))
    result = await nothing.calls("search_knowledge_base", query="offices in Brazil")
    check("no match is a success with found false", result["success"] and result["data"]["found"] is False)
    check("and the guidance is to say so, not guess", "Do not guess" in result["guidance"])

    empty = World(knowledge=FakeKnowledge(None))
    result = await empty.calls("search_knowledge_base", query="anything")
    check("an empty knowledge base reads as nothing found", result["success"] and result["data"]["found"] is False)

    broken = World(knowledge=FakeKnowledge(RuntimeError("pgvector is down")))
    result = await broken.calls("search_knowledge_base", query="pricing")
    check("a failing store is an external error", result["success"] is False and result["error_code"] == EXTERNAL_ERROR)
    check("with the guidance not to guess", "Do not guess" in result["guidance"])

    none = World(knowledge=None)
    result = await none.calls("search_knowledge_base", query="pricing")
    check("no knowledge base is `unavailable`", result["error_code"] == UNAVAILABLE)
    result = await world.calls("search_knowledge_base", query="x")
    check("a one-letter query is refused", result["error_code"] == INVALID_ARGUMENTS)


async def check_malformed_arguments() -> None:
    """The boundary refuses what does not fit the schema, in a structured way."""
    print("\n=== malformed tool arguments ===")

    world = World()
    result = await world.calls("book_meeting", arguments={})
    check("a missing required argument is refused", result["success"] is False and result["error_code"] == INVALID_ARGUMENTS)
    check("naming it", "start is required" in result["message"])
    check("and listing what is expected", result["data"]["expected"]["start"] == "string (required)")
    check("nothing reached the calendar", not world.calendar.booked)

    result = await world.calls("check_calendar_availability", arguments={"day": ["2026-09-07"]})
    check("a wrong type is refused", result["error_code"] == INVALID_ARGUMENTS and "must be text" in result["message"])
    result = await world.calls("check_calendar_availability", arguments={"day": None})
    check("null for a required argument is missing", result["error_code"] == INVALID_ARGUMENTS and "required" in result["message"])
    result = await world.calls("check_calendar_availability", arguments="2026-09-07")
    check("arguments that are not an object are refused", result["error_code"] == INVALID_ARGUMENTS and "must be an object" in result["message"])

    result = await world.calls("check_calendar_availability", arguments={"day": "2026-09-07", "date": "ignored", "verbose": True})
    check("unknown arguments are dropped and the call proceeds", result["success"] is True)

    result = await world.calls("set_interest", arguments={"level": "very keen"})
    check("an out-of-vocabulary enum is refused with the vocabulary", result["error_code"] == INVALID_ARGUMENTS and "INTERESTED" in result["data"]["allowed"])
    result = await world.calls("move_to_stage", arguments={"stage": "negotiation"})
    check("an unknown stage is refused", result["error_code"] == INVALID_ARGUMENTS and "unknown stage" in result["message"])
    check("every refusal says nothing happened", all("Nothing has happened" in r["guidance"] or "nothing was recorded" in r["message"].lower() or "refused" in r["guidance"].lower() for r in world.results if not r["success"]))

    # Coercions that are safe are made; the ones that are not are refused.
    cleaned, problems, ignored = validate_arguments(
        {"n": "3", "f": "1.5", "b": "true", "s": 7, "extra": 1},
        {"n": {"type": "integer"}, "f": {"type": "number"}, "b": {"type": "boolean"}, "s": {"type": "string"}},
        ["n"],
    )
    check("numeric strings become numbers", cleaned["n"] == 3 and cleaned["f"] == 1.5)
    check("'true' becomes a boolean, a number becomes text", cleaned["b"] is True and cleaned["s"] == "7")
    check("unknown keys are reported, not passed", ignored == ["extra"] and not problems)
    _, problems, _ = validate_arguments({"n": "three"}, {"n": {"type": "integer"}}, ["n"])
    check("an unparseable number is a problem", problems == ["n must be a whole number, not 'three'"])
    _, problems, _ = validate_arguments({"n": True}, {"n": {"type": "integer"}}, ["n"])
    check("a boolean is not a whole number", bool(problems))

    # A tool that raises is reported, not swallowed and not propagated.
    async def exploding(params: FunctionCallParams, thing: str) -> ToolResult:
        """Blow up.

        Args:
            thing: Anything.
        """
        raise RuntimeError("kaboom")

    schema = strict_tool(exploding, audit=AuditContext())
    captured: dict[str, Any] = {}

    async def cb(result: Any, *a: Any, **k: Any) -> None:
        captured.update(result)

    await schema.handler(FunctionCallParams(function_name="exploding", tool_call_id="t", arguments={"thing": "x"},
                                            llm=FakeLLM(), pipeline_worker=None, context=LLMContext(), result_callback=cb))
    check("a raising tool becomes an internal_error result", captured["success"] is False and captured["error_code"] == INTERNAL_ERROR)
    check("whose guidance says do not say it worked", "Do not tell them it worked" in captured["guidance"])

    async def silent(params: FunctionCallParams) -> None:
        """Return nothing and report nothing."""
        return None

    schema = strict_tool(silent, audit=AuditContext())
    captured.clear()
    await schema.handler(FunctionCallParams(function_name="silent", tool_call_id="t", arguments={},
                                            llm=FakeLLM(), pipeline_worker=None, context=LLMContext(), result_callback=cb))
    check("a tool that reports nothing is settled as a failure", captured["success"] is False and "without a result" in captured["message"])

    def with_kwargs():
        async def bad(params: FunctionCallParams, **anything: Any) -> None:
            """Take anything."""

        return strict_tool(bad, audit=AuditContext())

    try:
        with_kwargs()
        check("a tool with **kwargs is refused at definition", False)
    except TypeError:
        check("a tool with **kwargs is refused at definition", True)

    # The schema the model sees is exactly the one Pipecat derives.
    direct = DirectFunctionWrapper(exploding).to_function_schema().to_default_dict()
    check("the strict schema is the direct schema", strict_tool(exploding, audit=AuditContext()).to_default_dict() == direct)


async def check_unauthorized() -> None:
    """Actions the call's state forbids, whatever the model asks."""
    print("\n=== unauthorized actions ===")

    world = World(telephony=FakeTelephony(), call=phone_call(), transfer_number="+923009999999")
    await world.says("Take me off your list and don't call again.")
    check("the do-not-call is forced first", world.state is ConversationState.DO_NOT_CALL)
    for name, arguments in (
        ("check_calendar_availability", {"day": "2026-09-07"}),
        ("book_meeting", {"start": "2026-09-07T10:00"}),
        ("schedule_callback", {"when": "2026-09-09T10:00"}),
        ("transfer_to_human", {}),
        ("move_to_stage", {"stage": "value"}),
    ):
        result = await world.calls(name, arguments=arguments)
        check(f"after a do-not-call, {name} is not authorized", result["success"] is False and result["error_code"] == NOT_AUTHORIZED, str(result["error_code"]))
    check("and none of it reached the world", not world.calendar.availability_calls and not world.store.callbacks and not world.telephony.transfers)
    check("the guidance is the do-not-call closing", all("removed" in r["guidance"] or "do not sell" in r["guidance"].lower() for r in world.results[-5:]))

    declined = World()
    await declined.calls("set_interest", level="NOT_INTERESTED", reason="said no")
    result = await declined.calls("check_calendar_availability", day="2026-09-07")
    check("after a clear no, the calendar is not offered", result["error_code"] == NOT_AUTHORIZED and "said no" in result["message"])
    result = await declined.calls("request_meeting", when="Thursday")
    check("nor is a meeting recorded", result["error_code"] == NOT_AUTHORIZED and declined.record.meeting_intent is Intent.UNKNOWN)
    result = await declined.calls("schedule_callback", when="2026-09-09T10:00")
    check("but a callback after a no is allowed — people do ask", result["success"] is True and declined.state is ConversationState.CALLBACK)

    # Authorization at the service, for the cases the conversation cannot see.
    outcome = await World(calendar=None).service.check_availability(MONDAY, None)
    check("the service refuses a calendar it does not have", not outcome.ok and outcome.error_code == UNAVAILABLE)
    outcome = await World(brief=brief_for(prospect_id=None)).service.schedule_callback(datetime(2026, 9, 9, 10, 0, tzinfo=KARACHI))
    check("the service refuses a callback with no prospect", not outcome.ok and outcome.error_code == "no_prospect")


async def check_logging() -> None:
    """Every tool call leaves one line with the ids, the arguments and the verdict."""
    print("\n=== logging ===")

    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="INFO", format="{message}")
    try:
        world = World(call=phone_call())
        await world.calls("check_calendar_availability", day="2026-09-07")
        await world.calls("book_meeting", start="2026-09-07T10:00", notes="fuel")
        await world.calls("book_meeting", start="2026-09-07T11:00")
        await world.calls("schedule_callback", when="not a time")
    finally:
        logger.remove(sink_id)

    tool_lines = [line for line in lines if line.startswith("TOOL |")]
    check("one line per tool call", len(tool_lines) == 4, str(len(tool_lines)))
    first = tool_lines[0]
    check("it names the tool", first.startswith("TOOL | check_calendar_availability |"))
    check("and the session, call, prospect and attempt", all(part in first for part in ("session=sess-1", "call=CA123", "prospect=7", "attempt=11")))
    check("and the arguments", 'args={"day": "2026-09-07"}' in first)
    check("and the verdict with timing", " ok in " in first and "ms" in first)
    check("and a summary of the result", "slots[3]" in first)
    booked = tool_lines[1]
    check("a booking logs its reference", "ok in" in booked and "reference=ext-1" in booked)
    failed = tool_lines[2]
    check("a failure logs the code", "FAIL slot_not_offered" in failed)
    check("and the message", "not one of the times" in failed)
    check("a rejected argument logs as invalid_time", "FAIL invalid_time" in tool_lines[3])

    check("credential-shaped arguments are redacted", _render_arguments({"api_key": "cal_live_abc", "day": "x"}) == '{"api_key": "***", "day": "x"}')
    check("long values are clipped", len(_render_arguments({"q": "x" * 500})) < 120)

    outcome = world.conversation.outcome()
    check("the outcome record lists every action", [a["tool"] for a in outcome["actions"]] == ["check_calendar_availability", "book_meeting", "book_meeting", "schedule_callback"])
    check("with success and error codes", [a["success"] for a in outcome["actions"]] == [True, True, False, False] and outcome["actions"][2]["error_code"] == SLOT_NOT_OFFERED)
    check("and the capabilities", "calendar" in outcome["capabilities"])


async def check_result_shape() -> None:
    """Every result, success or failure, has the one shape."""
    print("\n=== the result shape ===")

    world = World(knowledge=FakeKnowledge([]), telephony=FakeTelephony(mode="refuse"), call=phone_call(), transfer_number="+923009999999")
    await world.calls("record_discovery", pain_point="fuel")
    await world.calls("record_objection", kind="price")
    await world.calls("set_interest", level="CURIOUS")
    await world.calls("move_to_stage", stage="meeting")
    await world.calls("request_meeting", when="Monday")
    await world.calls("search_knowledge_base", query="pricing")
    await world.calls("check_calendar_availability", day="2026-09-07")
    await world.calls("book_meeting", start="2026-09-07T10:00")
    await world.calls("schedule_callback", when="2026-09-09T10:00")
    await world.calls("transfer_to_human")
    await world.calls("mark_do_not_call")
    await world.calls("end_call")
    check("all twelve tools were called", len(world.results) == 12)
    check("every result has exactly the five keys and the invariant", all(shaped(r) for r in world.results), str([set(r) for r in world.results if not shaped(r)]))
    check("every result carries guidance", all(r["guidance"] for r in world.results))
    check("a failure always has a code from the vocabulary", all(r["error_code"] for r in world.results if not r["success"]))
    check("ToolResult refuses an unknown code", _raises(lambda: ToolResult.fail("made_up", "x"), ValueError))
    check("the successful ones are the ones that happened", [r["success"] for r in world.results] == [True, True, True, True, True, True, True, True, True, False, True, True])


async def check_timeparse() -> None:
    """The strict readers."""
    print("\n=== reading dates and times ===")

    check("a day parses", parse_day("2026-09-07") == MONDAY)
    check("a day with spaces parses", parse_day(" 2026-09-07 ") == MONDAY)
    check("a timestamp reduces to its day", parse_day("2026-09-07T10:00") == MONDAY)
    check("an impossible day is None", parse_day("2026-02-30") is None)
    check("prose is None", parse_day("next Tuesday") is None and parse_day("") is None and parse_day(None) is None)
    check("a naive moment lands in the zone", parse_when("2026-09-07T10:00", KARACHI) == datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI))
    check("a space separator is fine", parse_when("2026-09-07 10:00", KARACHI) == datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI))
    check("seconds are fine", parse_when("2026-09-07T10:00:00", KARACHI) is not None)
    check("Z converts into the zone", parse_when("2026-09-07T05:00Z", KARACHI) == datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI))
    check("an offset converts into the zone", parse_when("2026-09-07T06:00+01:00", KARACHI) == datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI))
    check("25 o'clock is None", parse_when("2026-09-07T25:00", KARACHI) is None)
    check("a bare date is not a moment", parse_when("2026-09-07", KARACHI) is None)
    check("2pm reads as 14:00", parse_clock("2pm") == time(14, 0) and parse_clock("2 p.m.") == time(14, 0))
    check("12am is midnight, 12pm is noon", parse_clock("12am") == time(0, 0) and parse_clock("12pm") == time(12, 0))
    check("14:30 reads as itself", parse_clock("14:30") == time(14, 30))
    check("a part of the day is a sorting anchor", parse_clock("morning") == time(9, 0) and parse_clock("the afternoon") == time(14, 0))
    check("prose is None", parse_clock("whenever") is None)
    check("UTC resolves without tzdata", resolve_timezone("UTC") is UTC and resolve_timezone(None) is UTC)
    check("a named zone resolves", resolve_timezone("Asia/Karachi").key == "Asia/Karachi")
    check("an unknown zone raises", _raises(lambda: resolve_timezone("Mars/Olympus"), ValueError))


def _raises(call, exception_type) -> bool:
    try:
        call()
    except exception_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def main() -> int:
    """Run every check and report."""
    print("Phase 7 action checks — no keys, no database, no calendar, no phone.")

    await check_capabilities()
    await check_calendar_lookup()
    await check_booking()
    await check_callbacks()
    await check_dnc()
    await check_end_call()
    await check_transfer()
    await check_knowledge()
    await check_malformed_arguments()
    await check_unauthorized()
    await check_logging()
    await check_result_shape()
    await check_timeparse()

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
