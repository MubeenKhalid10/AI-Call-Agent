#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the post-call result system (Phase 8). No keys, no database, no audio.

Run it from the `server/` directory::

    uv run python tests/test_results.py

**What this covers.** Every result here is built from a *real* conversation:
the actual `SalesConversation`, driven through the actual tools exactly as
Pipecat invokes them, with the calendar, callback and transfer backends
stubbed to succeed or to be absent. The outcome that produces is what the sink
receives on a live call, and `build_conversation_result` is given that outcome
and nothing else. The carrier-side results are built from attempt rows the
dialer would have written. So what is checked is the shape a CRM would read,
end to end, minus PostgreSQL — which `tests/test_campaigns.py` covers, in a
temporary schema, including the upsert rule between the two writers.

The twelve cases the phase asks for are the `=== ... ===` sections: a
successful call, a failed call, no answer, do-not-call, a booked meeting, a
callback, an uninterested prospect, an incomplete conversation, a malformed
record, missing optional values, transcript preservation and summary
generation — plus the disposition precedence table, the question extraction,
the validator, and the export shape.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.services.llm_service import FunctionCallParams  # noqa: E402

from src.campaigns import (  # noqa: E402
    SCHEMA_VERSION,
    CallAttempt,
    CallAttemptStatus,
    CallbackOutcome,
    CallResult,
    CallResultValidationError,
    CallSummary,
    Disposition,
    MeetingOutcome,
    ResultSource,
    attempt_status_for,
    build_carrier_result,
    build_conversation_result,
    derive_disposition,
    extract_questions,
    status_for_final_state,
    validate_call_result,
)
from src.conversation import (  # noqa: E402
    ActionOutcome,
    AttendeeDetails,
    BuyingTimeline,
    CallBrief,
    CampaignBrief,
    Capabilities,
    ConversationState,
    DecisionRole,
    InterestLevel,
    NextAction,
    ProspectBrief,
    QualificationStatus,
    SalesConversation,
)
from src.conversation.tools import build_tools  # noqa: E402

_failures: list[str] = []

KARACHI = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=KARACHI)
MONDAY_TEN = datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI)


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


# --- Stubs ------------------------------------------------------------------


class ScriptedBackend:
    """An `ActionBackend` on which everything succeeds, so results can carry
    a booking, a schedule or a transfer without a calendar or a carrier."""

    def __init__(self, *, transfer: bool = False) -> None:
        self._capabilities = Capabilities(
            can_check_calendar=True,
            can_book_meeting=True,
            can_schedule_callback=True,
            can_transfer=transfer,
            timezone="Asia/Karachi",
        )

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    async def search_knowledge(self, query: str) -> ActionOutcome:
        return ActionOutcome.success(found=False, passages=[])

    async def check_availability(self, day: date, preferred_time: time | None) -> ActionOutcome:
        return ActionOutcome.success(
            slots=[{"start": "2026-09-07T10:00", "end": "2026-09-07T10:30", "label": "Monday at ten"}],
            timezone="Asia/Karachi",
        )

    async def book_meeting(self, start: datetime, attendee: AttendeeDetails, *, notes: str = "") -> ActionOutcome:
        return ActionOutcome.success(
            start="2026-09-07T10:00", end="2026-09-07T10:30", label="Monday at ten",
            timezone="Asia/Karachi", provider="local", reference="meeting-1",
        )

    async def schedule_callback(self, when: datetime, *, note: str = "") -> ActionOutcome:
        local = when.astimezone(KARACHI)
        return ActionOutcome.success(
            scheduled_for=local.isoformat(timespec="minutes"), label="Tuesday at ten",
            timezone="Asia/Karachi", reference="callback-1",
        )

    async def transfer_to_human(self, reason: str) -> ActionOutcome:
        return ActionOutcome.success(destination="+92 *** 4567")

    async def close(self) -> None:
        return None


class FakeLLM:
    async def push_frame(self, frame: Any) -> None:
        return None


