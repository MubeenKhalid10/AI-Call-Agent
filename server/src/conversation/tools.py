"""The tools the model calls: to record what it learns, and to act.

**Why tools at all, rather than reading the state out of the transcript?** The
requirement is that conversation state must not depend on fragile string
matching alone. A tool call is the model's own structured statement of what it
just understood — "they told me their contract is up in March" arrives as
`record_discovery(timeline="THIS_QUARTER")` rather than as a regex over the word
"March". The deterministic detectors in `signals.py` sit underneath as a floor
for the few cases where the model failing to act would be unacceptable; these
are the mechanism.

**Two kinds of tool, one shape of result.** The recording tools write the
qualification record and move the state machine. The action tools — Phase 7 —
reach the world: they search the knowledge base, check and book a calendar,
schedule a callback, mark a do-not-call, transfer the call, and end it. Every
one of the twelve answers with the same `ToolResult` shape: `success` is the
only thing the model may read as "it happened", a failure always carries an
`error_code` and a message, and every result carries `guidance` saying what to
do next. See `results.py`.

**The tools decide nothing.** Each one parses nothing, checks nothing and stores
nothing; it hands its arguments to `SalesConversation`, which applies the rules
that depend on where the call is, and which in turn asks the `ActionBackend`
behind `actions.py` for anything that touches a database or an API. The model
never reaches the database, and neither does this file — the chain is::

    LLM -> tool (here) -> SalesConversation (rules) -> ActionBackend (validation, I/O) -> result -> LLM

**Each result carries guidance.** The turn after a tool call is generated from a
context frame the assistant aggregator pushes *upstream*, which never passes
through `director.ConversationDirector` — so the stage block is not in front of
the model on that turn. The tool result is, which makes it the right place to
say "acknowledge the objection first", or "nothing was booked; do not say it
was". See `playbook.TOOL_GUIDANCE`.

**The docstrings are the schema, and they are short on purpose.** Every word in
them is sent to the model on every single turn, twelve tools at a time, and the
model's provider bills — and rate-limits — by the token. Measured on
2026-09-04: the first draft of these descriptions cost about 2,000 tokens per
request against a Groq free-tier budget of 8,000 per minute, which throttled
every second turn. So a docstring says *what* the tool is for in a sentence and
names each argument's format; *when* and *how* to use it is the stage block's
job (`playbook.stage_block`), which is only ever in front of the model once and
only says what the current stage needs.

The tools are built per call by `build_tools`, closing over one
`SalesConversation`. They are written as plain async functions whose name, typed
signature and docstring become the schema automatically — Pipecat's
direct-function path, so there is no duplicated JSON to drift — and each is
wrapped by `toolkit.strict_tool`, which validates the model's arguments against
that schema, guards the call, and writes the audit log line.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame
from pipecat.services.llm_service import FunctionCallParams

from .actions import Capabilities
from .conversation import SalesConversation
from .playbook import TOOL_GUIDANCE
from .qualification import Intent, InterestLevel, ObjectionKind, QualificationRecord
from .results import INVALID_ARGUMENTS, NOT_AUTHORIZED, NOT_STORED, ToolResult
from .signals import Signal
from .states import ConversationState
from .toolkit import INVALID_ARGUMENTS_GUIDANCE, strict_tool

#: Every tool, in the order the model is shown them.
TOOL_NAMES = (
    "record_discovery",
    "set_interest",
    "record_objection",
    "move_to_stage",
    "request_meeting",
    "search_knowledge_base",
    "check_calendar_availability",
    "book_meeting",
    "schedule_callback",
    "mark_do_not_call",
    "transfer_to_human",
    "end_call",
)

#: The recording tools every selling stage keeps: a pain point, push-back, a
#: clear no or a do-not-call can be said at any point of a call (Phase 6's
#: reason for one tool set), so these are never withheld while selling.
_RECORDING = ("record_discovery", "record_objection", "set_interest", "mark_do_not_call")

#: Phase 32. Every tool call costs a second LLM request: the model calls,
#: the result goes into the context, the model is run again to speak. For
#: the tools below the result changes nothing the caller needs to hear — they
#: write the qualification record and the state machine, in memory, and
#: answer "recorded" — so when the model has already spoken its reply in
#: the same response as the call, that reply is the turn and the second
#: request is skipped (`toolkit.strict_tool`'s ``release``). Each still runs
#: the second request when its result *does* need acting on: a no that must
#: be closed (`stopped_selling`), a refused move. The other seven keep the
#: request unconditionally: the model must not claim an availability, a
#: booking, a callback, a transfer, a knowledge answer, a do-not-call
#: outcome or an ending it has not seen the result of.
TURN_RELEASING = ("record_discovery", "set_interest", "record_objection", "move_to_stage", "request_meeting")
RESULT_BLOCKING = (
    "search_knowledge_base",
    "check_calendar_availability",
    "book_meeting",
    "schedule_callback",
    "mark_do_not_call",
    "transfer_to_human",
    "end_call",
)


def _recorded(result: ToolResult) -> bool:
    """A plain success: the caller heard what the model said, and the record is written."""
    return result.success


def _recorded_and_still_selling(result: ToolResult) -> bool:
    """A success that did not end the selling: a no needs the closing turn the result asks for."""
    return result.success and not (result.data or {}).get("stopped_selling")


def advertised_tool_names(
    state: ConversationState,
    record: QualificationRecord,
    capabilities: Capabilities,
    *,
    spoken: bool,
    slots_offered: bool,
    signals: frozenset[Signal] = frozenset(),
) -> list[str]:
    """Which tools the model should see right now. Phase 31.

    Every tool stays registered for the whole call (`bot.py` registers each
    handler once, explicitly, which Pipecat never prunes), so this only decides
    what is *described* to the model on this request — the twelve schemas were
    1,043 tokens, 35% of every request, and most of them could not be used at
    the moment they were sent. The set follows the existing state machine and
    the same conditions the per-turn guidance already uses to name a tool:

    * before the prospect has spoken (the opening): nothing — there is nothing
      to record and nothing to act on;
    * every selling stage: the four recording tools; `move_to_stage` once past
      the greeting; `search_knowledge_base` when there is a knowledge base;
      `check_calendar_availability` when there is a calendar (a day can be
      named in any stage, HANDOFF §21), `book_meeting` once times have been
      offered or the call is in MEETING_REQUEST; `request_meeting` when there
      is no calendar; `schedule_callback` when a callback or a time was just
      heard (the override that names it fires on those signals); `end_call`
      once a meeting is booked or in MEETING_REQUEST; `transfer_to_human` when
      they have asked for a person and the call can be transferred;
    * CALLBACK: `schedule_callback`, `set_interest`, `mark_do_not_call`, `end_call`;
    * NOT_INTERESTED: `set_interest`, `mark_do_not_call`, `end_call`;
    * DO_NOT_CALL: `mark_do_not_call`, `end_call`;
    * ENDING: `end_call`, `mark_do_not_call`.

    Args:
        state: The stage the call is in.
        record: What the call has learned so far.
        capabilities: What this session can do.
        spoken: Whether the prospect has said anything yet.
        slots_offered: Whether the calendar has returned times this call.
        signals: What the detectors heard in the latest turn.

    Returns:
        Tool names in `TOOL_NAMES` order.
    """
    if not spoken:
        return []
    wanted: set[str] = set()
    if state.is_selling:
        wanted.update(_RECORDING)
        if state is not ConversationState.GREETING:
            wanted.add("move_to_stage")
        if capabilities.can_search_knowledge:
            wanted.add("search_knowledge_base")
        if capabilities.can_book_meeting:
            wanted.add("check_calendar_availability")
            if slots_offered or state is ConversationState.MEETING_REQUEST:
                wanted.add("book_meeting")
        else:
            wanted.add("request_meeting")
        if (
            Signal.CALLBACK in signals
            or Signal.MENTIONED_TIME in signals
            or record.callback_intent is Intent.ACCEPTED
        ):
            wanted.add("schedule_callback")
        if record.meeting_booked or state is ConversationState.MEETING_REQUEST:
            wanted.add("end_call")
    elif state is ConversationState.CALLBACK:
        wanted.update(("schedule_callback", "set_interest", "mark_do_not_call", "end_call"))
    elif state is ConversationState.NOT_INTERESTED:
        wanted.update(("set_interest", "mark_do_not_call", "end_call"))
    elif state is ConversationState.DO_NOT_CALL:
        wanted.update(("mark_do_not_call", "end_call"))
    else:  # ENDING
        wanted.update(("end_call", "mark_do_not_call"))
    if capabilities.can_transfer and record.human_requested and state is not ConversationState.DO_NOT_CALL:
        wanted.add("transfer_to_human")
    return [name for name in TOOL_NAMES if name in wanted]


def build_tools(conversation: SalesConversation) -> list[Any]:
    """Build this call's tools, bound to its conversation.

    Args:
        conversation: The call these tools record into and act for.

    Returns:
        A list of `FunctionSchema` objects to hand to `LLMContext(tools=...)`,
        each carrying its own validated handler; Pipecat registers them itself.
    """

    # --- Recording -------------------------------------------------------------

    async def record_discovery(
        params: FunctionCallParams,
        pain_point: str = "",
        current_process: str = "",
        existing_provider: str = "",
        impact: str = "",
        desired_outcome: str = "",
        timeline: str = "",
        decision_role: str = "",
    ) -> ToolResult:
        """Record what they just told you about their situation. Fill only what they said.

        Args:
            pain_point: A problem they described, in their words.
            current_process: How they handle this today.
            existing_provider: The supplier or tool they already use.
            impact: What the problem costs them.
            desired_outcome: What they want instead.
            timeline: IMMEDIATE, THIS_QUARTER, THIS_YEAR, LATER or NONE.
            decision_role: DECISION_MAKER, INFLUENCER or NOT_INVOLVED.
        """
        changed = conversation.record_discovery(
            pain_point=pain_point,
            current_process=current_process,
            existing_provider=existing_provider,
            impact=impact,
            desired_outcome=desired_outcome,
            timeline=timeline,
            decision_role=decision_role,
        )
        return ToolResult.ok({"recorded": changed}, guidance=TOOL_GUIDANCE["recorded"])

    async def set_interest(params: FunctionCallParams, level: str, reason: str = "") -> ToolResult:
        """Record how interested they sound. NOT_INTERESTED only for a clear no.

        Args:
            level: INTERESTED, CURIOUS, NEUTRAL, RELUCTANT or NOT_INTERESTED.
            reason: What they said that showed it, briefly.
        """
        parsed, moved = conversation.set_interest(level, reason)
        if parsed is InterestLevel.UNKNOWN:
            allowed = ", ".join(m.value for m in InterestLevel if m is not InterestLevel.UNKNOWN)
            return ToolResult.fail(
                INVALID_ARGUMENTS,
                f"level must be one of {allowed}; got {level!r}. Nothing was recorded.",
                guidance=INVALID_ARGUMENTS_GUIDANCE,
                data={"allowed": allowed.split(", ")},
            )
        guidance = TOOL_GUIDANCE["not_interested"] if moved else TOOL_GUIDANCE["recorded"]
        return ToolResult.ok(
            {"interest": parsed.value, "stopped_selling": moved}, guidance=guidance
        )

    async def record_objection(params: FunctionCallParams, kind: str, detail: str = "") -> ToolResult:
        """Record push-back, before you answer it.

        Args:
            kind: NOT_INTERESTED, EXISTING_PROVIDER, PRICE, SEND_INFORMATION, NO_TIME,
                WHAT_DO_YOU_DO, WHY_SWITCH, CALL_LATER, WANTS_HUMAN or OTHER.
            detail: What they said, briefly.
        """
        before = conversation.state
        parsed = conversation.record_objection(kind, detail)
        stopped = conversation.state is not before and conversation.state.is_rejection
        if stopped:
            key = "not_interested"
        elif parsed is ObjectionKind.SEND_INFORMATION:
            key = "send_information"
        else:
            key = "objection"
        return ToolResult.ok(
            {"objection": parsed.value, "stopped_selling": stopped}, guidance=TOOL_GUIDANCE[key]
        )

    async def move_to_stage(params: FunctionCallParams, stage: str) -> ToolResult:
        """Say which part of the call you are moving into.

        Args:
            stage: discovery, qualification, value or meeting.
        """
        moved, message = conversation.move_to(stage)
        data = {"stage": conversation.state.value, "moved": moved}
        if moved:
            return ToolResult.ok(data, guidance=TOOL_GUIDANCE["recorded"])
        if message.startswith("unknown stage"):
            return ToolResult.fail(
                INVALID_ARGUMENTS,
                f"{message}; use one of discovery, qualification, value, meeting",
                guidance=INVALID_ARGUMENTS_GUIDANCE,
                data=data,
            )
        return ToolResult.fail(
            NOT_AUTHORIZED, message, guidance=TOOL_GUIDANCE["refused_move"], data=data
        )

    async def request_meeting(params: FunctionCallParams, when: str = "", note: str = "") -> ToolResult:
        """Record that they agreed to a next step. Books nothing; a colleague confirms the time.

        Args:
            when: The time they suggested, in their words.
            note: Anything they asked to be covered.
        """
        moved = conversation.request_meeting(when, note)
        if not moved and conversation.state.is_rejection:
            return ToolResult.fail(
                NOT_AUTHORIZED,
                "they have said no; do not arrange a meeting",
                guidance=TOOL_GUIDANCE["not_interested"],
            )
        can_book = conversation.capabilities.can_book_meeting
        return ToolResult.ok(
            {"meeting_intent": "ACCEPTED", "booked": False},
            guidance=TOOL_GUIDANCE["meeting_intent_calendar" if can_book else "meeting_intent"],
        )

    # --- Acting ------------------------------------------------------------------

    async def search_knowledge_base(params: FunctionCallParams, query: str) -> ToolResult:
        """Look up a fact about the business. Answer only from what it returns.

        Args:
            query: What to look up, in a few plain words.
        """
        return await conversation.search_knowledge(query)

    async def check_calendar_availability(
        params: FunctionCallParams, day: str, preferred_time: str = ""
    ) -> ToolResult:
        """Free meeting times on one day. Call it before offering any time; books nothing.

        Args:
            day: The day, written YYYY-MM-DD.
            preferred_time: A time of day they mentioned, like 10:00 or 2pm, if any.
        """
        return await conversation.check_availability(day, preferred_time)

    async def book_meeting(
        params: FunctionCallParams, start: str, attendee_email: str = "", notes: str = ""
    ) -> ToolResult:
        """Book a time check_calendar_availability returned, once they chose it. Booked only if success is true.

        Args:
            start: The chosen slot's exact start, copied from the availability result.
            attendee_email: Their email address, if asked for or given.
            notes: Anything they asked to be covered.
        """
        return await conversation.book_meeting(start, attendee_email, notes)

    async def schedule_callback(params: FunctionCallParams, when: str, note: str = "") -> ToolResult:
        """Schedule a call back to them, then close. Scheduled only if success is true.

        Args:
            when: The exact day and time, written YYYY-MM-DDTHH:MM.
            note: Anything to pass on, briefly.
        """
        return await conversation.schedule_callback(when, note)

    async def mark_do_not_call(params: FunctionCallParams, reason: str = "") -> ToolResult:
        """Record that they asked never to be contacted again. Then confirm it and end the call.

        Args:
            reason: What they said, briefly.
        """
        already = conversation.dnc_recorded
        stored = await conversation.do_not_call(reason=reason, trigger="tool")
        data = {"do_not_call": True, "stored": stored, "already_marked": already}
        if stored:
            return ToolResult.ok(data, guidance=TOOL_GUIDANCE["do_not_call"])
        return ToolResult.fail(
            NOT_STORED,
            "the request is honoured on this call and logged, but there is no prospect record to mark",
            guidance=TOOL_GUIDANCE["do_not_call_unstored"],
            data=data,
        )

    async def transfer_to_human(params: FunctionCallParams, reason: str = "") -> ToolResult:
        """Connect them to a colleague now; tell them first. Connected only if success is true.

        Args:
            reason: Why they want a person, briefly.
        """
        return await conversation.transfer_to_human(reason)

    async def end_call(params: FunctionCallParams, reason: str = "") -> None:
        """End the call, after your goodbye.

        Args:
            reason: Why the call is ending, briefly.
        """
        conversation.begin_ending(reason)
        # Report first: the result is what lets the model produce its closing
        # sentence, and the end frame below is queued behind that sentence's
        # audio. This is the one tool that delivers its own result, because
        # something has to happen *after* delivery.
        result = ToolResult.ok({"ending": True}, guidance=TOOL_GUIDANCE["ending"])
        await params.result_callback(result.to_dict())
        logger.info(f"CALL | agent ending the call{f': {reason}' if reason else ''}")
        # Downstream, which is the default and the correct direction: queued
        # frames are flushed first, so the agent finishes speaking before the
        # pipeline ends. On a phone call this is what makes the carrier hang up.
        await params.llm.push_frame(EndWorkerFrame())
        return None

    audit = conversation.audit
    releases = {
        "record_discovery": _recorded,
        "set_interest": _recorded_and_still_selling,
        "record_objection": _recorded_and_still_selling,
        "move_to_stage": _recorded,
        "request_meeting": _recorded,
    }
    assert set(releases) == set(TURN_RELEASING)
    return [
        # Phase 31: after a tool has run the stage may have moved, so the tool
        # set the next request advertises is refreshed on the context before
        # the result is delivered — the request that answers a tool result is
        # built from the context itself, not from the director's copy.
        # Phase 32: a recording tool ends the turn without a second request
        # when the model already spoke; `speech` is read at call time so a
        # tally attached after the tools were built still counts.
        strict_tool(
            function,
            audit=audit,
            advertise=conversation.advertised_tools,
            release=releases.get(function.__name__),
            speech=lambda: conversation.speech,
        )
        for function in (
            record_discovery,
            set_interest,
            record_objection,
            move_to_stage,
            request_meeting,
            search_knowledge_base,
            check_calendar_availability,
            book_meeting,
            schedule_callback,
            mark_do_not_call,
            transfer_to_human,
            end_call,
        )
    ]
