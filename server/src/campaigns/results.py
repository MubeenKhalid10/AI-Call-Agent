"""What a finished call produced, in one validated shape a CRM can read. Phase 8.

Every call attempt that reaches a final status gets a `CallResult`: one row,
one shape, whether the person was reached and talked for ten minutes or the
line was busy. It is built from two sources and only two:

* **the conversation's outcome** (`SalesConversation.outcome()`, arriving
  through `ConversationSink.on_call_finished`) — the rich case, with the
  qualification record, every tool action, and the transcript;
* **the carrier's report** (`CampaignService.record_outcome`) — the thin case,
  for a call nobody answered, a busy line, or a dial that failed, where there
  was no conversation and the result says so.

`ResultSource` records which, and the store's upsert rule is that a conversation
result always wins over a carrier one and a carrier one never overwrites a
conversation one — whichever order they arrive in.

**Three rules shape everything in here, and each is enforced rather than hoped
for.**

1. **Unknown is a value, and it is never turned into anything else.** Every
   enum has an `UNKNOWN` member, every free-text field is nullable, and the
   builder never fills a gap. An unparseable value leaves its field `UNKNOWN`
   and adds a line to `issues` saying what it saw. Unknown is not false:
   `human_requested` is `None` on a call where nobody was reached, not
   `False`. Unknown is not "not interested": a disposition of `NOT_INTERESTED`
   requires a *recorded* no.
2. **Qualification is derived from the evidence, never asserted.** The
   builder rebuilds it from interest, pain points, decision role, timeline and
   next action through the same rule the live call uses
   (`QualificationRecord.qualification_status`), and `validate_call_result`
   refuses a result whose claimed status the evidence does not support. So
   "qualified" means the same thing on every row, and a record edited by hand
   cannot say it.
3. **The transcript and the summary are different things.** The transcript is
   stored verbatim and is never edited. The summary is composed
   *deterministically* from the structured fields — no model reads the
   transcript and no sentence in it comes from anywhere but a recorded field —
   which is the only way to guarantee it invents nothing. Where a field is
   unknown the summary says so, by name, rather than leaving the reader to
   guess whether it was asked.

**The disposition** is one word for what the call came to, derived by
`derive_disposition` in a fixed order of precedence, and it is what a CRM
filters on. The vocabulary reuses `CallAttemptStatus` for the outcomes the
carrier decides (`NO_ANSWER`, `BUSY`, `FAILED`, `COMPLETED`) and the three the
conversation decides (`CALLBACK_REQUESTED`, `NOT_INTERESTED`, `DO_NOT_CALL`),
and adds only what the attempt status cannot say: `MEETING_BOOKED`,
`TRANSFERRED`, `QUALIFIED` and `UNQUALIFIED`.

**Mapping to a CRM (not implemented here — Phase 8 designs the shape only).**
Every field is flat, typed, and either an id, an enum value, a number, a short
text, or a JSON list, so each of the three usual targets has a home for it:

| `CallResult`                | HubSpot                              | Pipedrive                     | Salesforce                          |
|-----------------------------|--------------------------------------|-------------------------------|-------------------------------------|
| `prospect_id` → phone/email | Contact (match on phone/email)       | Person                        | Lead / Contact                      |
| the whole row               | Call engagement (`hs_call_*`)        | Activity, type `call`         | Task, `TaskSubtype=Call`            |
| `disposition`               | `hs_call_disposition`                | activity subject / custom     | `CallDisposition`                   |
| `call_status`               | `hs_call_status`                     | `done` + custom               | `Status`                            |
| `duration_seconds`          | `hs_call_duration` (×1000, ms)       | `duration`                    | `CallDurationInSeconds`             |
| `summary.text`              | `hs_call_body`                       | activity `note`               | `Description`                       |
| `transcript`                | `hs_call_body` (appended) / note     | a Note on the person          | `Description` / ContentNote         |
| `qualification_status`      | contact `hs_lead_status` / custom    | lead label / deal stage       | `Lead.Status` / `Rating`            |
| `interest_level`            | custom property                      | custom field                  | custom field                        |
| `pain_points`, `objections` | custom multi-line properties         | deal notes / custom           | custom long text                    |
| `next_action`               | Task engagement                      | follow-up Activity            | follow-up Task                      |
| `meeting_status`/`_start`   | Meeting engagement                   | Activity, type `meeting`      | Event                               |
| `callback_status`/`_for`    | Task engagement, due date            | Activity, type `call`, due    | Task, `ActivityDate`                |
| `tool_actions`              | note / custom                        | note                          | custom long text                    |
| `schema_version`            | — (for the integration's own use)    | —                             | —                                   |

The row carries `prospect_id` and `campaign_id`; an integration joins the
prospect for the phone number and email it matches on. Nothing here talks to a
CRM: Phase 15's `src/crm/mapping.py` reads this shape into a vendor-neutral
contact and activity, and `src/crm/` does the talking, from its own process.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..conversation.qualification import (
    BuyingTimeline,
    DecisionRole,
    Intent,
    InterestLevel,
    NextAction,
    QualificationRecord,
    QualificationStatus,
)
from ..conversation.states import ConversationState
from ..conversation.transcript import ROLE_ASSISTANT, ROLE_USER, render_transcript
from .models import CallAttempt, CallAttemptStatus

SCHEMA_VERSION = 1
"""Bump when a field changes meaning. A row records the version it was written under."""


class Disposition(StrEnum):
    """One word for what the call came to. What a CRM filters on.

    In order of precedence when more than one could apply — see
    `derive_disposition` for the rule that picks one.
    """

    NO_ANSWER = "NO_ANSWER"
    """Rang out. Nobody picked up."""

    BUSY = "BUSY"
    FAILED = "FAILED"
    """Could not be placed, or dropped before it was answered."""

    VOICEMAIL = "VOICEMAIL"
    """An answering machine picked up. Phase 12. Nobody was reached."""

    OPTED_OUT = "OPTED_OUT"
    """They asked, *on this call*, never to be contacted again. Phase 19.

    The verbal request the agent heard — and honoured at once, by writing
    the person's status and the number onto the do-not-call list before
    the call ended. Outranks everything else said.
    """

    DO_NOT_CALL = "DO_NOT_CALL"
    """The number is on the do-not-call list, so the call was not placed (or was closed as such).

    Before Phase 19 this was also the disposition of a verbal request; that
    is now `OPTED_OUT`, so a report can tell "they told us" from "we already
    knew". Both carry `next_action = DO_NOT_CONTACT`.
    """

    MEETING_BOOKED = "MEETING_BOOKED"
    """A slot is in the calendar, confirmed by the provider."""

    TRANSFERRED = "TRANSFERRED"
    """The live call was handed to a person."""

    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    """They want calling back — scheduled or not; `callback_status` says which."""

    NOT_INTERESTED = "NOT_INTERESTED"
    """A clear, recorded no."""

    QUALIFIED = "QUALIFIED"
    """Need, interest and authority all established, and no next step booked yet."""

    UNQUALIFIED = "UNQUALIFIED"
    """The evidence rules them out: no intention to act, or not involved and no need."""

    COMPLETED = "COMPLETED"
    """Reached, and none of the above — including a conversation that ended
    before anything was established. `qualification_status` says how far it got."""

    @property
    def reached(self) -> bool:
        """Whether this disposition means somebody answered."""
        return self not in _UNREACHED


_UNREACHED = frozenset(
    {Disposition.NO_ANSWER, Disposition.BUSY, Disposition.FAILED, Disposition.VOICEMAIL}
)


class MeetingOutcome(StrEnum):
    """Where a meeting got to on this call. Not the `meetings` table's status."""

    UNKNOWN = "UNKNOWN"
    """Never came up, or nothing was recorded about it."""

    PROPOSED = "PROPOSED"
    """One side proposed one and the other has not answered."""

    AGREED = "AGREED"
    """They agreed, and nothing is booked — a person must arrange it."""

    BOOKED = "BOOKED"
    """In the calendar. Set only from a confirmed `book_meeting`."""

    DECLINED = "DECLINED"