class Call:
    """A real conversation driven turn by turn, exactly as the bot drives it."""

    def __init__(self, *, actions: Any = None, brief: CallBrief | None = None) -> None:
        self.conversation = SalesConversation(
            brief or brief_for(),
            actions=actions,
            timezone="Asia/Karachi",
            now=NOW,
        )
        self._tools = {tool.name: tool for tool in build_tools(self.conversation)}
        self.llm = FakeLLM()

    async def prospect_says(self, text: str) -> None:
        await self.conversation.note_user_turn(text)

    def agent_says(self, text: str, *, interrupted: bool = False) -> None:
        self.conversation.note_agent_turn(text, interrupted=interrupted)

    async def agent_calls(self, name: str, **arguments: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def result_callback(result: Any, *args: Any, **kwargs: Any) -> None:
            captured.update(result if isinstance(result, dict) else {"result": result})

        params = FunctionCallParams(
            function_name=name,
            tool_call_id="call-1",
            arguments=arguments,
            llm=self.llm,
            pipeline_worker=None,
            context=LLMContext(),
            result_callback=result_callback,
        )
        await self._tools[name].handler(params)
        return captured

    async def finish(self, **kwargs: Any) -> dict[str, Any]:
        return await self.conversation.finish(**kwargs)


def brief_for(**prospect: Any) -> CallBrief:
    return CallBrief(
        prospect=ProspectBrief(
            prospect_id=7, first_name="Sarah", last_name="Khan", company="Meridian", **prospect
        ),
        campaign=CampaignBrief(agent_name="Alex", company_name="Northwind Fleet", offer="fleet tracking"),
        campaign_id=3,
        call_attempt_id=11,
        source="campaign",
    )


def attempt_for(status: CallAttemptStatus = CallAttemptStatus.COMPLETED, **fields: Any) -> CallAttempt:
    return CallAttempt(
        id=11, prospect_id=7, campaign_id=3, campaign_prospect_id=5, attempt_number=1, status=status, **fields
    )


def result_for(outcome: Any, **kwargs: Any) -> CallResult:
    """Build the result the sink would store for this outcome, with the status the sink would set."""
    call_status = kwargs.pop("call_status", None) or attempt_status_for(outcome) or CallAttemptStatus.COMPLETED
    return build_conversation_result(attempt_for(), outcome, call_status=call_status, **kwargs)


def valid(result: CallResult) -> bool:
    problems = validate_call_result(result)
    if problems:
        print("        validation problems: " + "; ".join(problems))
    return not problems


def json_ok(result: CallResult) -> bool:
    try:
        json.dumps(result.to_dict())
    except (TypeError, ValueError) as exc:
        print(f"        not JSON: {exc}")
        return False
    return True


# --- The checks -------------------------------------------------------------


async def check_successful_call() -> None:
    """A full discovery-to-meeting call: qualified, meeting agreed, not booked."""
    print("\n=== successful call ===")
    call = Call()
    call.agent_says("Hi Sarah, it's Alex from Northwind Fleet. Have you got a minute?")
    await call.prospect_says("Sure, go on then.")
    await call.agent_calls("move_to_stage", stage="discovery")
    call.agent_says("How are you keeping track of fuel across the fleet at the moment?")
    await call.prospect_says("We run forty trucks and the fuel bill is out of control. How does yours work?")
    await call.agent_calls(
        "record_discovery",
        pain_point="fuel spend is out of control across forty trucks",
        current_process="paper logs",
        impact="about ten thousand euros a month",
    )
    await call.agent_calls("set_interest", level="INTERESTED", reason="described the problem unprompted")
    await call.prospect_says("I sign off on this sort of thing, and we'd want it this quarter.")
    await call.agent_calls("record_discovery", timeline="THIS_QUARTER", decision_role="DECISION_MAKER")
    await call.agent_calls("record_objection", kind="price", detail="sounds expensive")
    await call.agent_calls("request_meeting", when="Thursday morning")
    await call.agent_calls("end_call", reason="meeting agreed")
    call.agent_says("Thursday morning it is. Speak then, Sarah.")
    outcome = await call.finish(call_duration_secs=143.2)

    result = result_for(outcome)
    check("it is a conversation result", result.source is ResultSource.CONVERSATION)
    check("the attempt status is COMPLETED", result.call_status is CallAttemptStatus.COMPLETED)
    check("the disposition is QUALIFIED", result.disposition is Disposition.QUALIFIED, result.disposition.value)
    check("qualification is derived as QUALIFIED", result.qualification_status is QualificationStatus.QUALIFIED)
    check("interest, timeline and role are carried", (result.interest_level, result.buying_timeline, result.decision_role) == (InterestLevel.INTERESTED, BuyingTimeline.THIS_QUARTER, DecisionRole.DECISION_MAKER))
    check("the pain point is verbatim", result.pain_points == ("fuel spend is out of control across forty trucks",))
    check("current process and impact are carried", result.current_process == "paper logs" and result.impact == "about ten thousand euros a month")
    check("the objection is carried, handled", result.objections[0]["kind"] == "PRICE" and result.objections[0]["handled"] is True)
    check("the meeting is AGREED, not booked", result.meeting_status is MeetingOutcome.AGREED and result.meeting_start is None)
    check("in their words", result.meeting_when == "Thursday morning")
    check("the next action is a meeting request", result.next_action is NextAction.MEETING_REQUESTED)
    check("the callback is unknown, not declined", result.callback_status is CallbackOutcome.UNKNOWN)
    check("the phone call's duration wins", result.duration_seconds == 143)
    check("the agent ended the call", result.agent_ended_call is True)
    check("turns are counted", result.caller_turns == 3 and result.agent_turns == 3, f"{result.caller_turns}/{result.agent_turns}")
    check("the final state is recorded", result.final_state == "ENDING")
    check("the timezone travels with it", result.timezone == "Asia/Karachi")
    tools = [action["tool"] for action in result.tool_actions]
    check("every tool call is in the record, in order", tools == ["move_to_stage", "record_discovery", "set_interest", "record_discovery", "record_objection", "request_meeting", "end_call"], str(tools))
    check("with its verdict", all(action["success"] for action in result.tool_actions))
    check("the prospect's question was extracted", result.questions == ("How does yours work?",), str(result.questions))
    check("no field could not be read", result.issues == (), str(result.issues))
    check("it validates", valid(result))
    check("it exports as JSON", json_ok(result))
    check("the summary says qualified", result.summary.qualification.startswith("Qualified:"), result.summary.qualification)
    check("and that nothing is booked", "nothing is booked" in result.summary.next_step, result.summary.next_step)
    check("and who ended the call", "the agent ended the call" in result.summary.what_happened, result.summary.what_happened)
    check("and how far it got", "the meeting request before the goodbye" in result.summary.what_happened)
    check("and the objection", "price (handled): sounds expensive" in result.summary.objections, result.summary.objections)


async def check_meeting_booked() -> None:
    """A booking confirmed by the backend: MEETING_BOOKED with the slot."""
    print("\n=== meeting booked ===")
    call = Call(actions=ScriptedBackend())
    await call.prospect_says("Would Monday morning work?")
    await call.agent_calls("record_discovery", pain_point="fuel", decision_role="decision_maker")
    await call.agent_calls("set_interest", level="interested")
    await call.agent_calls("check_calendar_availability", day="2026-09-07", preferred_time="morning")
    booking = await call.agent_calls("book_meeting", start="2026-09-07T10:00")
    check("the stub booked it", booking["success"] is True)
    await call.agent_calls("end_call", reason="booked")
    result = result_for(await call.finish())

    check("the disposition is MEETING_BOOKED", result.disposition is Disposition.MEETING_BOOKED, result.disposition.value)
    check("it outranks QUALIFIED", result.qualification_status is QualificationStatus.QUALIFIED)
    check("the meeting is BOOKED", result.meeting_status is MeetingOutcome.BOOKED)
    check("with a timezone-aware start", result.meeting_start == MONDAY_TEN and result.meeting_start.tzinfo is not None, str(result.meeting_start))
    check("and the provider's reference", result.meeting_reference == "meeting-1")
    check("the next action is MEETING_BOOKED", result.next_action is NextAction.MEETING_BOOKED)
    check("it validates", valid(result))
    check("the summary names the slot", "Meeting booked for Mon 07 Sep 2026 10:00 Asia/Karachi" in result.summary.next_step, result.summary.next_step)
    check("and the reference", "meeting-1" in result.summary.next_step)


async def check_callback() -> None:
    """A callback: scheduled when the backend confirms it, requested when it cannot."""
    print("\n=== callback ===")
    call = Call(actions=ScriptedBackend())
    await call.prospect_says("Call me Tuesday at ten.")
    scheduled = await call.agent_calls("schedule_callback", when="2026-09-08T10:00", note="mornings")
    check("the stub scheduled it", scheduled["success"] is True)
    await call.agent_calls("end_call", reason="callback")
    result = result_for(await call.finish())
    check("the attempt status is CALLBACK_REQUESTED", result.call_status is CallAttemptStatus.CALLBACK_REQUESTED)
    check("the disposition is CALLBACK_REQUESTED", result.disposition is Disposition.CALLBACK_REQUESTED)
    check("the callback is SCHEDULED", result.callback_status is CallbackOutcome.SCHEDULED)
    check("at an aware moment", result.callback_scheduled_for == datetime(2026, 9, 8, 10, 0, tzinfo=KARACHI), str(result.callback_scheduled_for))
    check("it validates", valid(result))
    check("the summary says scheduled", "Callback scheduled for Tue 08 Sep 2026 10:00 Asia/Karachi" in result.summary.next_step, result.summary.next_step)

    bare = Call()
    await bare.prospect_says("Ring me next week.")
    unscheduled = await bare.agent_calls("schedule_callback", when="next week")
    check("with no backend the tool fails plainly", unscheduled["success"] is False)
    await bare.agent_calls("end_call")
    result = result_for(await bare.finish())
    check("the disposition is still CALLBACK_REQUESTED", result.disposition is Disposition.CALLBACK_REQUESTED)
    check("but the callback is REQUESTED, not scheduled", result.callback_status is CallbackOutcome.REQUESTED and result.callback_scheduled_for is None)
    check("in their words", result.callback_when == "next week")
    check("it validates", valid(result))
    check("the summary says a person must arrange it", "no callback is scheduled" in result.summary.next_step, result.summary.next_step)


async def check_not_interested() -> None:
    """A recorded no: NOT_INTERESTED, disqualified, and the summary says why."""
    print("\n=== uninterested ===")
    call = Call()
    await call.prospect_says("Honestly, we're happy where we are.")
    await call.agent_calls("set_interest", level="NOT_INTERESTED", reason="happy with their current provider")
    await call.agent_calls("end_call", reason="not interested")
    result = result_for(await call.finish())
    check("the attempt status is NOT_INTERESTED", result.call_status is CallAttemptStatus.NOT_INTERESTED)
    check("the disposition is NOT_INTERESTED", result.disposition is Disposition.NOT_INTERESTED)
    check("interest is the recorded no", result.interest_level is InterestLevel.NOT_INTERESTED)
    check("which disqualifies", result.qualification_status is QualificationStatus.DISQUALIFIED)
    check("the objection is recorded", any(o["kind"] == "NOT_INTERESTED" for o in result.objections))
    check("it validates", valid(result))
    check("the summary says why", "they said they are not interested" in result.summary.qualification, result.summary.qualification)
    check("and the interest line agrees", "not interested" in result.summary.interest)

    # "Not now — ring me in March" is both a no and a callback, and the
    # callback is what somebody has to act on.
    later = Call()
    await later.prospect_says("Not interested right now, but try me in March.")
    await later.agent_calls("set_interest", level="not_interested")
    await later.agent_calls("schedule_callback", when="March")
    result = result_for(await later.finish())
    check("a callback after a no is CALLBACK_REQUESTED", result.disposition is Disposition.CALLBACK_REQUESTED, result.disposition.value)
    check("with the no still on record", result.interest_level is InterestLevel.NOT_INTERESTED)
    check("it validates", valid(result))


async def check_do_not_call() -> None:
    """A do-not-call: outranks everything, and a later routing note cannot undo it."""
    print("\n=== do not call ===")
    call = Call()
    await call.agent_calls("move_to_stage", stage="discovery")
    await call.prospect_says("Take me off your list and don't call me again.")
    check("the detector forced the state", call.conversation.state is ConversationState.DO_NOT_CALL)
    # A later request for a person writes HUMAN_FOLLOW_UP into the record; the
    # result must not let that read as "somebody should call them".
    await call.prospect_says("Actually, can I speak to a real person about this?")
    check("which the record now says", call.conversation.record.next_action is NextAction.HUMAN_FOLLOW_UP)
    await call.agent_calls("end_call", reason="removed")
    result = result_for(await call.finish())
    check("the attempt status is DO_NOT_CALL", result.call_status is CallAttemptStatus.DO_NOT_CALL)
    # Phase 19: the conversation heard the request, so the disposition says "told", not "known".
    check("the disposition is OPTED_OUT (the verbal request; DO_NOT_CALL is the list)", result.disposition is Disposition.OPTED_OUT)
    check("the next action is DO_NOT_CONTACT regardless", result.next_action is NextAction.DO_NOT_CONTACT)
    check("and the override is on record", any("HUMAN_FOLLOW_UP" in issue for issue in result.issues), str(result.issues))
    check("the request for a person is still a fact", result.human_requested is True)
    check("it validates", valid(result))
    check("the summary says do not contact", result.summary.next_step == "Do not contact them again.", result.summary.next_step)
    check("and why they are not qualified", "they asked not to be contacted" in result.summary.qualification)


async def check_carrier_results() -> None:
    """No answer, busy, failed: a result with nothing inferred, and a completed call the bot never wrote."""
    print("\n=== no answer, busy, failed ===")
    for status, disposition, sentence in (
        (CallAttemptStatus.NO_ANSWER, Disposition.NO_ANSWER, "The call was not answered."),
        (CallAttemptStatus.BUSY, Disposition.BUSY, "The line was busy."),
    ):
        result = build_carrier_result(attempt_for(status))
        check(f"{status.value} is a carrier result", result.source is ResultSource.CARRIER)
        check(f"{status.value} keeps its disposition", result.disposition is disposition)
        check(f"{status.value} was not reached", not result.reached)
        check(f"{status.value} knows nothing about the person", result.qualification_status is QualificationStatus.UNKNOWN and result.interest_level is InterestLevel.UNKNOWN)
        check(f"{status.value} leaves the tri-states unknown, not false", result.human_requested is None and result.agent_ended_call is None)
        check(f"{status.value} has no transcript", result.transcript == () and result.questions == ())
        check(f"{status.value} says what happened", result.summary.what_happened == sentence)
        check(f"{status.value} says the rest is not applicable", "Not applicable" in result.summary.qualification)
        check(f"{status.value} validates", valid(result))
        check(f"{status.value} exports", json_ok(result))

    failed = build_carrier_result(attempt_for(CallAttemptStatus.FAILED, failure_reason="the number is not in service"))
    check("FAILED keeps the carrier's reason", result_reason(failed) == "the number is not in service")
    check("and puts it in the summary", failed.summary.what_happened == "The call failed: the number is not in service.")
    check("and says not to retry blindly", "Check the number" in failed.summary.next_step)
    check("FAILED validates", valid(failed))

    completed = build_carrier_result(attempt_for(CallAttemptStatus.COMPLETED, duration_seconds=45))
    check("a completed call the bot never wrote is COMPLETED", completed.disposition is Disposition.COMPLETED)
    check("with the carrier's duration", completed.duration_seconds == 45)
    check("and says the agent left no record", "the agent left no conversation record" in completed.summary.what_happened, completed.summary.what_happened)
    check("everything else unknown", completed.qualification_status is QualificationStatus.UNKNOWN)
    check("it validates", valid(completed))

    try:
        build_carrier_result(attempt_for(CallAttemptStatus.CALLING))
        check("a live attempt has no result yet", False, "no error")
    except ValueError:
        check("a live attempt has no result yet", True)


def result_reason(result: CallResult) -> str | None:
    return result.failure_reason


async def check_incomplete_conversation() -> None:
    """The line dropped during discovery: nothing established, nothing inferred."""
    print("\n=== incomplete conversation ===")
    call = Call()
    call.agent_says("Hi Sarah, it's Alex from Northwind Fleet.")
    await call.prospect_says("Hello? Who is this?")
    await call.agent_calls("move_to_stage", stage="discovery")
    call.agent_says("I'm calling about fuel spend across your fleet. How do you", interrupted=True)
    await call.prospect_says("Hang on, I've got another call coming in.")
    # No end_call: the caller hung up. The sink still gets an outcome.
    result = result_for(await call.finish())
    check("the disposition is COMPLETED", result.disposition is Disposition.COMPLETED)
    check("the attempt status is COMPLETED", result.call_status is CallAttemptStatus.COMPLETED)
    check("qualification is UNKNOWN, not disqualified", result.qualification_status is QualificationStatus.UNKNOWN)
    check("interest is UNKNOWN, not reluctant", result.interest_level is InterestLevel.UNKNOWN)
    check("the meeting and callback are UNKNOWN", result.meeting_status is MeetingOutcome.UNKNOWN and result.callback_status is CallbackOutcome.UNKNOWN)
    check("the agent did not end it", result.agent_ended_call is False)
    check("it ended during discovery", result.final_state == "DISCOVERY")
    check("the two caller turns are there", result.caller_turns == 2 and len(result.transcript) == 4)
    check("the interrupted reply is marked", result.transcript[2]["interrupted"] is True)
    check("it validates", valid(result))
    check("the summary says the line closed", "the line closed before the agent ended the call" in result.summary.what_happened, result.summary.what_happened)
    check("and where it got to", "ended during discovery" in result.summary.what_happened)
    check("and that nothing was established", "Qualification not established" in result.summary.qualification)
    check("and no next step", result.summary.next_step == "No next step was established.")
    check("the question is extracted", "Who is this?" in result.questions, str(result.questions))


async def check_transferred() -> None:
    """A live transfer: TRANSFERRED, and the follow-up belongs to the person."""
    print("\n=== transferred ===")
    call = Call(actions=ScriptedBackend(transfer=True))
    await call.prospect_says("Can I speak to an actual person?")
    transfer = await call.agent_calls("transfer_to_human", reason="asked for a person")
    check("the stub transferred it", transfer["success"] is True)
    result = result_for(await call.finish())
    check("the disposition is TRANSFERRED", result.disposition is Disposition.TRANSFERRED)
    check("transferred is True", result.transferred is True)
    check("the next action is TRANSFERRED", result.next_action is NextAction.TRANSFERRED)
    check("it validates", valid(result))
    check("the summary hands over the follow-up", "transferred to a person" in result.summary.next_step)
    check("and says so in what happened", "transferred to a person" in result.summary.what_happened)


async def check_malformed() -> None:
    """A record that is not what the builder expects: nothing invented, everything named."""
    print("\n=== malformed structured result ===")
    result = result_for("this is not a record")
    check("a string outcome still builds", result.disposition is Disposition.COMPLETED)
    check("and says what it saw", any("not a record" in issue for issue in result.issues), str(result.issues))
    check("and validates", valid(result))

    odd = {
        "final_state": "FLYING",
        "qualification": "nope",
        "transcript": "nope",
        "actions": 5,
        "user_turns": -1,
        "agent_turns": True,
        "timezone": "Mars/Olympus",
        "duration_secs": "long",
        "agent_ended_call": "yes",
    }
    result = result_for(odd)
    issues = " | ".join(result.issues)
    check("every bad field is named", all(word in issues for word in ("final_state", "qualification", "transcript", "actions", "user_turns", "agent_turns", "timezone", "duration_secs", "agent_ended_call")), issues)
    check("and left unknown", result.final_state is None and result.caller_turns is None and result.agent_ended_call is None and result.duration_seconds is None)
    check("the zone falls back to UTC", result.timezone == "UTC")
    check("it validates", valid(result))

    bad_values = {
        "final_state": "ENDING",
        "qualification": {
            "interest_level": "MAYBE",
            "buying_timeline": 7,
            "decision_role": "CEO",
            "next_action": "PARTY",
            "meeting_intent": "SURE",
            "meeting_booked": "yes",
            "meeting_start": "tomorrow",
            "callback_scheduled_for": "soon",
            "pain_points": "fuel",
            "objections": [{"detail": "no kind"}, "not a dict", {"kind": "price", "detail": "too much", "handled": "yes"}],
            "notes": [None, "", 42, "kept"],
            "human_requested": "yes",
            "qualification_status": "QUALIFIED",
        },
        "transcript": [{"role": "narrator", "text": "x"}, "y", {"role": "user", "text": "  "}, {"role": "user", "text": "kept?"}],
        "actions": [{"summary": "no tool"}, {"tool": "end_call", "success": "yes"}],
    }
    result = result_for(bad_values)
    check("unknown enum values stay UNKNOWN", result.interest_level is InterestLevel.UNKNOWN and result.buying_timeline is BuyingTimeline.UNKNOWN and result.decision_role is DecisionRole.UNKNOWN and result.next_action is NextAction.UNKNOWN)
    check("a claimed QUALIFIED with no evidence is not believed", result.qualification_status is QualificationStatus.UNKNOWN)
    check("and the claim is on record", any("evidence supports UNKNOWN" in issue for issue in result.issues), " | ".join(result.issues))
    check("'yes' is not a booking", result.meeting_status is MeetingOutcome.UNKNOWN and result.meeting_start is None)
    check("an unreadable callback time is REQUESTED, not SCHEDULED", result.callback_status is CallbackOutcome.REQUESTED and result.callback_scheduled_for is None)
    check("a string is not a list of pain points", result.pain_points == ())
    check("only the well-formed objection survives", result.objections == ({"kind": "PRICE", "detail": "too much", "handled": False},), str(result.objections))
    check("notes keep only text", result.notes == ("42", "kept"), str(result.notes))
    check("'yes' is not True", result.human_requested is None)
    check("only the well-formed transcript turn survives", [entry["text"] for entry in result.transcript] == ["kept?"])
    check("only the named action survives, with an honest verdict", result.tool_actions == ({"tool": "end_call", "success": False},), str(result.tool_actions))
    check("it validates", valid(result))
    check("and exports", json_ok(result))

    unknown_is_not_no = result_for({"final_state": "ENDING", "qualification": {"qualification_status": "DISQUALIFIED"}})
    check("a claimed DISQUALIFIED with no evidence is UNKNOWN", unknown_is_not_no.qualification_status is QualificationStatus.UNKNOWN)
    check("and the disposition is COMPLETED, not NOT_INTERESTED", unknown_is_not_no.disposition is Disposition.COMPLETED)


async def check_missing_optional_values() -> None:
    """A record with nothing in it: every field unknown, and still a valid result."""
    print("\n=== missing optional values ===")
    for label, outcome in (("an empty record", {}), ("a record with only a state", {"final_state": "ENDING", "qualification": {}})):
        result = result_for(outcome)
        check(f"{label} builds", result.disposition is Disposition.COMPLETED)
        check(f"{label} leaves every enum UNKNOWN", all(getattr(result, name).value == "UNKNOWN" for name in ("qualification_status", "interest_level", "buying_timeline", "decision_role", "next_action", "meeting_status", "callback_status")))
        check(f"{label} leaves every text None", all(getattr(result, name) is None for name in ("existing_provider", "current_process", "impact", "desired_outcome", "meeting_when", "callback_when", "meeting_reference", "duration_seconds")))
        check(f"{label} leaves the tri-states None", result.human_requested is None and result.transferred is None and result.agent_ended_call is None)
        check(f"{label} leaves the counts unknown", result.caller_turns is None and result.agent_turns is None)
        check(f"{label} has an honest summary", "did not speak" not in result.summary.what_happened and "not established" in result.summary.qualification, result.summary.what_happened)
        check(f"{label} validates", valid(result))
        check(f"{label} exports", json_ok(result))


async def check_transcript_preservation() -> None:
    """The transcript is what was said, in order, untouched, and separate from the summary."""
    print("\n=== transcript preservation ===")
    call = Call()
    lines = [
        ("assistant", "Hi Sarah, it's Alex from Northwind Fleet. Have you got a minute?", False),
        ("user", "Hello? Who is this?", False),
        ("assistant", "Alex, from Northwind. We help fleets cut fuel spend — I wanted to ask how you", True),
        ("user", "We run forty trucks. What does it cost?", False),
        ("assistant", "It depends on the fleet size; for forty trucks it is usually a few hundred a month.", False),
    ]
    for role, text, interrupted in lines:
        if role == "user":
            await call.prospect_says(text)
        else:
            call.agent_says(text, interrupted=interrupted)
    call.agent_says("", interrupted=True)  # Cut off before a word: nothing was heard.
    outcome = await call.finish()

    check("the outcome carries the transcript", len(outcome["transcript"]) == 5)
    result = result_for(outcome)
    check("every turn is kept", len(result.transcript) == 5)
    check("in order", [entry["role"] for entry in result.transcript] == [role for role, _, _ in lines])
    check("word for word", [entry["text"] for entry in result.transcript] == [text for _, text, _ in lines])
    check("with the interruption marked", [entry["interrupted"] for entry in result.transcript] == [flag for _, _, flag in lines])
    check("and timestamps that do not go backwards", all(a["at"] <= b["at"] for a, b in zip(result.transcript, result.transcript[1:])))
    check("an empty interrupted turn leaves no entry", outcome["agent_turns"] == 4 and result.agent_turns == 4)
    rendered = result.transcript_text()
    check("it renders one turn per line", rendered.count("\n") == 4 and "PROSPECT: Hello? Who is this?" in rendered and "(interrupted)" in rendered)
    summary = result.summary.text
    check("the summary is not the transcript", all(text not in summary for _, text, _ in lines))
    check("and the transcript is not the summary", "What happened" not in rendered)
    check("the questions come from the transcript", result.questions == ("Who is this?", "What does it cost?"), str(result.questions))
    check("the export carries both, separately", set(result.to_dict()) >= {"transcript", "summary"} and json_ok(result))


async def check_summary_generation() -> None:
    """Six parts, each from the record, each honest about what it does not know."""
    print("\n=== summary generation ===")
    call = Call()
    await call.prospect_says("We've got a provider already, and it's pricey enough as it is.")
    await call.agent_calls("record_discovery", pain_point="trucks idling for hours", existing_provider="Fleetwise", desired_outcome="fewer idle hours")
    await call.agent_calls("record_objection", kind="existing_provider", detail="already with Fleetwise")
    await call.agent_calls("record_objection", kind="price", detail="pricey enough as it is")
    await call.agent_calls("set_interest", level="curious")
    await call.agent_calls("record_discovery", timeline="this_year", decision_role="influencer")
    result = result_for(await call.finish())
    summary = result.summary
    exported = result.to_dict()["summary"]
    check("the summary has the six parts and a text", set(exported) == {"what_happened", "prospect_needs", "objections", "interest", "qualification", "next_step", "text"})
    check("the text is the six parts, labelled", summary.text.startswith("What happened:") and "Next step:" in summary.text)
    check("needs come from the record", "Pain points: trucks idling for hours." in summary.prospect_needs and "Existing provider: Fleetwise." in summary.prospect_needs and "Desired outcome: fewer idle hours." in summary.prospect_needs, summary.prospect_needs)
    check("and say what was not asked", "How they handle it today" not in summary.prospect_needs)
    check("objections are listed, open ones marked open", "existing provider (open): already with Fleetwise" in summary.objections and "price (open): pricey enough as it is" in summary.objections, summary.objections)
    check("interest is described", summary.interest == "Interest: listening but not committed.")
    check("qualification names what is established and what is not", summary.qualification.startswith("Qualified:"), summary.qualification)
    check("with the timeline and role", "Timeline: this year." in summary.qualification and "Decision role: involved in the decision without owning it." in summary.qualification)
    check("the next step is honest about being unknown", summary.next_step == "No next step was established.")
    check("it validates", valid(result))

    partial = Call()
    await partial.agent_calls("record_discovery", pain_point="fuel")
    result = result_for(await partial.finish())
    check("a lone pain point is partially qualified, and says which parts are missing", result.summary.qualification == "Partially qualified: a need established; interest and authority not established.", result.summary.qualification)

    empty = result_for({"final_state": "ENDING"})
    check("an empty record says nothing was established", empty.summary.prospect_needs == "No needs were established." and empty.summary.objections == "No objections were recorded." and empty.summary.interest == "Interest level was not established.")
    check("and invents no next step", empty.summary.next_step == "No next step was established.")
    for word in ("interested", "qualified:", "booked", "scheduled"):
        check(f"and never says {word!r}", word not in empty.summary.text.lower().replace("not qualified", "").replace("qualification not established", ""))


async def check_questions() -> None:
    """The prospect's questions, from their turns only, verbatim."""
    print("\n=== questions ===")
    transcript = [
        {"role": "assistant", "text": "Would Monday work for you?"},
        {"role": "user", "text": "What does it cost? We run forty trucks."},
        {"role": "user", "text": "Do you have an office in Lahore"},
        {"role": "user", "text": "Sure. How long does setup take?"},
        {"role": "user", "text": "what does it cost?"},
        {"role": "user", "text": "Yeah, we do it by hand at the moment."},
        {"role": "user", "text": "Can't say I'm keen."},
        {"role": "user", "text": "Is this a recording"},
        "not a turn",
        {"role": "user", "text": 42},
    ]
    questions = extract_questions(transcript)
    check("questions with a question mark are kept", "What does it cost?" in questions)
    check("questions with no question mark are kept when they open with an interrogative", "Do you have an office in Lahore" in questions and "Is this a recording" in questions)
    check("a question inside a turn is separated from the statement beside it", "How long does setup take?" in questions and "Sure." not in questions)
    check("statements are not questions", not any("forty trucks" in q or "by hand" in q or "keen" in q for q in questions), str(questions))
    check("the agent's questions are not the prospect's", not any("Monday" in q for q in questions))
    check("duplicates are kept once", sum(1 for q in questions if q.lower() == "what does it cost?") == 1)
    check("malformed entries are ignored", len(questions) == 4, str(questions))
    check("nothing is rewritten", questions[0] == "What does it cost?")
    check("an empty transcript has no questions", extract_questions([]) == ())


async def check_disposition_precedence() -> None:
    """The one-word outcome, by precedence, each branch needing a recorded fact."""
    print("\n=== disposition precedence ===")
    reached = CallAttemptStatus.COMPLETED
    check("no answer", derive_disposition(CallAttemptStatus.NO_ANSWER) is Disposition.NO_ANSWER)
    check("busy", derive_disposition(CallAttemptStatus.BUSY) is Disposition.BUSY)
    check("failed", derive_disposition(CallAttemptStatus.FAILED) is Disposition.FAILED)
    check("reached with nothing recorded is COMPLETED", derive_disposition(reached) is Disposition.COMPLETED)
    check("unknown interest is not a no", derive_disposition(reached, interest_level=InterestLevel.UNKNOWN) is Disposition.COMPLETED)
    check("a recorded no is NOT_INTERESTED", derive_disposition(reached, interest_level=InterestLevel.NOT_INTERESTED) is Disposition.NOT_INTERESTED)
    check("so is ending in the NOT_INTERESTED state", derive_disposition(reached, final_state=ConversationState.NOT_INTERESTED) is Disposition.NOT_INTERESTED)
    check("a callback outranks a no", derive_disposition(reached, interest_level=InterestLevel.NOT_INTERESTED, callback_status=CallbackOutcome.REQUESTED) is Disposition.CALLBACK_REQUESTED)
    check("a proposed callback is not a request", derive_disposition(reached, callback_status=CallbackOutcome.PROPOSED) is Disposition.COMPLETED)
    check("a transfer outranks a callback", derive_disposition(reached, callback_status=CallbackOutcome.SCHEDULED, transferred=True) is Disposition.TRANSFERRED)
    check("a booked meeting outranks a transfer", derive_disposition(reached, transferred=True, meeting_status=MeetingOutcome.BOOKED) is Disposition.MEETING_BOOKED)
    check("an agreed meeting is not a booking", derive_disposition(reached, meeting_status=MeetingOutcome.AGREED, qualification_status=QualificationStatus.QUALIFIED) is Disposition.QUALIFIED)
    check("a do-not-call outranks everything", derive_disposition(reached, meeting_status=MeetingOutcome.BOOKED, next_action=NextAction.DO_NOT_CONTACT) is Disposition.DO_NOT_CALL)
    check("from the state too — as OPTED_OUT, since the conversation heard it (Phase 19)", derive_disposition(reached, final_state=ConversationState.DO_NOT_CALL) is Disposition.OPTED_OUT)
    check("a conversation that ended DO_NOT_CALL and said goodbye is OPTED_OUT too", derive_disposition(CallAttemptStatus.DO_NOT_CALL, final_state=ConversationState.ENDING) is Disposition.OPTED_OUT)
    check("a DO_NOT_CALL status with no conversation behind it stays DO_NOT_CALL (the list refused the dial)", derive_disposition(CallAttemptStatus.DO_NOT_CALL) is Disposition.DO_NOT_CALL)
    check("and from the attempt status", derive_disposition(CallAttemptStatus.DO_NOT_CALL) is Disposition.DO_NOT_CALL)
    check("qualified", derive_disposition(reached, qualification_status=QualificationStatus.QUALIFIED) is Disposition.QUALIFIED)
    check("disqualified is UNQUALIFIED", derive_disposition(reached, qualification_status=QualificationStatus.DISQUALIFIED) is Disposition.UNQUALIFIED)
    check("partially qualified is COMPLETED", derive_disposition(reached, qualification_status=QualificationStatus.PARTIALLY_QUALIFIED) is Disposition.COMPLETED)
    check("a recorded no on the attempt is NOT_INTERESTED", derive_disposition(CallAttemptStatus.NOT_INTERESTED) is Disposition.NOT_INTERESTED)
    check("the three conversation states map to attempt statuses", (status_for_final_state("DO_NOT_CALL"), status_for_final_state("CALLBACK"), status_for_final_state("NOT_INTERESTED")) == (CallAttemptStatus.DO_NOT_CALL, CallAttemptStatus.CALLBACK_REQUESTED, CallAttemptStatus.NOT_INTERESTED))
    check("and nothing else does", status_for_final_state("ENDING") is None and status_for_final_state("garbage") is None and status_for_final_state(None) is None)

    print("\n=== the attempt status, from the path ===")
    check("a callback closed with a goodbye is still a callback", attempt_status_for({"final_state": "ENDING", "state_path": ["GREETING", "DISCOVERY", "CALLBACK", "ENDING"]}) is CallAttemptStatus.CALLBACK_REQUESTED)
    check("a no closed with a goodbye is still a no", attempt_status_for({"final_state": "ENDING", "state_path": ["GREETING", "NOT_INTERESTED", "ENDING"]}) is CallAttemptStatus.NOT_INTERESTED)
    check("a no followed by a callback is a callback", attempt_status_for({"final_state": "ENDING", "state_path": ["GREETING", "NOT_INTERESTED", "CALLBACK", "ENDING"]}) is CallAttemptStatus.CALLBACK_REQUESTED)
    check("a callback that went back to discovery is not a callback", attempt_status_for({"final_state": "ENDING", "state_path": ["GREETING", "CALLBACK", "DISCOVERY", "ENDING"]}) is None)
    check("a do-not-call anywhere on the path wins", attempt_status_for({"final_state": "ENDING", "state_path": ["GREETING", "DO_NOT_CALL", "ENDING"]}) is CallAttemptStatus.DO_NOT_CALL)
    check("a call that dropped mid-callback is a callback", attempt_status_for({"final_state": "CALLBACK", "state_path": ["GREETING", "CALLBACK"]}) is CallAttemptStatus.CALLBACK_REQUESTED)
    check("a final state with no path still counts", attempt_status_for({"final_state": "NOT_INTERESTED"}) is CallAttemptStatus.NOT_INTERESTED)
    check("an ordinary ending sets nothing", attempt_status_for({"final_state": "ENDING", "state_path": ["GREETING", "DISCOVERY", "MEETING_REQUEST", "ENDING"]}) is None)
    check("garbage sets nothing", attempt_status_for("nope") is None and attempt_status_for({"state_path": "nope", "final_state": 3}) is None)


async def check_validation() -> None:
    """Results that must not be stored, and the reasons given."""
    print("\n=== validation ===")

    def refused(label: str, result: Any, *needles: str) -> None:
        problems = validate_call_result(result)
        found = all(any(needle in problem for problem in problems) for needle in needles)
        check(label, bool(problems) and found, "; ".join(problems) if problems else "accepted")

    base = build_carrier_result(attempt_for(CallAttemptStatus.NO_ANSWER))
    refused("a transcript on a call nobody answered", base.__class__(**{**base.__dict__, "transcript": ({"role": "user", "text": "hi"},)}), "nobody answered")
    refused("a false on a call nobody answered", base.__class__(**{**base.__dict__, "human_requested": False}), "unknown is not false")
    refused("interest on a call nobody answered", base.__class__(**{**base.__dict__, "interest_level": InterestLevel.CURIOUS}), "cannot be known")

    talked = result_for({"final_state": "ENDING"})
    refused("QUALIFIED with no evidence", talked.__class__(**{**talked.__dict__, "qualification_status": QualificationStatus.QUALIFIED, "disposition": Disposition.QUALIFIED}), "not supported by the evidence")
    refused("NOT_INTERESTED with unknown interest", talked.__class__(**{**talked.__dict__, "disposition": Disposition.NOT_INTERESTED}), "does not follow from the record")
    # The attempt status is written by the sink from a recorded no, so it is
    # evidence in its own right — the row is not "unknown".
    check(
        "NOT_INTERESTED from the attempt status is a recorded no",
        validate_call_result(talked.__class__(**{**talked.__dict__, "call_status": CallAttemptStatus.NOT_INTERESTED, "disposition": Disposition.NOT_INTERESTED})) == [],
    )
    refused("MEETING_BOOKED without a booking", talked.__class__(**{**talked.__dict__, "disposition": Disposition.MEETING_BOOKED}), "does not follow")
    refused("a booking claimed without the disposition", talked.__class__(**{**talked.__dict__, "meeting_status": MeetingOutcome.BOOKED}), "supports MEETING_BOOKED")
    refused("SCHEDULED without a time", talked.__class__(**{**talked.__dict__, "callback_status": CallbackOutcome.SCHEDULED, "disposition": Disposition.CALLBACK_REQUESTED}), "without a scheduled time")
    refused("DO_NOT_CALL without DO_NOT_CONTACT", talked.__class__(**{**talked.__dict__, "call_status": CallAttemptStatus.DO_NOT_CALL, "disposition": Disposition.DO_NOT_CALL}), "next_action is not DO_NOT_CONTACT")
    refused("a live attempt", talked.__class__(**{**talked.__dict__, "call_status": CallAttemptStatus.CALLING}), "not final")
    refused("an empty summary", talked.__class__(**{**talked.__dict__, "summary": CallSummary.pending()}), "summary.what_happened is empty")
    refused("a negative duration", talked.__class__(**{**talked.__dict__, "duration_seconds": -1}), "non-negative")
    refused("a naive meeting time", talked.__class__(**{**talked.__dict__, "meeting_start": datetime(2026, 9, 7, 10, 0)}), "timezone-aware")
    refused("a wrong schema version", talked.__class__(**{**talked.__dict__, "schema_version": SCHEMA_VERSION + 1}), "schema_version")
    refused("a bad prospect id", talked.__class__(**{**talked.__dict__, "prospect_id": 0}), "prospect_id")
    refused("not a result at all", {"disposition": "QUALIFIED"}, "expected a CallResult")
    check("a good result is accepted", validate_call_result(talked) == [])

    try:
        raise CallResultValidationError(["one", "two"])
    except CallResultValidationError as exc:
        check("the error lists every problem", exc.problems == ["one", "two"] and "- one" in str(exc) and "- two" in str(exc))


async def check_export() -> None:
    """The flat shape an integration reads."""
    print("\n=== export ===")
    call = Call(actions=ScriptedBackend())
    await call.prospect_says("Monday?")
    await call.agent_calls("check_calendar_availability", day="2026-09-07")
    await call.agent_calls("book_meeting", start="2026-09-07T10:00")
    result = result_for(await call.finish())
    exported = result.to_dict()
    check("every enum is its name", exported["disposition"] == "MEETING_BOOKED" and exported["interest_level"] == "INTERESTED" and exported["meeting_status"] == "BOOKED")
    check("times are ISO 8601 with an offset", exported["meeting_start"] == "2026-09-07T10:00:00+05:00", str(exported["meeting_start"]))
    check("reached is stated", exported["reached"] is True)
    check("the ids are there", (exported["call_attempt_id"], exported["prospect_id"], exported["campaign_id"]) == (11, 7, 3))
    check("the version is there", exported["schema_version"] == SCHEMA_VERSION)
    check("lists are lists", isinstance(exported["pain_points"], list) and isinstance(exported["transcript"], list) and isinstance(exported["tool_actions"], list))
    check("the summary is nested, with its text", exported["summary"]["text"].startswith("What happened:"))
    check("it is JSON", json_ok(result))
    check("and round-trips its keys", set(json.loads(json.dumps(exported))) == set(exported))


async def main() -> int:
    """Run every check and report."""
    print("Post-call result checks — no keys, no database, no audio.")

    await check_successful_call()
    await check_meeting_booked()
    await check_callback()
    await check_not_interested()
    await check_do_not_call()
    await check_carrier_results()
    await check_incomplete_conversation()
    await check_transferred()
    await check_malformed()
    await check_missing_optional_values()
    await check_transcript_preservation()
    await check_summary_generation()
    await check_questions()
    await check_disposition_precedence()
    await check_validation()
    await check_export()

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
