"""One cold call, as an object: where it is, what it has learned, what it does next.

`SalesConversation` is the seam between the model and everything else. The tools
in `tools.py` are thin wrappers around its methods; the director in `director.py`
asks it for the guidance to attach to each inference; `bot.py` asks it for the
system instruction at the start and for the outcome at the end. Nothing else in
the project imports it, and it imports nothing from the pipeline, the campaign
database, the calendar or the carrier.

**It holds six things and keeps them apart.**

* the state machine — where the call is (`states.py`)
* the qualification record — what the call learned (`qualification.py`)
* the brief — who is being called and on whose behalf (`brief.py`)
* the sink — what to do about it afterwards (`sink.py`)
* the action backend — what it can do *during* the call (`actions.py`)
* the transcript — what was actually said, verbatim (`transcript.py`, Phase 8)

None of the six is derived from any other, which is the property the phase's
architecture requirement asks for. The state can move without touching the
prospect data; the prospect data is read-only for the whole call; the
qualification record grows without the state moving; an action succeeding or
failing changes the record and the state only through the rules written here;
and the transcript is evidence that nothing else here edits or summarises.

**Two ways in, and both are needed.** Every method here is called either by a
tool the model chose to call, or by `note_user_turn` reacting to a deterministic
signal. The tools are the mechanism and the signals are the floor — see
`signals.py` for why do-not-call in particular cannot be left to the model
alone.

**The Phase 7 rule, applied here.** An action's success is decided by the
backend and nowhere else. Every method that acts — `check_availability`,
`book_meeting`, `schedule_callback`, `transfer_to_human` — validates what the
model asked for, asks the backend, and only on an `ok` outcome writes the field
that says it happened (`meeting_booked`, `callback_scheduled_for`,
`transferred`). On any other outcome it records the *intent* — they wanted a
meeting, they wanted a callback — and hands the model guidance that says,
plainly, that nothing happened. The record therefore never claims more than the
backend confirmed, and neither does the agent.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema

from .actions import ActionBackend, ActionOutcome, AttendeeDetails, Capabilities, NullActionBackend
from .brief import CallBrief
from .playbook import (
    CALLBACK_OVERRIDE,
    CALLBACK_UNSCHEDULED_OVERRIDE,
    DO_NOT_CALL_OVERRIDE,
    END_CALL_OVERRIDE,
    HUMAN_QUESTION_OVERRIDE,
    INSTRUCTION_PREFIX,
    INTERRUPTED_HOLD_OVERRIDE,
    INTERRUPTED_OVERRIDE,
    REJECTION_OVERRIDE,
    SEND_INFORMATION_OVERRIDE,
    TIME_MENTIONED_CALLBACK_ONLY_OVERRIDE,
    TIME_MENTIONED_OVERRIDE,
    TOOL_GUIDANCE,
    VAGUE_OVERRIDE,
    WANTS_HUMAN_OVERRIDE,
    WANTS_HUMAN_TRANSFER_OVERRIDE,
    build_system_instruction,
    email_heard_override,
    opening_instruction,
    phone_heard_override,
    stage_block,
)
from .qualification import (
    BuyingTimeline,
    DecisionRole,
    Intent,
    InterestLevel,
    NextAction,
    ObjectionKind,
    QualificationRecord,
    parse_enum,
    parse_objection_kind,
)
from .results import (
    EMAIL_REQUIRED,
    EXTERNAL_ERROR,
    INVALID_ARGUMENTS,
    INVALID_TIME,
    NOT_AUTHORIZED,
    PAST_TIME,
    SLOT_NOT_OFFERED,
    SLOT_TAKEN,
    TOO_FAR_AHEAD,
    TRANSFER_FAILED,
    TRANSFER_UNAVAILABLE,
    UNAVAILABLE,
    ToolResult,
)
from .signals import Signal, SignalReport, detect
from .sink import ConversationSink, LoggingSink
from .spoken_values import ensure_read_back, find_email, find_phone, normalize_email, speakable
from .states import ConversationState, ConversationStateMachine
from .timeparse import label, label_day, parse_clock, parse_day, parse_when, resolve_timezone
from .toolkit import AuditContext
from .transcript import Transcript

# Stages the model may ask for by name. Deliberately not every state: the model
# cannot declare a call `NOT_INTERESTED`, `DO_NOT_CALL` or `ENDING` through this
# route, because each of those has consequences and each has its own tool with
# its own recording. This one only moves the call along the selling path.
_REQUESTABLE = {
    "discovery": ConversationState.DISCOVERY,
    "qualification": ConversationState.QUALIFICATION,
    "value": ConversationState.VALUE_PROPOSITION,
    "value_proposition": ConversationState.VALUE_PROPOSITION,
    "meeting": ConversationState.MEETING_REQUEST,
    "meeting_request": ConversationState.MEETING_REQUEST,
    "greeting": ConversationState.GREETING,
}

# An interrupting turn that is nothing but a request to pause: "Wait.", "Hold on
# a second", "Sorry, one moment". Anything after it ("Hold on, where is your
# office?") makes it an ordinary interruption with a question in it.
_HOLD_ONLY = re.compile(
    r"^(?=.*\b(?:wait|stop|hold|hang|second|sec|moment|minute)\b)"
    r"(?:(?:sorry|okay|ok|hey|no|please|just|wait|stop|hold on|hang on|hold up|one (?:second|sec|moment|minute)"
    r"|a (?:second|sec|moment|minute)|give me|excuse me)\W*)+$",
    re.IGNORECASE,
)

# A callback or a booking asked for less than this far ahead is "now", and now
# is not a time anybody can be called back at.
_MIN_NOTICE = timedelta(minutes=1)


class SalesConversation:
    """The state of one sales call, and the only thing allowed to change it."""

    def __init__(
        self,
        brief: CallBrief,
        *,
        sink: ConversationSink | None = None,
        knowledge_base: bool = True,
        actions: ActionBackend | None = None,
        timezone: str | tzinfo = "UTC",
        now: datetime | Callable[[], datetime] | None = None,
        audit: AuditContext | None = None,
    ) -> None:
        """Create the conversation.

        Args:
            brief: Who is being called and on whose behalf. Read-only for the
                whole call.
            sink: Where consequences go after the call. Defaults to
                `LoggingSink`, which is the right answer for a browser session
                or an eval run.
            knowledge_base: Whether the retrieval stage is in the pipeline. Only
                affects the system instruction — see `build_system_instruction`.
            actions: What the call can do while it is happening. Defaults to
                `NullActionBackend`, on which every action fails plainly.
            timezone: The zone times are stated and asked for in. An IANA name
                or a `tzinfo`.
            now: The clock. A fixed `datetime` or a callable returning one, for
                tests; None uses the real clock. Always read through `_now()`.
            audit: Ids for the tool log. Defaults to an anonymous one.
        """
        self._brief = brief
        self._sink = sink or LoggingSink()
        self._tz = timezone if isinstance(timezone, tzinfo) else resolve_timezone(timezone)
        self._actions: ActionBackend = actions or NullActionBackend(_zone_name(self._tz))
        self._clock = now
        self._audit = audit or AuditContext(
            prospect_id=brief.prospect_id, call_attempt_id=brief.call_attempt_id
        )
        self._machine = ConversationStateMachine()
        self._record = QualificationRecord()
        self._transcript = Transcript()
        self._knowledge_base = knowledge_base
        self._started_at = time.monotonic()
        self._call_duration_secs: float | None = None
        # Phase 11: filled in at teardown from the pipeline's own metrics.
        self._usage: dict[str, Any] | None = None
        self._cost: dict[str, Any] | None = None
        # Phase 12: how the turns went, from `voice_quality.TurnMonitor`, and
        # whether a machine answered, from `voicemail.VoicemailDetector`. Both
        # plain data, for the same reason `_usage` is.
        self._quality: dict[str, Any] | None = None
        self._voicemail: dict[str, Any] | None = None
        self._user_turns = 0
        # Phase 31: handler-less schemas by name, built on first use.
        self._advertisable: dict[str, FunctionSchema] | None = None
        # Phase 32: what the model has said in the current response — the
        # session's `spoken_text.SpeechTally`, attached by `bot.py`. None (a
        # test, an older caller) means a recording tool never skips the second
        # request.
        self.speech: Any = None
        self._agent_turns = 0
        self._instructions: set[str] = set()
        self._pending_overrides: list[str] = []
        # What the detectors heard in the latest user turn, for the tools that
        # answer it: a no the model files as an objection is still a no.
        self._turn_signals: frozenset[Signal] = frozenset()
        # What the agent last said, which is where "what number can we reach
        # you on?" lives, and the first part of a number whose turn ended on a
        # pause before the number did. Both read by `_note_contact_details`.
        self._last_agent_text = ""
        # Whether the agent's latest reply was cut off by the caller. The next
        # caller turn is answered under `INTERRUPTED_OVERRIDE`, once.
        self._cut_off = False
        self._phone_pending = ""
        # A number or address the caller just dictated and has not heard back
        # yet. The model is asked to confirm it; `complete_reply` makes sure.
        self._read_back_owed = ""
        # Every distinct number and address given, in order — their own and a
        # colleague's both reach the notes; the record's fields hold the latest.
        self._contacts_given: list[tuple[str, str]] = []
        self._dnc_recorded = False
        self._dnc_stored = False
        self._finished = False
        self._end_requested = False
        # The caller explicitly asked to end the call (a detected END_CALL
        # signal). It lets the next goodbye hang up even if the model does not
        # call end_call itself — the same fallback the rejection path uses.
        self._caller_asked_to_end = False
        # The slots `check_calendar_availability` has returned this call, by
        # their start in UTC. `book_meeting` will only book one of these — the
        # rule that keeps the model from booking a time nobody offered.
        self._offered_slots: dict[datetime, dict[str, Any]] = {}

    # --- What the pipeline needs -------------------------------------------

    @property
    def brief(self) -> CallBrief:
        """Who is being called. Never changes during the call."""
        return self._brief

    @property
    def state(self) -> ConversationState:
        """The stage the call is in."""
        return self._machine.state

    @property
    def record(self) -> QualificationRecord:
        """What the call has established."""
        return self._record

    @property
    def machine(self) -> ConversationStateMachine:
        """The state machine, for the transition history."""
        return self._machine

    @property
    def transcript(self) -> Transcript:
        """What has been said so far, verbatim. Phase 8."""
        return self._transcript

    @property
    def capabilities(self) -> Capabilities:
        """What this session can do. Fixed for the call."""
        return self._actions.capabilities

    @property
    def audit(self) -> AuditContext:
        """Ids for the tool log, and the running list of actions taken."""
        return self._audit

    @property
    def timezone(self) -> tzinfo:
        """The zone the call's times are expressed in."""
        return self._tz

    @property
    def end_requested(self) -> bool:
        """Whether the agent has asked to hang up.

        `bot.py` reads this so a session that ends for another reason — the
        caller hung up, the line dropped — is not reported as an agent-initiated
        ending.
        """
        return self._end_requested

    @property
    def dnc_recorded(self) -> bool:
        """Whether a do-not-call has already fired its backend action this call."""
        return self._dnc_recorded

    @property
    def offered_slots(self) -> list[dict[str, Any]]:
        """Every slot the calendar has offered this call, in start order."""
        return [self._offered_slots[key] for key in sorted(self._offered_slots)]

    def system_instruction(self) -> str:
        """The system instruction for this call. Built once, never rewritten."""
        return build_system_instruction(
            self._brief,
            knowledge_base=self._knowledge_base,
            capabilities=self.capabilities,
            now=self._now(),
            tz=self._tz,
        )

    def opening(self) -> str:
        """The turn instruction that makes the agent speak first."""
        instruction = opening_instruction(self._brief)
        self._instructions.add(instruction)
        return instruction

    def is_own_instruction(self, text: str) -> bool:
        """Whether `text` is guidance this layer added rather than something said.

        Consulted by the knowledge retriever so it does not embed the agent's
        own stage directions and search the knowledge base for them. The prefix
        check is what covers the instructions built per call, which cannot be
        compared against a fixed set.
        """
        return text.startswith(INSTRUCTION_PREFIX) or text in self._instructions

    def guidance(self) -> str:
        """The block to append to this inference's copy of the context.

        Consumes any overrides raised by the last user turn: they apply to the
        reply to *that* turn and to nothing after it.
        """
        overrides = self._pending_overrides
        self._pending_overrides = []
        # The opening inference is the one before the prospect has said anything:
        # its guidance is "just greet", so it does not fight the opening
        # instruction and make the model front-load its introduction.
        opening = self.state is ConversationState.GREETING and self._user_turns == 0
        return stage_block(
            self.state,
            self._record,
            overrides=overrides,
            capabilities=self.capabilities,
            opening=opening,
        )

    # --- Reacting to the person on the phone -------------------------------

    async def note_user_turn(self, text: str) -> SignalReport:
        """Take in one thing the prospect said, before the agent replies to it.

        Runs the deterministic detectors and acts on the ones that must not wait
        for the model: a do-not-call request moves the state and fires the
        backend action here, in the same turn, rather than at the end of the
        call. Everything else becomes guidance placed in front of the model for
        its next reply.

        Returns:
            What was detected, for the caller to log.
        """
        self._user_turns += 1
        # The words the detectors run over are the words the transcript keeps,
        # so the record and the transcript cannot disagree about what was said.
        self._transcript.add_user(text)
        if self._cut_off:
            # State, not text: the reply before this turn was interrupted.
            self._cut_off = False
            self._pending_overrides.append(
                INTERRUPTED_HOLD_OVERRIDE if _HOLD_ONLY.match(text.strip()) else INTERRUPTED_OVERRIDE
            )
        self._read_back_owed = ""
        self._note_contact_details(text)
        report = detect(text)
        self._turn_signals = frozenset(report.matched)
        if not report:
            return report
        if report.forces_do_not_call or Signal.END_CALL in report:
            # The reply to this is an apology or a goodbye, and nothing else.
            self._read_back_owed = ""

        for signal in report.matched:
            override = self._override_for(signal)
            if override:
                self._pending_overrides.append(override)

        if Signal.WANTS_HUMAN in report:
            self._record.human_requested = True
            if self._record.next_action is not NextAction.TRANSFERRED:
                self._record.next_action = NextAction.HUMAN_FOLLOW_UP

        if Signal.END_CALL in report:
            # Honour it even if the model forgets end_call: the next non-question
            # goodbye then hangs up (`closing_line_needs_hangup`).
            self._caller_asked_to_end = True
            logger.info(f"SIGNAL | asked to end the call: {report.reason(Signal.END_CALL)!r}")

        # Heard, so recorded — the same way a do-not-call is. The tool the
        # model is then told to call merges into this entry rather than adding
        # a second one (`QualificationRecord.add_objection`).
        if Signal.SEND_INFORMATION in report and not self.state.is_rejection:
            phrase = report.reason(Signal.SEND_INFORMATION)
            logger.info(f"SIGNAL | asked to be sent information: {phrase!r}")
            self.record_objection(ObjectionKind.SEND_INFORMATION.value, f"said {phrase!r}")

        if report.forces_do_not_call:
            phrase = report.reason(Signal.DO_NOT_CALL)
            logger.info(f"SIGNAL | do-not-call phrase detected: {phrase!r}")
            await self.do_not_call(reason=f"said {phrase!r}", trigger="signal")

        return report

    def _note_contact_details(self, text: str) -> None:
        """Record a phone number or an email address the caller just dictated.

        The transcript keeps their words; the record gets the value a CRM can
        use (`spoken_values`), and the model is handed that value for its reply
        rather than left to count "double one" itself. Pure string work, tens
        of microseconds, so it adds nothing a caller could hear.
        """
        phone = find_phone(text, context=self._last_agent_text, pending=self._phone_pending)
        # The rest of a number split across two turns: keep both halves' words.
        continued = bool(phone and self._phone_pending and phone.value.startswith(self._phone_pending))
        # A turn can end on the pause between two groups, so a number that is
        # still short of a full mobile number may get its rest in the next turn.
        short = bool(phone) and (not phone.complete or len(phone.value.lstrip("+")) < 10)
        self._phone_pending = phone.value if phone and short else ""
        if phone:
            heard = phone.raw
            if continued and self._record.contact_phone_heard:
                heard = f"{self._record.contact_phone_heard} … {phone.raw}"
            self._record.contact_phone = phone.value
            self._record.contact_phone_heard = heard
            if continued and self._contacts_given and self._contacts_given[-1][0] == "phone number":
                self._contacts_given.pop()
            if phone.complete and ("phone number", phone.value) not in self._contacts_given:
                self._contacts_given.append(("phone number", phone.value))
            logger.info(f"CONTACT | phone {phone.value}{'' if phone.complete else ' (so far)'} <- {phone.raw!r}")
            self._pending_overrides.append(
                phone_heard_override(phone.value, speakable(phone.value), complete=phone.complete)
            )
            if phone.complete:
                self._read_back_owed = phone.value
        email = find_email(text, context=self._last_agent_text)
        if email:
            self._record.contact_email = email.value
            self._record.contact_email_heard = email.raw
            if ("email address", email.value) not in self._contacts_given:
                self._contacts_given.append(("email address", email.value))
            logger.info(f"CONTACT | email {email.value} <- {email.raw!r}")
            self._pending_overrides.append(email_heard_override(email.value, speakable(email.value)))
            self._read_back_owed = email.value

    def complete_reply(self, reply: str) -> str:
        """The reply the model wrote, with the read-back it owed if it left it out.

        Called by the spoken-text filter on the finished reply, before any of
        it reaches the voice. The model is asked to confirm a dictated number
        or address and mostly does (five live turns in six, 2026-09-18); the
        sixth is settled here, in code, rather than by a firmer instruction —
        firmer wording is what made the model narrate a tool call (Failed §53).
        A response with no words in it (a tool call on its own) leaves the
        read-back owed to the response that follows the tool's result.
        """
        value = self._read_back_owed
        if not value or not any(c.isalnum() for c in reply):
            return reply
        self._read_back_owed = ""
        completed = ensure_read_back(reply, value)
        if completed != reply:
            logger.info(f"CONTACT | reply adjusted so that {value} is read back, in words")
        return completed

    def _override_for(self, signal: Signal) -> str | None:
        """The override block a signal raises, given what this session can do.

        The one that varies is the request for a person: on a phone call with a
        transfer destination the agent offers to connect them; anywhere else it
        offers a callback, because offering a transfer it cannot make is a
        promise it would then break.
        """
        if signal is Signal.DO_NOT_CALL:
            return DO_NOT_CALL_OVERRIDE
        if signal is Signal.ASKED_IF_HUMAN:
            return HUMAN_QUESTION_OVERRIDE
        if signal is Signal.WANTS_HUMAN:
            return (
                WANTS_HUMAN_TRANSFER_OVERRIDE
                if self.capabilities.can_transfer
                else WANTS_HUMAN_OVERRIDE
            )
        if signal is Signal.REJECTION:
            return REJECTION_OVERRIDE
        if signal is Signal.END_CALL:
            return END_CALL_OVERRIDE
        if signal is Signal.SEND_INFORMATION and not self.state.is_rejection:
            return SEND_INFORMATION_OVERRIDE
        if signal is Signal.VAGUE and self.state.is_selling:
            return VAGUE_OVERRIDE
        if signal is Signal.CALLBACK and self.state is not ConversationState.CALLBACK:
            return (
                CALLBACK_OVERRIDE
                if self.capabilities.can_schedule_callback
                else CALLBACK_UNSCHEDULED_OVERRIDE
            )
        if signal is Signal.MENTIONED_TIME and self.state.is_selling and not self._record.meeting_booked:
            if self.capabilities.can_check_calendar:
                return TIME_MENTIONED_OVERRIDE
            if self.capabilities.can_schedule_callback:
                return TIME_MENTIONED_CALLBACK_ONLY_OVERRIDE
        return None

    def note_voicemail(self, verdict: dict[str, Any], action: str) -> None:
        """Record that an answering machine picked up, and what was done. Phase 12.

        A note on the record rather than a state: the state machine describes
        a conversation with a person, and there was none. The sink turns the
        verdict into the attempt's `VOICEMAIL` status through
        `results.attempt_status_for`, which reads it off the outcome.
        """
        self._voicemail = dict(verdict)
        method = str(verdict.get("method") or "unknown")
        evidence = str(verdict.get("evidence") or "")
        what = "hung up" if action == "hangup" else "left a message"
        self._record.add_note(
            f"An answering machine answered ({method}: {evidence}); the agent {what}."
        )
        logger.info(f"CONVERSATION | voicemail detected by {method}; {what}")

    def note_agent_turn(self, text: str = "", *, interrupted: bool = False) -> None:
        """Count one finished reply from the agent, and keep its words.

        Args:
            text: What the caller heard — the assistant aggregator's content,
                which on an interrupted reply is the fragment up to the cut.
                Empty text still counts as a turn but leaves no transcript
                entry: there is nothing the caller heard.
            interrupted: Whether the caller cut the reply off.
        """
        self._agent_turns += 1
        self._cut_off = interrupted
        self._last_agent_text = text or self._last_agent_text
        self._transcript.add_assistant(text, interrupted=interrupted)

    def closing_line_needs_hangup(self, text: str = "", *, interrupted: bool = False) -> bool:
        """Whether the reply just spoken closed a refused call that the model left open.

        After a no (`NOT_INTERESTED`, `DO_NOT_CALL`) the model is told to say
        goodbye and call `end_call` in the same turn. Observed 2026-09-10: the
        goodbye came every time and the tool call came in one run of three on
        the configured model and none of three on another — the caller was
        left holding a silent line until the idle timeout. So the hang-up is
        the conversation's decision, not the model's: `bot.py` ends the call
        when this is true, once the closing line has finished playing.

        It is false when the model did call `end_call` (nothing more to do),
        when the caller interrupted the line (they have something to say),
        when the reply asked them something (an answer is expected — the
        guidance says not to, but hanging up mid-question would be worse),
        and when nothing was said at all (there is no goodbye to have ended on).
        """
        if interrupted or self._end_requested:
            return False
        spoken = text.strip()
        if spoken.endswith("?"):
            # A question expects an answer; hanging up mid-question is worse.
            return False
        if self._caller_asked_to_end:
            # They explicitly asked to end. Honour it even if the goodbye came
            # out empty — a weak model that emitted a tool call as text, or said
            # nothing, must still not leave the line open (observed 2026-09-15).
            return True
        if self.state.is_rejection:
            # The rejection fallback still needs an actual spoken goodbye.
            return bool(spoken)
        return False

    # --- The operations the recording tools expose --------------------------

    def move_to(self, stage: str) -> tuple[bool, str]:
        """Move along the selling path, if the state machine allows it.

        Returns:
            `(moved, message)`. The message goes back to the model as the tool
            result, and says plainly when a move was refused — a model that has
            been told "no, they have said they are not interested" behaves
            better than one whose tool silently did nothing.
        """
        target = _REQUESTABLE.get(stage.strip().lower().replace(" ", "_"))
        if target is None:
            return False, f"unknown stage {stage!r}"

        # Asked for, then refused by the table — deliberately in that order,
        # rather than short-circuiting on `state.is_rejection`. The refusal is
        # worth recording: a call where the model repeatedly tried to go back to
        # pitching after a no is one where the prompt is not holding, and that is
        # invisible if we never ask.
        was_rejection = self.state.is_rejection
        moved = self._machine.transition(target, reason="model requested", trigger="move_to_stage")
        if moved:
            return True, "ok"
        if was_rejection and target.is_selling:
            return False, "they have said no; do not go back to selling"
        if target is self.state:
            return True, "already there"
        return False, f"cannot move from {self.state.value} to {target.value}"

    def record_discovery(
        self,
        *,
        pain_point: str | None = None,
        current_process: str | None = None,
        existing_provider: str | None = None,
        impact: str | None = None,
        desired_outcome: str | None = None,
        timeline: str | None = None,
        decision_role: str | None = None,
        note: str | None = None,
    ) -> list[str]:
        """Record what was learned. Every argument is optional and unknown stays unknown.

        Only fields actually supplied are written, and an unparseable enum value
        leaves its field alone rather than defaulting — see
        `qualification.parse_enum`.

        Returns:
            The names of the fields that changed, for the log.
        """
        changed: list[str] = []

        if self._record.add_pain_point(pain_point):
            changed.append("pain_point")
        if self._record.add_note(note):
            changed.append("note")

        for name, value in (
            ("current_process", current_process),
            ("existing_provider", existing_provider),
            ("impact", impact),
            ("desired_outcome", desired_outcome),
        ):
            cleaned = (value or "").strip()
            if cleaned and getattr(self._record, name) != cleaned:
                setattr(self._record, name, cleaned)
                changed.append(name)

        if timeline:
            parsed = parse_enum(BuyingTimeline, timeline, BuyingTimeline.UNKNOWN)
            if parsed is not BuyingTimeline.UNKNOWN and parsed is not self._record.buying_timeline:
                self._record.buying_timeline = parsed
                changed.append("buying_timeline")

        if decision_role:
            parsed_role = parse_enum(DecisionRole, decision_role, DecisionRole.UNKNOWN)
            if parsed_role is not DecisionRole.UNKNOWN and parsed_role is not self._record.decision_role:
                self._record.decision_role = parsed_role
                changed.append("decision_role")

        # Learning something about their situation *is* discovery, so a call
        # still nominally in GREETING catches up on its own. Nothing else here
        # moves the state: what stage to be in is the model's decision.
        if changed and self.state is ConversationState.GREETING:
            self._machine.transition(
                ConversationState.DISCOVERY, reason="learned something", trigger="record_discovery"
            )

        if changed:
            logger.info(f"QUALIFY | recorded {', '.join(changed)}")
        return changed

    def set_interest(self, level: str, reason: str = "") -> tuple[InterestLevel, bool]:
        """Record how interested they sound. Returns the level and whether it moved the state.

        `NOT_INTERESTED` is the one value with a consequence: it moves the call
        into the state of the same name, from which the transition table does
        not allow a route back to pitching.
        """
        parsed = parse_enum(InterestLevel, level, InterestLevel.UNKNOWN)
        if parsed is InterestLevel.UNKNOWN:
            return parsed, False

        self._record.interest_level = parsed
        self._record.add_note(reason)
        logger.info(f"QUALIFY | interest={parsed.value}{f' ({reason})' if reason else ''}")

        if parsed is InterestLevel.NOT_INTERESTED:
            self._record.add_objection(ObjectionKind.NOT_INTERESTED, reason)
            if self._record.next_action in (NextAction.UNKNOWN, NextAction.NONE):
                self._record.next_action = NextAction.NONE
            moved = self._machine.transition(
                ConversationState.NOT_INTERESTED, reason=reason or "said no", trigger="set_interest"
            )
            return parsed, moved

        return parsed, False

    def record_objection(self, kind: str, detail: str = "") -> ObjectionKind:
        """Record something they pushed back on, and move into objection handling."""
        parsed = parse_objection_kind(kind)

        # A clear no, filed as an objection. On 2026-09-11 the model answered
        # "we're really not interested in anything like that" with
        # record_objection(NOT_INTERESTED) instead of set_interest, said a polite
        # goodbye, and the call sat in objection handling with nothing to end
        # it. When the detector heard a rejection in the same turn and the
        # model classified it as NOT_INTERESTED, the two agree: treat it as the
        # no it is, so the guidance and the hang-up follow.
        if (
            parsed is ObjectionKind.NOT_INTERESTED
            and Signal.REJECTION in self._turn_signals
            and not self.state.is_rejection
        ):
            logger.info("OBJECTION | NOT_INTERESTED filed on a heard rejection: taken as the no")
            self.set_interest(InterestLevel.NOT_INTERESTED.value, detail)
            return parsed

        self._record.add_objection(parsed, detail)
        logger.info(f"OBJECTION | {parsed.value}{f': {detail}' if detail else ''}")

        if parsed is ObjectionKind.SEND_INFORMATION and self._record.next_action in (
            NextAction.UNKNOWN,
            NextAction.NONE,
        ):
            self._record.next_action = NextAction.SEND_INFORMATION
        if parsed is ObjectionKind.WANTS_HUMAN:
            self._record.human_requested = True
            if self._record.next_action is not NextAction.TRANSFERRED:
                self._record.next_action = NextAction.HUMAN_FOLLOW_UP

        # An objection raised after a rejection is not a detour back into
        # selling — the call stays where it is.
        if not self.state.is_rejection:
            self._machine.transition(
                ConversationState.OBJECTION_HANDLING,
                reason=parsed.value,
                trigger="record_objection",
            )
        return parsed

    def request_callback(self, when: str = "") -> bool:
        """Record that they want to be called another time. An intent, not a schedule.

        `schedule_callback` is what actually creates one; this is the part of it
        that is true whether or not the backend succeeded, and it is also the
        whole of what happens on a session with nowhere to schedule into.
        """
        self._record.callback_intent = Intent.ACCEPTED
        self._record.callback_when = when.strip() or None
        if self._record.next_action is not NextAction.TRANSFERRED:
            self._record.next_action = NextAction.CALLBACK_REQUESTED
        self._record.mark_objections_handled()
        logger.info(f"NEXT | callback requested{f' for {when!r}' if when else ''}")
        return self._machine.transition(
            ConversationState.CALLBACK, reason=when or "no time given", trigger="schedule_callback"
        )

    def request_meeting(self, when: str = "", note: str = "") -> bool:
        """Record that they are willing to take a next step. An intent, never a booking.

        Refused, and nothing recorded, after a clear no: the transition table
        forbids `MEETING_REQUEST` from a rejection state, and a record that said
        "meeting agreed" on a call where the person had said no would be worse
        than a missing one.

        Returns:
            Whether the state moved to `MEETING_REQUEST`.
        """
        if self.state.is_rejection:
            self._machine.transition(
                ConversationState.MEETING_REQUEST, reason="after a no", trigger="request_meeting"
            )
            return False

        self._record.meeting_intent = Intent.ACCEPTED
        self._record.meeting_when = when.strip() or None
        if not self._record.meeting_booked:
            self._record.next_action = NextAction.MEETING_REQUESTED
        self._record.add_note(note)
        self._record.mark_objections_handled()
        if self._record.interest_level in (InterestLevel.UNKNOWN, InterestLevel.NEUTRAL):
            self._record.interest_level = InterestLevel.INTERESTED
        logger.info(f"NEXT | meeting agreed{f' for {when!r}' if when else ''} (not booked)")
        return self._machine.transition(
            ConversationState.MEETING_REQUEST, reason=when or "no time given", trigger="request_meeting"
        )

    def decline_meeting(self, reason: str = "") -> None:
        """Record that a proposed next step was refused, without ending the call."""
        self._record.meeting_intent = Intent.DECLINED
        self._record.add_note(reason)

    async def do_not_call(self, *, reason: str = "", trigger: str = "tool") -> bool:
        """Honour a request never to be contacted again.

        The one operation that is both immediate and durable. It moves the state
        from wherever the call was, records the outcome, and fires the sink's
        backend action *now* rather than at the end of the call — because a call
        that drops thirty seconds later must still have honoured the request.

        Idempotent: a call where the prospect says it twice, or says it and the
        model also calls the tool, produces one backend action.

        Returns:
            Whether anything durable recorded it. A `LoggingSink` returns False
            and the call still behaves correctly for the rest of its length.
        """
        self._machine.force_do_not_call(reason=reason, trigger=trigger)
        self._record.next_action = NextAction.DO_NOT_CONTACT
        self._record.interest_level = InterestLevel.NOT_INTERESTED
        self._record.add_objection(ObjectionKind.NOT_INTERESTED, reason)
        self._offered_slots.clear()

        if self._dnc_recorded:
            return self._dnc_stored
        self._dnc_recorded = True
        self._dnc_stored = False

        try:
            stored = await self._sink.on_do_not_call(self._brief, reason or trigger)
        except Exception:  # noqa: BLE001 - a failing sink must not end the call
            logger.exception("DNC | the backend action failed; the agent will still honour it")
            return False
        self._dnc_stored = bool(stored)
        logger.info(f"DNC | recorded via {trigger} ({'stored' if stored else 'not stored'})")
        return self._dnc_stored

    def begin_ending(self, reason: str = "") -> bool:
        """Move the call into `ENDING`.

        Separate from actually hanging up: the pipeline ends when the goodbye
        has finished playing, which `tools.end_call` arranges by pushing an
        `EndWorkerFrame` downstream after the result. This only records that the
        call is closing.
        """
        self._end_requested = True
        return self._machine.transition(
            ConversationState.ENDING, reason=reason, trigger="end_call"
        )

    # --- The operations the action tools expose ------------------------------
    #
    # Each of these returns the `ToolResult` the model will read, because the
    # guidance it carries depends on the state — and the state is this class's
    # to know. The pattern is the same in every one: refuse what the state
    # forbids, refuse what does not parse, ask the backend, and write the
    # "it happened" field only on `ok`.

    async def search_knowledge(self, query: str) -> ToolResult:
        """Look something up through the retrieval path, on the model's request."""
        cleaned = (query or "").strip()
        if len(cleaned) < 2:
            return ToolResult.fail(
                INVALID_ARGUMENTS,
                "query must be a few words describing what to look up",
                guidance=TOOL_GUIDANCE["knowledge_unavailable"],
            )
        if not self.capabilities.can_search_knowledge:
            return ToolResult.fail(
                UNAVAILABLE,
                "no knowledge base is available on this call",
                guidance=TOOL_GUIDANCE["knowledge_unavailable"],
            )

        outcome = await self._call_backend(self._actions.search_knowledge(cleaned))
        if not outcome.ok:
            return _failed(outcome, EXTERNAL_ERROR, TOOL_GUIDANCE["knowledge_unavailable"])

        found = bool(outcome.data.get("found"))
        return ToolResult.ok(
            outcome.data,
            guidance=TOOL_GUIDANCE["knowledge_found" if found else "knowledge_none"],
        )

    async def check_availability(self, day_text: str, preferred_time_text: str = "") -> ToolResult:
        """Find open slots on a day, and remember them as the only bookable ones."""
        refused = self._refuse_if_rejected("offer a meeting")
        if refused:
            return refused

        day = parse_day(day_text)
        if day is None:
            return ToolResult.fail(
                INVALID_TIME,
                f"day must be a date written YYYY-MM-DD, for example {self._today():%Y-%m-%d}; got {day_text!r}",
                guidance=TOOL_GUIDANCE["invalid_time"],
            )
        today = self._today()
        if day < today:
            return ToolResult.fail(
                PAST_TIME,
                f"{label_day(day)} has already passed; today is {label_day(today)}",
                guidance=TOOL_GUIDANCE["invalid_time"],
            )
        if not self.capabilities.can_check_calendar:
            return ToolResult.fail(
                UNAVAILABLE,
                "no calendar is available on this call",
                guidance=TOOL_GUIDANCE["calendar_unavailable"],
            )

        preferred = parse_clock(preferred_time_text)
        outcome = await self._call_backend(self._actions.check_availability(day, preferred))
        if not outcome.ok:
            guidance = (
                TOOL_GUIDANCE["too_far_ahead"]
                if outcome.error_code == TOO_FAR_AHEAD
                else TOOL_GUIDANCE["calendar_unavailable"]
            )
            return _failed(outcome, EXTERNAL_ERROR, guidance)

        slots = [slot for slot in outcome.data.get("slots", []) if isinstance(slot, dict)]
        for slot in slots:
            start = parse_when(str(slot.get("start", "")), self._tz)
            if start is not None:
                self._offered_slots[start.astimezone(UTC)] = slot

        # Checking the calendar *is* asking for the meeting. The intent is at
        # least requested from here on, and the stage follows if the table
        # allows it — from a rejection it will not, and that is right.
        if self._record.meeting_intent is Intent.UNKNOWN:
            self._record.meeting_intent = Intent.REQUESTED
        self._machine.transition(
            ConversationState.MEETING_REQUEST,
            reason=f"checking {label_day(day)}",
            trigger="check_calendar_availability",
        )
        logger.info(f"CALENDAR | {len(slots)} slot(s) offered for {label_day(day)}")
        data = {**outcome.data, "slots": slots, "day": day.isoformat()}
        if not slots:
            return ToolResult.ok(data, guidance=TOOL_GUIDANCE["no_slots"])
        labels = [str(slot.get("label") or slot.get("start")) for slot in slots[:2]]
        offer = " or ".join(labels)
        return ToolResult.ok(data, guidance=TOOL_GUIDANCE["slots"].format(offer=offer))

    async def book_meeting(
        self, start_text: str, attendee_email: str = "", notes: str = ""
    ) -> ToolResult:
        """Book one of the offered slots. Claims a booking only when the backend does."""
        refused = self._refuse_if_rejected("book a meeting")
        if refused:
            return refused

        if not self.capabilities.can_book_meeting:
            # The intent is real even when the booking is impossible here.
            self.request_meeting(start_text, notes)
            return ToolResult.fail(
                UNAVAILABLE,
                "no calendar is available on this call; the request is recorded",
                guidance=TOOL_GUIDANCE["calendar_unavailable"],
            )

        start = parse_when(start_text, self._tz)
        if start is None:
            return ToolResult.fail(
                INVALID_TIME,
                f"start must be the exact start of an offered slot, written like"
                f" {self._now():%Y-%m-%dT%H:%M}; got {start_text!r}",
                guidance=TOOL_GUIDANCE["invalid_time"],
            )
        key = start.astimezone(UTC)
        slot = self._offered_slots.get(key)
        if slot is None:
            offered = ", ".join(s.get("label", s.get("start", "")) for s in self.offered_slots[:4])
            return ToolResult.fail(
                SLOT_NOT_OFFERED,
                f"{label(start)} is not one of the times check_calendar_availability returned"
                + (f" (offered: {offered})" if offered else " (nothing has been offered yet)"),
                guidance=TOOL_GUIDANCE["slot_not_offered"],
                data={"offered": self.offered_slots[:4]},
            )

        # The model may pass the address as it heard it ("john dot smith at gmail
        # dot com"); the calendar needs the address. What the caller dictated on
        # this call comes before the address on file.
        email = (
            normalize_email(attendee_email)
            or (attendee_email or "").strip()
            or self._record.contact_email
            or (self._brief.prospect.email or "").strip()
            or None
        )
        if self.capabilities.booking_requires_email and not email:
            return ToolResult.fail(
                EMAIL_REQUIRED,
                "the calendar needs the attendee's email address and none is known",
                guidance=TOOL_GUIDANCE["email_required"],
            )

        attendee = AttendeeDetails(
            name=" ".join(
                part for part in (self._brief.prospect.first_name, self._brief.prospect.last_name) if part
            )
            or "Prospect",
            email=email,
            phone=self._brief.prospect.phone,
        )
        outcome = await self._call_backend(
            self._actions.book_meeting(start, attendee, notes=(notes or "").strip())
        )

        # Whatever happened, they agreed to meet. Record that first, so a
        # backend that failed still leaves the intent for a person to act on.
        self._record.meeting_intent = Intent.ACCEPTED
        self._record.meeting_when = label(start)
        self._record.add_note(notes)
        self._record.mark_objections_handled()
        if self._record.interest_level in (InterestLevel.UNKNOWN, InterestLevel.NEUTRAL):
            self._record.interest_level = InterestLevel.INTERESTED
        self._machine.transition(
            ConversationState.MEETING_REQUEST, reason=label(start), trigger="book_meeting"
        )

        if not outcome.ok:
            if not self._record.meeting_booked:
                self._record.next_action = NextAction.MEETING_REQUESTED
            if outcome.error_code == SLOT_TAKEN:
                self._offered_slots.pop(key, None)
                logger.warning(f"CALENDAR | slot {label(start)} was taken before it could be booked")
                return _failed(outcome, SLOT_TAKEN, TOOL_GUIDANCE["slot_taken"])
            if outcome.error_code == EMAIL_REQUIRED:
                return _failed(outcome, EMAIL_REQUIRED, TOOL_GUIDANCE["email_required"])
            logger.warning(f"CALENDAR | booking failed: {outcome.error_code} {outcome.message}")
            return _failed(outcome, EXTERNAL_ERROR, TOOL_GUIDANCE["booking_failed"])

        self._record.meeting_booked = True
        self._record.meeting_start = str(outcome.data.get("start") or start.isoformat())
        self._record.meeting_reference = _text(outcome.data.get("reference"))
        self._record.next_action = NextAction.MEETING_BOOKED
        logger.info(
            f"CALENDAR | BOOKED {label(start)} via {outcome.data.get('provider', '?')}"
            f"{f' ref {self._record.meeting_reference}' if self._record.meeting_reference else ''}"
        )
        return ToolResult.ok(outcome.data, guidance=TOOL_GUIDANCE["booked"])

    async def schedule_callback(self, when_text: str, note: str = "") -> ToolResult:
        """Create a future callback. Claims one only when the backend confirms it."""
        if self.state is ConversationState.DO_NOT_CALL:
            return ToolResult.fail(
                NOT_AUTHORIZED,
                "they have asked not to be contacted again; no callback may be scheduled",
                guidance=TOOL_GUIDANCE["do_not_call"],
            )

        if not self.capabilities.can_schedule_callback:
            # Nowhere to put it, so the exact format does not matter: record
            # what they said in their words and say a colleague will arrange it.
            self.request_callback(when_text)
            return ToolResult.fail(
                UNAVAILABLE,
                "callbacks cannot be scheduled on this call; the request is recorded",
                guidance=TOOL_GUIDANCE["callback_unavailable"],
            )

        when = parse_when(when_text, self._tz)
        if when is None:
            return ToolResult.fail(
                INVALID_TIME,
                f"when must be an exact day and time written YYYY-MM-DDTHH:MM in"
                f" {_zone_name(self._tz)}, for example {(self._now() + timedelta(days=1)):%Y-%m-%dT%H:%M};"
                f" got {when_text!r}",
                guidance=TOOL_GUIDANCE["invalid_time"],
            )
        now = self._now()
        if when <= now + _MIN_NOTICE:
            return ToolResult.fail(
                PAST_TIME,
                f"{label(when)} is not in the future; it is now {label(now)}",
                guidance=TOOL_GUIDANCE["callback_past"],
            )

        # The intent is true from here whatever the backend says.
        self.request_callback(label(when))
        outcome = await self._call_backend(
            self._actions.schedule_callback(when, note=(note or "").strip())
        )
        if not outcome.ok:
            guidance = {
                PAST_TIME: TOOL_GUIDANCE["callback_past"],
                TOO_FAR_AHEAD: TOOL_GUIDANCE["too_far_ahead"],
            }.get(outcome.error_code or "", TOOL_GUIDANCE["callback_failed"])
            logger.warning(f"CALLBACK | not scheduled: {outcome.error_code} {outcome.message}")
            return _failed(outcome, EXTERNAL_ERROR, guidance)

        self._record.callback_scheduled_for = str(outcome.data.get("scheduled_for") or when.isoformat())
        logger.info(f"CALLBACK | SCHEDULED for {label(when)}")
        return ToolResult.ok(outcome.data, guidance=TOOL_GUIDANCE["callback_scheduled"])

    async def transfer_to_human(self, reason: str = "") -> ToolResult:
        """Hand the call to a person, if this session can. Claims it only on success."""
        self._record.human_requested = True
        if self.state is ConversationState.DO_NOT_CALL:
            return ToolResult.fail(
                NOT_AUTHORIZED,
                "they have asked not to be contacted; end the call instead of transferring it",
                guidance=TOOL_GUIDANCE["do_not_call"],
            )
        if not self.capabilities.can_transfer:
            self._record.next_action = NextAction.HUMAN_FOLLOW_UP
            return ToolResult.fail(
                TRANSFER_UNAVAILABLE,
                "this call cannot be transferred; offer a callback from a colleague",
                guidance=TOOL_GUIDANCE["transfer_failed"],
            )

        outcome = await self._call_backend(self._actions.transfer_to_human((reason or "").strip()))
        if not outcome.ok:
            self._record.next_action = NextAction.HUMAN_FOLLOW_UP
            logger.warning(f"TRANSFER | failed: {outcome.error_code} {outcome.message}")
            return _failed(outcome, TRANSFER_FAILED, TOOL_GUIDANCE["transfer_failed"])

        self._record.transferred = True
        self._record.next_action = NextAction.TRANSFERRED
        self._record.add_note(f"transferred to a person{f': {reason}' if reason else ''}")
        # The call is leaving us. Recorded as an ending the agent chose, so the
        # summary does not read as a dropped line — but no end frame is pushed:
        # the carrier closes the media stream when it moves the call, and an
        # `EndFrame` here would make the serializer hang up the very call we
        # just handed over.
        self._end_requested = True
        self._machine.transition(
            ConversationState.ENDING, reason="transferred to a person", trigger="transfer_to_human"
        )
        logger.info(f"TRANSFER | handed over to {outcome.data.get('destination', 'a colleague')}")
        return ToolResult.ok(outcome.data, guidance=TOOL_GUIDANCE["transferring"])

    # --- The result ---------------------------------------------------------

    def outcome(self) -> dict[str, Any]:
        """Everything the call established, as plain data.

        Deliberately a dict rather than a typed object: it crosses into the
        campaign layer through `ConversationSink`, and keeping it untyped there
        means adding a field to the qualification record does not change the
        interface between two packages that otherwise know nothing about each
        other.
        """
        return {
            "final_state": self.state.value,
            "state_path": [state.value for state in self._machine.path],
            "transitions": [
                {
                    "from": t.previous.value,
                    "to": t.current.value,
                    "trigger": t.trigger,
                    "reason": t.reason,
                }
                for t in self._machine.history
            ],
            "refused_transitions": [
                {"from": f.value, "to": t.value, "reason": r}
                for f, t, r in self._machine.refused
            ],
            "qualification": self._record.to_dict(),
            "actions": [action.to_dict() for action in self._audit.actions],
            "capabilities": self.capabilities.describe(),
            "prospect_id": self._brief.prospect_id,
            "campaign_id": self._brief.campaign_id,
            "call_attempt_id": self._brief.call_attempt_id,
            "identity_source": self._brief.source,
            "user_turns": self._user_turns,
            "agent_turns": self._agent_turns,
            "duration_secs": round(time.monotonic() - self._started_at, 1),
            # Phase 8. The phone call's own audio-connected duration, when the
            # session is a phone call; None on a browser or eval session. The
            # line above is the conversation's view and starts at construction.
            "call_duration_secs": self._call_duration_secs,
            "agent_ended_call": self._end_requested,
            # Phase 11. What the call consumed, and what that cost when rates
            # are configured. Travels with the outcome for the same reason the
            # transcript does: the sink is the one module that knows both this
            # package and the campaign tables, so it is the only thing that
            # should be writing to them.
            "usage": self._usage,
            "cost": self._cost,
            # Phase 12. How the turns went — latency, barge-ins, failed turns —
            # and whether a machine answered. `voicemail` is what the sink reads
            # to set the attempt's status; `quality` is stored beside it so the
            # numbers behind a bad call can be read back with `campaign.py
            # result`.
            "quality": self._quality,
            "voicemail": self._voicemail or {"detected": False},
            # Phase 8. The transcript travels with the outcome, verbatim, so the
            # sink can store it beside the record it is the evidence for.
            "transcript": self._transcript.to_list(),
            # The zone the record's naive times (`meeting_start`) are written in.
            "timezone": _zone_name(self._tz),
        }

    async def finish(
        self,
        *,
        call_duration_secs: float | None = None,
        usage: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None,
        quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close the conversation and hand the outcome to the sink.

        Idempotent, because a session can end through more than one path — the
        caller hangs up while the agent's own `end_call` is still in flight — and
        writing the outcome twice would be worse than writing it once.

        Args:
            call_duration_secs: How long the phone call's audio was connected,
                from `CallSession`, when this was a phone call. Recorded on the
                outcome as the call's duration; the conversation's own clock is
                the fallback.
            usage: What the call consumed, from `reliability/usage.py` (Phase
                11). Plain data, so this package still knows nothing about
                Pipecat's metrics types.
            cost: What that cost, when per-unit rates are configured. `None`
                means no rate was set — never a guessed zero.
            quality: What the turns looked like, from the turn monitor (Phase
                12). Plain data; stored with the record, never interpreted here.
        """
        if call_duration_secs is not None and self._call_duration_secs is None:
            self._call_duration_secs = round(float(call_duration_secs), 1)
        if usage is not None:
            self._usage = usage
        if cost is not None:
            self._cost = cost
        if quality is not None:
            self._quality = quality
        # Notes reach `call_results` and the CRM; the fields themselves stay in
        # the record. De-duplicated, so a second `finish` adds nothing.
        for kind, value in self._contacts_given:
            self._record.add_note(f"{kind} given on the call: {value}")
        outcome = self.outcome()
        if self._finished:
            return outcome
        self._finished = True

        logger.info(
            f"CONVERSATION | {self.state.value} after {outcome['user_turns']} caller turn(s) | "
            f"{self._record.describe()}"
        )
        for transition in self._machine.history:
            logger.debug(f"CONVERSATION | {transition.describe()}")

        try:
            await self._sink.on_call_finished(self._brief, outcome)
        except Exception:  # noqa: BLE001 - the call is over; never mask its ending
            logger.exception("OUTCOME | could not be stored")
        return outcome

    def tools(self) -> list[Any]:
        """Every tool of this call, each carrying its validated handler.

        Phase 31: these are *registered* on the LLM service once, explicitly
        (`bot.py`), which Pipecat never prunes; what the model is *shown* on a
        given request is `advertised_tools()`.
        """
        from .tools import build_tools  # Local import: tools.py imports this module.

        return build_tools(self)

    def advertised_tools(self) -> list[Any]:
        """The tools to describe to the model on the next request. Phase 31.

        Handler-less copies of this call's tools, chosen for the stage the call
        is in (`tools.advertised_tool_names`). Handler-less so that advertising
        them registers nothing and withdrawing them unregisters nothing: the
        handlers live on the service for the whole call. Empty before the
        prospect has spoken.
        """
        from .tools import advertised_tool_names  # Local import: tools.py imports this module.

        if self._advertisable is None:
            self._advertisable = {
                schema.name: FunctionSchema(
                    name=schema.name,
                    description=schema.description,
                    properties=schema.properties,
                    required=list(schema.required),
                )
                for schema in self.tools()
            }
        names = advertised_tool_names(
            self.state,
            self._record,
            self.capabilities,
            spoken=self._user_turns > 0,
            slots_offered=bool(self._offered_slots),
            signals=self._turn_signals,
        )
        return [self._advertisable[name] for name in names if name in self._advertisable]

    # --- Internals -----------------------------------------------------------

    def _now(self) -> datetime:
        """The current moment, timezone-aware, in the call's zone."""
        if self._clock is None:
            moment = datetime.now(UTC)
        elif callable(self._clock):
            moment = self._clock()
        else:
            moment = self._clock
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self._tz)
        return moment.astimezone(self._tz)

    def _today(self):
        return self._now().date()

    def _refuse_if_rejected(self, action: str) -> ToolResult | None:
        """The authorization check every meeting action shares."""
        if self.state is ConversationState.DO_NOT_CALL:
            return ToolResult.fail(
                NOT_AUTHORIZED,
                f"they have asked not to be contacted again; do not {action}",
                guidance=TOOL_GUIDANCE["do_not_call"],
            )
        if self.state.is_rejection:
            return ToolResult.fail(
                NOT_AUTHORIZED,
                f"they have said no; do not {action}",
                guidance=TOOL_GUIDANCE["not_interested"],
            )
        return None

    async def _call_backend(self, call: Any) -> ActionOutcome:
        """Await a backend call, turning any exception into a failed outcome.

        The backend's contract is that it never raises; this is what makes that
        true even when it is a test stub or a bug. A person is on the line.
        """
        try:
            outcome = await call
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.exception("ACTION | the backend raised")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"{exc.__class__.__name__}: {exc}")
        if not isinstance(outcome, ActionOutcome):
            logger.error(f"ACTION | the backend returned {type(outcome).__name__}, not an ActionOutcome")
            return ActionOutcome.failure(EXTERNAL_ERROR, "the backend returned an unusable answer")
        return outcome


def _failed(outcome: ActionOutcome, default_code: str, guidance: str) -> ToolResult:
    """A `ToolResult` for a failed backend outcome, keeping its code when it has one."""
    code = outcome.error_code or default_code
    try:
        return ToolResult.fail(
            code, outcome.message or "the action failed", guidance=guidance, data=outcome.data or None
        )
    except ValueError:
        return ToolResult.fail(
            default_code, outcome.message or "the action failed", guidance=guidance
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _zone_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or ("UTC" if tz is UTC else str(tz))