class CallbackOutcome(StrEnum):
    """Where a callback got to on this call. Not the `callbacks` table's status."""

    UNKNOWN = "UNKNOWN"
    PROPOSED = "PROPOSED"
    REQUESTED = "REQUESTED"
    """They want one, and none is scheduled — a person must arrange it."""

    SCHEDULED = "SCHEDULED"
    """A `callbacks` row exists. Set only from a confirmed `schedule_callback`."""

    DECLINED = "DECLINED"


class ResultSource(StrEnum):
    """Who wrote the result, which decides who may overwrite it."""

    CONVERSATION = "CONVERSATION"
    """The bot, at the end of a call it held. Wins."""

    CARRIER = "CARRIER"
    """The dialer, from the carrier's report. Never overwrites a conversation result."""


class CallResultValidationError(ValueError):
    """A result that must not be stored, with every problem found."""

    def __init__(self, problems: list[str]) -> None:
        """Record the problems; the message lists them all."""
        super().__init__("the call result is not valid:\n  - " + "\n  - ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class CallSummary:
    """The post-call summary, in six parts. Composed from the record, never from the transcript.

    Each part is one or two sentences and each says "not established" when the
    field behind it is unknown, so a reader can tell "they had no objections"
    from "nobody asked".
    """

    what_happened: str
    prospect_needs: str
    objections: str
    interest: str
    qualification: str
    next_step: str

    @property
    def text(self) -> str:
        """The six parts as one labelled paragraph, for a CRM with a single note field."""
        return "\n".join(
            f"{label}: {value}"
            for label, value in (
                ("What happened", self.what_happened),
                ("Needs", self.prospect_needs),
                ("Objections", self.objections),
                ("Interest", self.interest),
                ("Qualification", self.qualification),
                ("Next step", self.next_step),
            )
        )

    def to_dict(self) -> dict[str, str]:
        """Plain data."""
        return {
            "what_happened": self.what_happened,
            "prospect_needs": self.prospect_needs,
            "objections": self.objections,
            "interest": self.interest,
            "qualification": self.qualification,
            "next_step": self.next_step,
            "text": self.text,
        }

    @classmethod
    def pending(cls) -> CallSummary:
        """A placeholder the builder replaces. Fails validation if it survives."""
        return cls("", "", "", "", "", "")


@dataclass(frozen=True)
class CallResult:
    """What one call attempt produced. One per attempt, CRM-ready.

    Every enum field defaults to `UNKNOWN` and every nullable field to `None`,
    and the builder leaves them that way unless the record said otherwise.

    Attributes:
        source: Who wrote this — the conversation or the carrier.
        call_status: The attempt's final status, from `CallAttemptStatus`.
        disposition: The one-word outcome. Derived; see `derive_disposition`.
        duration_seconds: The bot's audio-connected view on a phone call, or
            the carrier's when the carrier wrote the row. `None` when neither
            is known.
        qualification_status: Derived from the evidence, never asserted.
        meeting_start: When a booked meeting starts, timezone-aware.
        callback_scheduled_for: When a scheduled callback is due, timezone-aware.
        meeting_when, callback_when: What the prospect *said*, in their words.
            Not a time anything can dial at.
        pain_points, questions, notes: Verbatim, as recorded.
        objections: `{"kind", "detail", "handled"}` each, from the closed
            `ObjectionKind` vocabulary.
        questions: Things the prospect asked, taken from their transcript
            turns by `extract_questions`. A filter over their words, not an
            interpretation of them.
        human_requested, transferred, agent_ended_call: Tri-state. `None`
            means it could not be known — the usual case when nobody answered.
        final_state: The conversation's last state, when there was one.
        transcript: Every turn, verbatim, `{"role", "text", "at", "interrupted"}`.
        tool_actions: Every tool call, with its verdict, from the audit log.
        issues: What the builder could not read and left unknown. Empty on a
            clean record. Kept on the row because an integration should be
            able to see that a field is unknown *because* the record was odd.
        failure_reason: Why a call could not be placed, from the attempt.
    """

    call_attempt_id: int
    prospect_id: int
    campaign_id: int | None
    source: ResultSource
    call_status: CallAttemptStatus
    disposition: Disposition
    summary: CallSummary = field(default_factory=CallSummary.pending)
    duration_seconds: int | None = None
    qualification_status: QualificationStatus = QualificationStatus.UNKNOWN
    interest_level: InterestLevel = InterestLevel.UNKNOWN
    buying_timeline: BuyingTimeline = BuyingTimeline.UNKNOWN
    decision_role: DecisionRole = DecisionRole.UNKNOWN
    next_action: NextAction = NextAction.UNKNOWN
    meeting_status: MeetingOutcome = MeetingOutcome.UNKNOWN
    meeting_start: datetime | None = None
    meeting_reference: str | None = None
    meeting_when: str | None = None
    callback_status: CallbackOutcome = CallbackOutcome.UNKNOWN
    callback_scheduled_for: datetime | None = None
    callback_when: str | None = None
    pain_points: tuple[str, ...] = ()
    objections: tuple[dict[str, Any], ...] = ()
    questions: tuple[str, ...] = ()
    existing_provider: str | None = None
    current_process: str | None = None
    impact: str | None = None
    desired_outcome: str | None = None
    notes: tuple[str, ...] = ()
    human_requested: bool | None = None
    transferred: bool | None = None
    agent_ended_call: bool | None = None
    caller_turns: int | None = None
    agent_turns: int | None = None
    final_state: str | None = None
    timezone: str = "UTC"
    transcript: tuple[dict[str, Any], ...] = ()
    tool_actions: tuple[dict[str, Any], ...] = ()
    issues: tuple[str, ...] = ()
    failure_reason: str | None = None
    schema_version: int = SCHEMA_VERSION
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def reached(self) -> bool:
        """Whether somebody answered."""
        return self.call_status.reached_person

    def transcript_text(self) -> str:
        """The transcript rendered one turn per line. Empty when there is none."""
        return render_transcript(self.transcript)

    def to_dict(self) -> dict[str, Any]:
        """The result as flat, JSON-able data — the export shape for an integration."""
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "source": self.source.value,
            "call_attempt_id": self.call_attempt_id,
            "prospect_id": self.prospect_id,
            "campaign_id": self.campaign_id,
            "call_status": self.call_status.value,
            "disposition": self.disposition.value,
            "reached": self.reached,
            "duration_seconds": self.duration_seconds,
            "failure_reason": self.failure_reason,
            "qualification_status": self.qualification_status.value,
            "interest_level": self.interest_level.value,
            "buying_timeline": self.buying_timeline.value,
            "decision_role": self.decision_role.value,
            "next_action": self.next_action.value,
            "meeting_status": self.meeting_status.value,
            "meeting_start": _iso(self.meeting_start),
            "meeting_reference": self.meeting_reference,
            "meeting_when": self.meeting_when,
            "callback_status": self.callback_status.value,
            "callback_scheduled_for": _iso(self.callback_scheduled_for),
            "callback_when": self.callback_when,
            "pain_points": list(self.pain_points),
            "objections": [dict(objection) for objection in self.objections],
            "questions": list(self.questions),
            "existing_provider": self.existing_provider,
            "current_process": self.current_process,
            "impact": self.impact,
            "desired_outcome": self.desired_outcome,
            "notes": list(self.notes),
            "human_requested": self.human_requested,
            "transferred": self.transferred,
            "agent_ended_call": self.agent_ended_call,
            "caller_turns": self.caller_turns,
            "agent_turns": self.agent_turns,
            "final_state": self.final_state,
            "timezone": self.timezone,
            "summary": self.summary.to_dict(),
            "transcript": [dict(entry) for entry in self.transcript],
            "tool_actions": [dict(action) for action in self.tool_actions],
            "issues": list(self.issues),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


# --- Building -----------------------------------------------------------------


def status_for_final_state(final_state: Any) -> CallAttemptStatus | None:
    """The attempt status one conversation state implies, if any.

    Three states come from what the person *said* and become the attempt's
    status; everything else is left to the carrier, which only ever reports
    that the call completed. Prefer `attempt_status_for`, which reads the
    whole path: a call the agent closed properly ends in `ENDING`, and the
    state that matters is the one before the goodbye.
    """
    state = _enum_or_none(ConversationState, final_state)
    if state is ConversationState.DO_NOT_CALL:
        return CallAttemptStatus.DO_NOT_CALL
    if state is ConversationState.CALLBACK:
        return CallAttemptStatus.CALLBACK_REQUESTED
    if state is ConversationState.NOT_INTERESTED:
        return CallAttemptStatus.NOT_INTERESTED
    return None


def attempt_status_for(outcome: Any) -> CallAttemptStatus | None:
    """The attempt status a conversation implies, from where it was when it closed.

    Reads the state path rather than the final state alone, because
    `end_call` moves every call to `ENDING` — so a callback agreed and then
    politely closed ends in `ENDING`, and the callback is the state before
    it. A do-not-call anywhere on the path wins: it cannot be left except
    for the goodbye, and it is a promise. One copy of the rule, shared with
    the sink, which writes what it returns onto the attempt row.

    Returns:
        One of the three statuses that come from what the person said, or
        None to leave the status to the carrier's report.
    """
    if not isinstance(outcome, dict):
        return None
    path = _state_path(outcome)
    final = _enum_or_none(ConversationState, outcome.get("final_state"))
    if final is not None and (not path or path[-1] is not final):
        path = [*path, final]
    if ConversationState.DO_NOT_CALL in path:
        return CallAttemptStatus.DO_NOT_CALL
    # Phase 12. A machine answered: the bot decided so and hung up, or left a
    # message. Nothing a recording said can be a callback request or a no, so
    # this outranks the state path — but never a do-not-call, which is a
    # promise the system keeps whatever else it believes about the call.
    if voicemail_detected(outcome):
        return CallAttemptStatus.VOICEMAIL
    for state in reversed(path):
        if state is ConversationState.ENDING:
            continue
        return status_for_final_state(state)
    return None


def voicemail_detected(outcome: Any) -> bool:
    """Whether a conversation outcome records that an answering machine picked up. Phase 12."""
    if not isinstance(outcome, dict):
        return False
    verdict = outcome.get("voicemail")
    return isinstance(verdict, dict) and verdict.get("detected") is True


def build_conversation_result(
    attempt: CallAttempt,
    outcome: Any,
    *,
    call_status: CallAttemptStatus | None = None,
) -> CallResult:
    """Build the result of a call the agent held, from the conversation's outcome.

    Tolerant by design: the outcome crosses a package boundary as plain data and
    may be a record this version does not recognise — an older bot, a row
    edited by hand, a bug. Anything that cannot be read is left unknown and
    named in `issues`; nothing here raises on the shape of the data, because
    the call is already over and a lost result is worse than a partial one.
    What comes out still has to pass `validate_call_result`, and a result built
    here always does — the builder derives the same things the validator checks.

    Args:
        attempt: The attempt row, for the ids and the carrier's failure reason.
        outcome: `SalesConversation.outcome()`, or whatever was stored as
            `conversation_data`.
        call_status: The attempt's final status, when the caller has decided
            it. Defaults to what the final state implies, else the attempt's
            own status if final, else `COMPLETED`.
    """
    issues: list[str] = []
    data = _record(outcome, issues, "the outcome")
    qualification = _record(data.get("qualification"), issues, "qualification")
    if "qualification" not in data:
        issues.append("no qualification record; every qualification field is unknown")

    final_state = _enum_or_none(ConversationState, data.get("final_state"))
    if data.get("final_state") is not None and final_state is None:
        issues.append(f"final_state {data.get('final_state')!r} is not a conversation state")
    if call_status is None:
        call_status = attempt_status_for(data) or (
            attempt.status if attempt.status.is_final else CallAttemptStatus.COMPLETED
        )
    zone_name, zone = _zone(data.get("timezone"), issues)

    interest = _enum(InterestLevel, qualification.get("interest_level"), issues, "interest_level")
    timeline = _enum(BuyingTimeline, qualification.get("buying_timeline"), issues, "buying_timeline")
    role = _enum(DecisionRole, qualification.get("decision_role"), issues, "decision_role")
    next_action = _enum(NextAction, qualification.get("next_action"), issues, "next_action")
    meeting_intent = _enum(Intent, qualification.get("meeting_intent"), issues, "meeting_intent")
    callback_intent = _enum(Intent, qualification.get("callback_intent"), issues, "callback_intent")

    pain_points = _strings(qualification.get("pain_points"), issues, "pain_points")
    notes = _strings(qualification.get("notes"), issues, "notes")
    objections = _objections(qualification.get("objections"), issues)

    meeting_booked = _flag(qualification.get("meeting_booked"), issues, "meeting_booked") is True
    meeting_start = _when(qualification.get("meeting_start"), zone, issues, "meeting_start")
    callback_raw = _text(qualification.get("callback_scheduled_for"))
    callback_scheduled_for = _when(callback_raw, zone, issues, "callback_scheduled_for")
    human_requested = _flag(qualification.get("human_requested"), issues, "human_requested")
    transferred = _flag(qualification.get("transferred"), issues, "transferred")

    meeting_status = (
        MeetingOutcome.BOOKED
        if meeting_booked
        else {
            Intent.REQUESTED: MeetingOutcome.PROPOSED,
            Intent.ACCEPTED: MeetingOutcome.AGREED,
            Intent.DECLINED: MeetingOutcome.DECLINED,
        }.get(meeting_intent, MeetingOutcome.UNKNOWN)
    )
    # A scheduled callback is one with a time the queue can dial at. A record
    # that claims one but carries no readable time is a request, not a
    # schedule, and the row must not say otherwise.
    if callback_scheduled_for is not None:
        callback_status = CallbackOutcome.SCHEDULED
    elif callback_raw:
        issues.append("callback_scheduled_for could not be read as a time; recorded as REQUESTED, not SCHEDULED")
        callback_status = CallbackOutcome.REQUESTED
    else:
        callback_status = {
            Intent.REQUESTED: CallbackOutcome.PROPOSED,
            Intent.ACCEPTED: CallbackOutcome.REQUESTED,
            Intent.DECLINED: CallbackOutcome.DECLINED,
        }.get(callback_intent, CallbackOutcome.UNKNOWN)

    # Two normalisations, and both restate the state rather than infer beyond
    # it. A call whose state is DO_NOT_CALL *is* a do-not-contact, whatever a
    # later tool wrote into `next_action` ("let me speak to a person" after
    # "never call me again" leaves HUMAN_FOLLOW_UP there) — and a rep who reads
    # the next action alone must not phone them. A call that ended in CALLBACK
    # got there only through a recorded request.
    if (final_state is ConversationState.DO_NOT_CALL or call_status is CallAttemptStatus.DO_NOT_CALL) and (
        next_action is not NextAction.DO_NOT_CONTACT
    ):
        issues.append(
            f"next_action was {next_action.value} on a do-not-call; recorded as DO_NOT_CONTACT"
        )
        next_action = NextAction.DO_NOT_CONTACT
    if (final_state is ConversationState.CALLBACK or call_status is CallAttemptStatus.CALLBACK_REQUESTED) and (
        callback_status in (CallbackOutcome.UNKNOWN, CallbackOutcome.PROPOSED)
    ):
        issues.append("the call ended in a callback request with no callback intent recorded; recorded as REQUESTED")
        callback_status = CallbackOutcome.REQUESTED

    # Derived, never read: the same rule the live call applies, over the same
    # fields. A record claiming something else is reported and overruled.
    derived = _derive_qualification(interest, pain_points, role, timeline, next_action)
    claimed = _enum_or_none(QualificationStatus, qualification.get("qualification_status"))
    if qualification.get("qualification_status") is not None and claimed is None:
        issues.append(f"qualification_status {qualification.get('qualification_status')!r} is not a known value")
    elif claimed is not None and claimed is not derived:
        issues.append(
            f"qualification_status was recorded as {claimed.value} but the evidence supports "
            f"{derived.value}; using the derived value"
        )

    transcript = _transcript(data.get("transcript"), issues)
    actions = _actions(data.get("actions"), issues)
    caller_turns = _count(data.get("user_turns"), issues, "user_turns")
    agent_turns = _count(data.get("agent_turns"), issues, "agent_turns")

    # Phase 12. A machine answered, so nothing the record claims about the
    # "prospect" was said by a person: every qualification field goes back to
    # unknown, and the transcript stays as the evidence. A model that recorded
    # a pain point from a voicemail greeting is reported, not believed.
    voicemail = call_status is CallAttemptStatus.VOICEMAIL
    if voicemail:
        claimed = [
            name
            for name, value in (
                ("interest_level", interest),
                ("buying_timeline", timeline),
                ("decision_role", role),
                ("next_action", next_action),
            )
            if value is not value.__class__.UNKNOWN
        ]
        if pain_points:
            claimed.append("pain_points")
        if objections:
            claimed.append("objections")
        if claimed:
            issues.append(
                "an answering machine answered; " + ", ".join(claimed) + " were recorded from a "
                "recording and have been left unknown"
            )
        interest, timeline, role = InterestLevel.UNKNOWN, BuyingTimeline.UNKNOWN, DecisionRole.UNKNOWN
        next_action = NextAction.UNKNOWN
        meeting_status, callback_status = MeetingOutcome.UNKNOWN, CallbackOutcome.UNKNOWN
        meeting_start = callback_scheduled_for = None
        pain_points, objections = (), ()
        human_requested = transferred = None
        # Derived again over the emptied fields, so the row cannot claim a
        # qualification the recording supposedly established.
        derived = _derive_qualification(interest, pain_points, role, timeline, next_action)
    # A transcript that was there can stand in for a missing count. No
    # transcript and no count is unknown — not zero, which would make the
    # summary say the person never spoke on the strength of nothing.
    if isinstance(data.get("transcript"), list):
        if caller_turns is None:
            caller_turns = sum(1 for entry in transcript if entry["role"] == ROLE_USER)
        if agent_turns is None:
            agent_turns = sum(1 for entry in transcript if entry["role"] == ROLE_ASSISTANT)
    duration = _seconds(data.get("call_duration_secs"), issues, "call_duration_secs")
    if duration is None:
        duration = _seconds(data.get("duration_secs"), issues, "duration_secs")

    result = CallResult(
        call_attempt_id=attempt.id,
        prospect_id=attempt.prospect_id,
        campaign_id=attempt.campaign_id,
        source=ResultSource.CONVERSATION,
        call_status=call_status,
        disposition=Disposition.COMPLETED,  # replaced below
        duration_seconds=duration,
        qualification_status=derived,
        interest_level=interest,
        buying_timeline=timeline,
        decision_role=role,
        next_action=next_action,
        meeting_status=meeting_status,
        meeting_start=meeting_start,
        meeting_reference=_text(qualification.get("meeting_reference")),
        meeting_when=_text(qualification.get("meeting_when")),
        callback_status=callback_status,
        callback_scheduled_for=callback_scheduled_for,
        callback_when=_text(qualification.get("callback_when")),
        pain_points=pain_points,
        objections=objections,
        questions=() if voicemail else extract_questions(transcript),
        existing_provider=None if voicemail else _text(qualification.get("existing_provider")),
        current_process=None if voicemail else _text(qualification.get("current_process")),
        impact=None if voicemail else _text(qualification.get("impact")),
        desired_outcome=None if voicemail else _text(qualification.get("desired_outcome")),
        notes=notes,
        human_requested=human_requested,
        transferred=transferred,
        agent_ended_call=_flag(data.get("agent_ended_call"), issues, "agent_ended_call"),
        caller_turns=caller_turns,
        agent_turns=agent_turns,
        final_state=final_state.value if final_state else None,
        timezone=zone_name,
        transcript=transcript,
        tool_actions=actions,
        issues=tuple(issues),
        failure_reason=_text(attempt.failure_reason),
    )
    result = replace(result, disposition=_disposition_of(result))
    return replace(result, summary=_summarize(result, _state_path(data)))


def build_carrier_result(attempt: CallAttempt) -> CallResult:
    """Build the result of a call from the carrier's report alone.

    For the attempts that never became a conversation — nobody answered, the
    line was busy, the dial failed — and, as a fallback, for a completed call
    whose bot wrote nothing. Everything the carrier cannot know is left unknown,
    and the summary says why.

    Raises:
        ValueError: The attempt is not in a final status; there is no result yet.
    """
    status = attempt.status
    if not status.is_final:
        raise ValueError(f"attempt {attempt.id} is {status.value}, which is not a final status")

    # The three conversation statuses restated, should a caller record one of
    # them through this path: the status *is* the fact, so the field that
    # carries it in a conversation result carries it here too.
    interest = InterestLevel.NOT_INTERESTED if status is CallAttemptStatus.NOT_INTERESTED else InterestLevel.UNKNOWN
    next_action = NextAction.UNKNOWN
    callback_status = CallbackOutcome.UNKNOWN
    if status is CallAttemptStatus.DO_NOT_CALL:
        next_action = NextAction.DO_NOT_CONTACT
    elif status is CallAttemptStatus.CALLBACK_REQUESTED:
        next_action = NextAction.CALLBACK_REQUESTED
        callback_status = CallbackOutcome.REQUESTED

    result = CallResult(
        call_attempt_id=attempt.id,
        prospect_id=attempt.prospect_id,
        campaign_id=attempt.campaign_id,
        source=ResultSource.CARRIER,
        call_status=status,
        disposition=Disposition.COMPLETED,  # replaced below
        duration_seconds=attempt.duration_seconds,
        qualification_status=_derive_qualification(interest, (), DecisionRole.UNKNOWN, BuyingTimeline.UNKNOWN, next_action),
        interest_level=interest,
        next_action=next_action,
        callback_status=callback_status,
        failure_reason=_text(attempt.failure_reason),
    )
    result = replace(result, disposition=_disposition_of(result))
    return replace(result, summary=_summarize(result, []))


def derive_disposition(
    call_status: CallAttemptStatus,
    *,
    final_state: ConversationState | None = None,
    next_action: NextAction = NextAction.UNKNOWN,
    interest_level: InterestLevel = InterestLevel.UNKNOWN,
    qualification_status: QualificationStatus = QualificationStatus.UNKNOWN,
    meeting_status: MeetingOutcome = MeetingOutcome.UNKNOWN,
    callback_status: CallbackOutcome = CallbackOutcome.UNKNOWN,
    transferred: bool | None = None,
) -> Disposition:
    """The one-word outcome the evidence supports, by precedence.

    The order is the order a CRM user would want to be told about: a
    do-not-call outranks everything, a booked meeting outranks a callback, a
    callback outranks a no (they can both be true — "not now, ring me in
    March"), and qualification only decides between the rows that have no
    firmer outcome. Every branch needs a *recorded* fact: unknown interest is
    not a no, and an agreed meeting that was never booked is not a booking.
    """
    if not call_status.reached_person:
        if call_status is CallAttemptStatus.NO_ANSWER:
            return Disposition.NO_ANSWER
        if call_status is CallAttemptStatus.BUSY:
            return Disposition.BUSY
        if call_status is CallAttemptStatus.VOICEMAIL:
            return Disposition.VOICEMAIL
        return Disposition.FAILED
    heard = final_state is ConversationState.DO_NOT_CALL or (
        # A conversation result (it has a final state) that ended DO_NOT_CALL:
        # the request was heard and then the agent said goodbye, so the final
        # state is ENDING while the status carries the request.
        final_state is not None and call_status is CallAttemptStatus.DO_NOT_CALL
    )
    if heard:
        # Phase 19: the conversation heard the request. Told, not known.
        return Disposition.OPTED_OUT
    if call_status is CallAttemptStatus.DO_NOT_CALL or next_action is NextAction.DO_NOT_CONTACT:
        # No conversation behind it: the list refused the dial, or a carrier
        # result restated the status. Known, not told.
        return Disposition.DO_NOT_CALL
    if meeting_status is MeetingOutcome.BOOKED:
        return Disposition.MEETING_BOOKED
    if transferred is True:
        return Disposition.TRANSFERRED
    if callback_status in (CallbackOutcome.REQUESTED, CallbackOutcome.SCHEDULED):
        return Disposition.CALLBACK_REQUESTED
    # A recorded no: the interest the model recorded, the state that only a
    # recorded no reaches, or the attempt status the sink wrote from it.
    if (
        interest_level is InterestLevel.NOT_INTERESTED
        or final_state is ConversationState.NOT_INTERESTED
        or call_status is CallAttemptStatus.NOT_INTERESTED
    ):
        return Disposition.NOT_INTERESTED
    if qualification_status is QualificationStatus.QUALIFIED:
        return Disposition.QUALIFIED
    if qualification_status is QualificationStatus.DISQUALIFIED:
        return Disposition.UNQUALIFIED
    return Disposition.COMPLETED


def _disposition_of(result: CallResult) -> Disposition:
    """`derive_disposition` over a result's own fields."""
    return derive_disposition(
        result.call_status,
        final_state=_enum_or_none(ConversationState, result.final_state),
        next_action=result.next_action,
        interest_level=result.interest_level,
        qualification_status=result.qualification_status,
        meeting_status=result.meeting_status,
        callback_status=result.callback_status,
        transferred=result.transferred,
    )


def _derive_qualification(
    interest: InterestLevel,
    pain_points: tuple[str, ...] | list[str],
    role: DecisionRole,
    timeline: BuyingTimeline,
    next_action: NextAction,
) -> QualificationStatus:
    """The live call's rule, applied to a result's fields. One definition of "qualified"."""
    record = QualificationRecord(
        interest_level=interest,
        pain_points=list(pain_points),
        buying_timeline=timeline,
        decision_role=role,
        next_action=next_action,
    )
    return record.qualification_status


# --- Validation ---------------------------------------------------------------


def validate_call_result(result: Any) -> list[str]:
    """Every reason this result must not be stored. Empty means it may be.

    The store calls this before every write. The checks are the three rules in
    the module docstring made concrete: the disposition and the qualification
    must be the ones the evidence derives; a call nobody answered cannot carry
    anything only a conversation could know; and every disposition with a
    consequence must have the recorded fact behind it.
    """
    if not isinstance(result, CallResult):
        return [f"expected a CallResult, got {type(result).__name__}"]

    problems: list[str] = []
    if result.schema_version != SCHEMA_VERSION:
        problems.append(f"schema_version {result.schema_version} is not {SCHEMA_VERSION}")
    if not isinstance(result.call_attempt_id, int) or result.call_attempt_id <= 0:
        problems.append("call_attempt_id must be a positive integer")
    if not isinstance(result.prospect_id, int) or result.prospect_id <= 0:
        problems.append("prospect_id must be a positive integer")
    if not isinstance(result.source, ResultSource):
        problems.append("source must be a ResultSource")
    if not isinstance(result.call_status, CallAttemptStatus):
        return [*problems, "call_status must be a CallAttemptStatus"]
    if not result.call_status.is_final:
        problems.append(f"call_status {result.call_status.value} is not final; there is no result yet")
    for name, enum in _ENUM_FIELDS:
        if not isinstance(getattr(result, name), enum):
            problems.append(f"{name} must be a {enum.__name__}")
    if problems:
        return problems

    expected = _disposition_of(result)
    if result.disposition is not expected:
        problems.append(
            f"disposition {result.disposition.value} does not follow from the record, "
            f"which supports {expected.value}"
        )
    derived = _derive_qualification(
        result.interest_level, result.pain_points, result.decision_role, result.buying_timeline, result.next_action
    )
    if result.qualification_status is not derived:
        problems.append(
            f"qualification_status {result.qualification_status.value} is not supported by the evidence "
            f"(interest={result.interest_level.value}, pain points={len(result.pain_points)}, "
            f"decision={result.decision_role.value}); the evidence supports {derived.value}"
        )

    if not result.reached:
        if result.disposition.reached:
            problems.append(f"disposition {result.disposition.value} on a call nobody answered")
        for name, enum in _UNKNOWABLE_FIELDS:
            if getattr(result, name) is not enum.UNKNOWN:
                problems.append(f"{name} is {getattr(result, name).value} on a call nobody answered; it cannot be known")
        # Phase 12: a voicemail is a call the bot *held* — it heard the
        # greeting, transcribed it and hung up — so the transcript, the
        # notes, the tool log and who ended it are real evidence and may
        # stay. What a recording cannot supply is anything about a person.
        voicemail = result.call_status is CallAttemptStatus.VOICEMAIL
        empty = ("pain_points", "objections", "questions") if voicemail else (
            "pain_points", "objections", "questions", "notes", "transcript", "tool_actions"
        )
        for name in empty:
            if getattr(result, name):
                problems.append(f"{name} is not empty on a call nobody answered")
        unknown = ("human_requested", "transferred") if voicemail else (
            "human_requested", "transferred", "agent_ended_call"
        )
        for name in unknown:
            if getattr(result, name) is not None:
                problems.append(f"{name} is {getattr(result, name)!r} on a call nobody answered; unknown is not false")
    elif not result.disposition.reached:
        problems.append(f"disposition {result.disposition.value} on a call that was answered")

    if result.disposition is Disposition.NOT_INTERESTED and not (
        result.interest_level is InterestLevel.NOT_INTERESTED
        or result.final_state == ConversationState.NOT_INTERESTED.value
        or result.call_status is CallAttemptStatus.NOT_INTERESTED
    ):
        problems.append("disposition NOT_INTERESTED without a recorded no; unknown is not not-interested")
    if result.disposition in (Disposition.DO_NOT_CALL, Disposition.OPTED_OUT) and result.next_action is not NextAction.DO_NOT_CONTACT:
        problems.append(f"disposition {result.disposition.value} but next_action is not DO_NOT_CONTACT")
    if result.disposition is Disposition.OPTED_OUT and not (
        result.final_state == ConversationState.DO_NOT_CALL.value
        or (result.final_state is not None and result.call_status is CallAttemptStatus.DO_NOT_CALL)
    ):
        problems.append("disposition OPTED_OUT without the conversation having heard the request")
    if result.disposition is Disposition.MEETING_BOOKED and result.meeting_status is not MeetingOutcome.BOOKED:
        problems.append("disposition MEETING_BOOKED but meeting_status is not BOOKED")
    if result.disposition is Disposition.CALLBACK_REQUESTED and result.callback_status not in (
        CallbackOutcome.REQUESTED,
        CallbackOutcome.SCHEDULED,
    ):
        problems.append("disposition CALLBACK_REQUESTED but no callback was requested or scheduled")
    if result.disposition is Disposition.TRANSFERRED and result.transferred is not True:
        problems.append("disposition TRANSFERRED but the record does not say the call was transferred")
    if result.disposition is Disposition.QUALIFIED and result.qualification_status is not QualificationStatus.QUALIFIED:
        problems.append("disposition QUALIFIED but qualification_status is not QUALIFIED")
    if result.disposition is Disposition.UNQUALIFIED and result.qualification_status is not QualificationStatus.DISQUALIFIED:
        problems.append("disposition UNQUALIFIED but qualification_status is not DISQUALIFIED")
    if result.callback_status is CallbackOutcome.SCHEDULED and result.callback_scheduled_for is None:
        problems.append("callback_status SCHEDULED without a scheduled time")

    for name in ("duration_seconds", "caller_turns", "agent_turns"):
        value = getattr(result, name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            problems.append(f"{name} must be a non-negative integer or None, not {value!r}")
    for name in ("meeting_start", "callback_scheduled_for"):
        value = getattr(result, name)
        if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
            problems.append(f"{name} must be a timezone-aware datetime or None")
    for name in ("human_requested", "transferred", "agent_ended_call"):
        value = getattr(result, name)
        if value is not None and not isinstance(value, bool):
            problems.append(f"{name} must be True, False or None (unknown), not {value!r}")

    summary = result.summary
    if not isinstance(summary, CallSummary):
        problems.append("summary must be a CallSummary")
    else:
        for name in ("what_happened", "prospect_needs", "objections", "interest", "qualification", "next_step"):
            if not isinstance(getattr(summary, name), str) or not getattr(summary, name).strip():
                problems.append(f"summary.{name} is empty")

    for index, entry in enumerate(result.transcript):
        if not isinstance(entry, dict) or entry.get("role") not in (ROLE_USER, ROLE_ASSISTANT):
            problems.append(f"transcript entry {index} has no valid role")
        elif not isinstance(entry.get("text"), str) or not entry["text"].strip():
            problems.append(f"transcript entry {index} has no text")
    for index, objection in enumerate(result.objections):
        if not isinstance(objection, dict) or not isinstance(objection.get("kind"), str):
            problems.append(f"objection {index} has no kind")
    return problems


# The fields that can only be known from a conversation, each with an UNKNOWN
# member. On a call nobody answered every one of them must still be UNKNOWN.
_UNKNOWABLE_FIELDS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("qualification_status", QualificationStatus),
    ("interest_level", InterestLevel),
    ("buying_timeline", BuyingTimeline),
    ("decision_role", DecisionRole),
    ("next_action", NextAction),
    ("meeting_status", MeetingOutcome),
    ("callback_status", CallbackOutcome),
)

_ENUM_FIELDS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("disposition", Disposition),
    *_UNKNOWABLE_FIELDS,
)


# --- The prospect's questions ---------------------------------------------------

# Words that open a question when there is no question mark to go by — a
# transcript from a phone line often has none. Only the prospect's turns are
# read, and only whole sentences are kept, verbatim.
_INTERROGATIVES = frozenset(
    {
        "what", "whats", "how", "hows", "why", "when", "where", "wheres", "who", "whos", "whose", "which",
        "can", "could", "would", "will", "shall", "should", "do", "does", "did", "is", "are", "am",
        "was", "were", "have", "has", "had", "any",
    }
)
_SENTENCES = re.compile(r"[^.?!]+[.?!]*")
_MAX_QUESTIONS = 25


def extract_questions(transcript: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    """The questions the prospect asked, verbatim, from their transcript turns.

    A filter, not an interpretation: a sentence is kept when it ends in a
    question mark or opens with an interrogative, and it is kept exactly as
    transcribed. The heuristic will miss a question phrased as a statement and
    keep the odd "do it by hand at the moment", and both are the right way to
    fail — a reader can see the transcript, and nothing here rewrote it.
    """
    questions: list[str] = []
    seen: set[str] = set()
    for entry in transcript:
        if not isinstance(entry, dict) or entry.get("role") != ROLE_USER:
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            continue
        for match in _SENTENCES.finditer(text):
            sentence = match.group(0).strip()
            if not sentence or not _is_question(sentence):
                continue
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            questions.append(sentence)
            if len(questions) >= _MAX_QUESTIONS:
                return tuple(questions)
    return tuple(questions)


def _is_question(sentence: str) -> bool:
    """A question mark on more than one word, or an interrogative opening a sentence.

    "Hello?" and "Sorry?" are a question mark on a single word and not a
    question anybody needs answered; "Can't say I'm keen." opens with a
    contraction of an interrogative and is a statement, which is why the
    negative contractions are not in the list.
    """
    words = re.sub(r"[^a-z0-9\s]", "", sentence.lower()).split()
    if sentence.endswith("?"):
        return len(words) >= 2
    return len(words) >= 3 and words[0] in _INTERROGATIVES


# --- The summary --------------------------------------------------------------

_STAGE_NAMES = {
    ConversationState.GREETING: "the opening",
    ConversationState.DISCOVERY: "discovery",
    ConversationState.QUALIFICATION: "qualification",
    ConversationState.VALUE_PROPOSITION: "the value proposition",
    ConversationState.OBJECTION_HANDLING: "objection handling",
    ConversationState.MEETING_REQUEST: "the meeting request",
    ConversationState.CALLBACK: "a callback request",
    ConversationState.NOT_INTERESTED: "a clear no",
    ConversationState.DO_NOT_CALL: "a do-not-call request",
    ConversationState.ENDING: "the goodbye",
}

_INTEREST_WORDS = {
    InterestLevel.INTERESTED: "actively interested",
    InterestLevel.CURIOUS: "listening but not committed",
    InterestLevel.NEUTRAL: "neutral",
    InterestLevel.RELUCTANT: "reluctant and wanting the call to end",
    InterestLevel.NOT_INTERESTED: "not interested; they said no",
}

_TIMELINE_WORDS = {
    BuyingTimeline.IMMEDIATE: "immediately",
    BuyingTimeline.THIS_QUARTER: "this quarter",
    BuyingTimeline.THIS_YEAR: "this year",
    BuyingTimeline.LATER: "later than this year",
    BuyingTimeline.NONE: "not at all; they said they will not act",
}

_ROLE_WORDS = {
    DecisionRole.DECISION_MAKER: "the decision maker",
    DecisionRole.INFLUENCER: "involved in the decision without owning it",
    DecisionRole.NOT_INVOLVED: "not involved in the decision",
}


def _summarize(result: CallResult, state_path: list[ConversationState]) -> CallSummary:
    """Compose the six-part summary from the result's fields and nothing else."""
    return CallSummary(
        what_happened=_what_happened(result, state_path),
        prospect_needs=_needs(result),
        objections=_objections_text(result),
        interest=_interest_text(result),
        qualification=_qualification_text(result),
        next_step=_next_step_text(result),
    )


def _what_happened(result: CallResult, state_path: list[ConversationState]) -> str:
    status = result.call_status
    if status is CallAttemptStatus.NO_ANSWER:
        return "The call was not answered."
    if status is CallAttemptStatus.BUSY:
        return "The line was busy."
    if status is CallAttemptStatus.FAILED:
        if not result.failure_reason:
            return "The call failed."
        return f"The call failed: {result.failure_reason.rstrip('.')}."
    if status is CallAttemptStatus.VOICEMAIL:
        return _voicemail_happened(result)

    if result.source is ResultSource.CARRIER:
        length = f" after {_duration_text(result.duration_seconds)}" if result.duration_seconds is not None else ""
        if status is CallAttemptStatus.COMPLETED:
            return f"The carrier reports the call completed{length}; the agent left no conversation record."
        return (
            f"The attempt was recorded as {status.value.lower().replace('_', ' ')}{length}, "
            f"with no conversation record from the agent."
        )

    parts = ["The call connected"]
    if result.duration_seconds is not None:
        parts.append(f" and lasted {_duration_text(result.duration_seconds)}")
    if result.caller_turns == 0:
        parts.append("; the other end did not speak")
    elif result.caller_turns is not None:
        parts.append(f"; the prospect spoke {result.caller_turns} time{'s' if result.caller_turns != 1 else ''}")

    final = _enum_or_none(ConversationState, result.final_state)
    furthest = next((state for state in reversed(state_path) if state is not ConversationState.ENDING), None)
    if final is ConversationState.ENDING and furthest is not None:
        parts.append(f"; the conversation got as far as {_STAGE_NAMES[furthest]} before the goodbye")
    elif final is not None:
        parts.append(f"; the conversation ended during {_STAGE_NAMES[final]}")

    if result.transferred is True:
        parts.append("; the call was transferred to a person")
    elif result.agent_ended_call is True:
        parts.append("; the agent ended the call")
    elif result.agent_ended_call is False:
        parts.append("; the line closed before the agent ended the call")
    return "".join(parts) + "."


def _voicemail_happened(result: CallResult) -> str:
    """Phase 12: what the row says when a machine picked up."""
    if result.source is ResultSource.CARRIER:
        length = f" after {_duration_text(result.duration_seconds)}" if result.duration_seconds is not None else ""
        return f"An answering machine picked up; the carrier reported it{length}. Nobody was reached."
    note = next((n for n in result.notes if n.lower().startswith("an answering machine")), None)
    if note:
        # The note already says how it was detected and what the agent did.
        return f"An answering machine picked up; nobody was reached. {note.rstrip('.')}."
    ending = (
        " The agent hung up." if result.agent_ended_call is True
        else " The line closed." if result.agent_ended_call is False
        else ""
    )
    return f"An answering machine picked up; nobody was reached.{ending}"


def _unreached_reason(result: CallResult) -> str:
    if result.call_status is CallAttemptStatus.VOICEMAIL:
        return "Not applicable: an answering machine picked up."
    return "Not applicable: nobody was reached."


def _needs(result: CallResult) -> str:
    if not result.reached:
        return _unreached_reason(result)
    fragments: list[str] = []
    if result.pain_points:
        fragments.append("Pain points: " + "; ".join(result.pain_points) + ".")
    if result.current_process:
        fragments.append(f"How they handle it today: {result.current_process}.")
    if result.impact:
        fragments.append(f"Impact: {result.impact}.")
    if result.desired_outcome:
        fragments.append(f"Desired outcome: {result.desired_outcome}.")
    if result.existing_provider:
        fragments.append(f"Existing provider: {result.existing_provider}.")
    if not fragments:
        return "No needs were established."
    return " ".join(fragments)


def _objections_text(result: CallResult) -> str:
    if not result.reached:
        return _unreached_reason(result)
    if not result.objections:
        return "No objections were recorded."
    rendered = []
    for objection in result.objections:
        kind = str(objection.get("kind", "OTHER")).replace("_", " ").lower()
        state = "handled" if objection.get("handled") else "open"
        detail = str(objection.get("detail") or "").strip()
        rendered.append(f"{kind} ({state})" + (f": {detail}" if detail else ""))
    return "Objections: " + "; ".join(rendered) + "."


def _interest_text(result: CallResult) -> str:
    if not result.reached:
        return _unreached_reason(result)
    words = _INTEREST_WORDS.get(result.interest_level)
    text = f"Interest: {words}." if words else "Interest level was not established."
    if result.human_requested is True:
        text += " They asked to speak to a person."
    return text


def _qualification_text(result: CallResult) -> str:
    if not result.reached:
        return _unreached_reason(result)
    status = result.qualification_status
    need = bool(result.pain_points)
    interest = result.interest_level in (InterestLevel.INTERESTED, InterestLevel.CURIOUS)
    authority = result.decision_role in (DecisionRole.DECISION_MAKER, DecisionRole.INFLUENCER)
    established = [name for name, met in (("a need", need), ("interest", interest), ("authority", authority)) if met]
    missing = [name for name, met in (("a need", need), ("interest", interest), ("authority", authority)) if not met]

    if status is QualificationStatus.QUALIFIED:
        text = "Qualified: a need, interest and decision-making authority were all established."
    elif status is QualificationStatus.PARTIALLY_QUALIFIED:
        text = f"Partially qualified: {_join(established)} established; {_join(missing)} not established."
    elif status is QualificationStatus.DISQUALIFIED:
        text = "Not qualified: " + _disqualified_reason(result) + "."
    else:
        text = "Qualification not established: the conversation did not cover need, interest or authority."

    extras = []
    if result.buying_timeline in _TIMELINE_WORDS:
        extras.append(f"Timeline: {_TIMELINE_WORDS[result.buying_timeline]}.")
    if result.decision_role in _ROLE_WORDS:
        extras.append(f"Decision role: {_ROLE_WORDS[result.decision_role]}.")
    return " ".join([text, *extras])


def _disqualified_reason(result: CallResult) -> str:
    if result.next_action is NextAction.DO_NOT_CONTACT:
        return "they asked not to be contacted"
    if result.interest_level is InterestLevel.NOT_INTERESTED:
        return "they said they are not interested"
    if result.buying_timeline is BuyingTimeline.NONE:
        return "they said they will not act"
    if result.decision_role is DecisionRole.NOT_INVOLVED and not result.pain_points:
        return "they are not involved in the decision and no need was established"
    return "the evidence rules them out"


def _next_step_text(result: CallResult) -> str:
    if not result.reached:
        if result.call_status is CallAttemptStatus.FAILED:
            return "Check the number before trying again; a failed dial is not retried automatically."
        if result.call_status is CallAttemptStatus.VOICEMAIL:
            return "Retry later, ideally at a different time of day; the campaign's retry policy applies."
        return "Retry later; the campaign's retry policy applies."

    zone = _zone_or_utc(result.timezone)
    primary: str | None = None
    if result.next_action is NextAction.DO_NOT_CONTACT:
        primary = "Do not contact them again."
    elif result.transferred is True:
        primary = "The call was transferred to a person, who now owns the follow-up."
    elif result.meeting_status is MeetingOutcome.BOOKED:
        when = _when_text(result.meeting_start, zone) if result.meeting_start else (result.meeting_when or "a time on record")
        reference = f" (reference {result.meeting_reference})" if result.meeting_reference else ""
        primary = f"Meeting booked for {when}{reference}."
    elif result.callback_status is CallbackOutcome.SCHEDULED:
        when = _when_text(result.callback_scheduled_for, zone) if result.callback_scheduled_for else (result.callback_when or "a time on record")
        primary = f"Callback scheduled for {when}."
    elif result.meeting_status is MeetingOutcome.AGREED:
        said = f" ({result.meeting_when})" if result.meeting_when else ""
        primary = f"They agreed to a meeting{said}, but nothing is booked; a person must arrange it."
    elif result.callback_status is CallbackOutcome.REQUESTED:
        said = f" ({result.callback_when})" if result.callback_when else ""
        primary = f"They asked to be called back{said}; no callback is scheduled, so a person must arrange it."
    elif result.meeting_status is MeetingOutcome.PROPOSED:
        said = f" ({result.meeting_when})" if result.meeting_when else ""
        primary = f"A meeting was proposed{said} and not yet agreed."
    elif result.next_action is NextAction.SEND_INFORMATION:
        primary = "Send them information."
    elif result.next_action is NextAction.HUMAN_FOLLOW_UP:
        primary = "A person should follow up; they asked to speak to someone."
    elif result.next_action is NextAction.NONE:
        primary = "No next step was agreed."
    else:
        primary = "No next step was established."

    secondary = []
    if result.next_action is NextAction.SEND_INFORMATION and "Send them information" not in primary:
        secondary.append("Also send them information.")
    if result.meeting_status is MeetingOutcome.DECLINED:
        secondary.append("They declined a meeting.")
    if result.callback_status is CallbackOutcome.DECLINED:
        secondary.append("They declined a callback.")
    return " ".join([primary, *secondary])


# --- Small readers, each tolerant and each honest about what it dropped -----


def _record(value: Any, issues: list[str], name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    issues.append(f"{name} was {type(value).__name__}, not a record; nothing could be read from it")
    return {}


def _enum(enum: type[Any], value: Any, issues: list[str], name: str) -> Any:
    """Read an enum field, leaving it UNKNOWN — and saying so — when it cannot be read."""
    if value is None:
        return enum.UNKNOWN
    member = _enum_or_none(enum, value)
    if member is None:
        issues.append(f"{name} {value!r} is not a known value; left UNKNOWN")
        return enum.UNKNOWN
    return member


def _enum_or_none(enum: type[Any], value: Any) -> Any:
    if isinstance(value, enum):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return enum(key)
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strings(value: Any, issues: list[str], name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        issues.append(f"{name} was {type(value).__name__}, not a list; left empty")
        return ()
    kept = []
    for item in value:
        text = _text(item) if isinstance(item, (str, int, float)) else None
        if text:
            kept.append(text)
        elif item not in (None, ""):
            issues.append(f"{name} contained a {type(item).__name__}; dropped")
    return tuple(kept)


def _objections(value: Any, issues: list[str]) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        issues.append(f"objections was {type(value).__name__}, not a list; left empty")
        return ()
    kept = []
    for item in value:
        if not isinstance(item, dict) or not _text(item.get("kind")):
            issues.append("an objection had no kind; dropped")
            continue
        kept.append(
            {
                "kind": str(item["kind"]).strip().upper(),
                "detail": _text(item.get("detail")) or "",
                "handled": item.get("handled") is True,
            }
        )
    return tuple(kept)


def _flag(value: Any, issues: list[str], name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    issues.append(f"{name} was {value!r}, not true or false; left unknown")
    return None


def _count(value: Any, issues: list[str], name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        issues.append(f"{name} was {value!r}, not a count; left unknown")
        return None
    return int(value)


def _seconds(value: Any, issues: list[str], name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        issues.append(f"{name} was {value!r}, not a duration; left unknown")
        return None
    return int(round(value))


def _when(value: Any, zone: tzinfo, issues: list[str], name: str) -> datetime | None:
    """Read an ISO 8601 time; a naive one is in the record's zone."""
    text = _text(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        issues.append(f"{name} {text!r} is not an ISO 8601 time; left unknown")
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def _zone(value: Any, issues: list[str]) -> tuple[str, tzinfo]:
    name = _text(value) or "UTC"
    if name.upper() == "UTC":
        return "UTC", UTC
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        issues.append(f"timezone {name!r} is not a known zone; times are read as UTC")
        return "UTC", UTC


def _zone_or_utc(name: str) -> tzinfo:
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _transcript(value: Any, issues: list[str]) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        issues.append(f"transcript was {type(value).__name__}, not a list; no transcript kept")
        return ()
    kept = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            issues.append(f"transcript entry {index} was {type(entry).__name__}, not a turn; dropped")
            continue
        role = entry.get("role")
        text = entry.get("text")
        if role not in (ROLE_USER, ROLE_ASSISTANT) or not isinstance(text, str) or not text.strip():
            issues.append(f"transcript entry {index} had no valid role or text; dropped")
            continue
        at = entry.get("at")
        kept.append(
            {
                "role": role,
                "text": text,  # Verbatim. Not even stripped.
                "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
                "interrupted": entry.get("interrupted") is True,
            }
        )
    return tuple(kept)


def _actions(value: Any, issues: list[str]) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        issues.append(f"actions was {type(value).__name__}, not a list; no tool actions kept")
        return ()
    kept = []
    for index, action in enumerate(value):
        if not isinstance(action, dict) or not _text(action.get("tool")):
            issues.append(f"action {index} had no tool name; dropped")
            continue
        kept.append({**action, "tool": str(action["tool"]).strip(), "success": action.get("success") is True})
    return tuple(kept)


def _state_path(data: dict[str, Any]) -> list[ConversationState]:
    path = data.get("state_path")
    if not isinstance(path, list):
        return []
    states = [_enum_or_none(ConversationState, name) for name in path]
    return [state for state in states if state is not None]


def _duration_text(seconds: int) -> str:
    minutes, rest = divmod(int(seconds), 60)
    if minutes and rest:
        return f"{minutes} min {rest} s"
    if minutes:
        return f"{minutes} min"
    return f"{rest} s"


def _when_text(moment: datetime, zone: tzinfo) -> str:
    local = moment.astimezone(zone)
    name = getattr(zone, "key", None) or "UTC"
    return f"{local:%a %d %b %Y %H:%M} {name}"


def _join(names: list[str]) -> str:
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def dumps(result: CallResult) -> str:
    """The result as a JSON document, for an export or a log line."""
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
