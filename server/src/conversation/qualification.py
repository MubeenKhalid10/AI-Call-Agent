"""What the call learned about the prospect, in a shape a CRM could read.

**The rule that shapes every type in here: unknown is a value, not a blank.**
Every enum has an explicit `UNKNOWN` member and every free-text field defaults
to `None`, and neither is ever filled in by inference. If the agent did not ask
about the budget, `buying_timeline` stays `UNKNOWN` — it does not become
`LATER` because the prospect sounded unenthusiastic. A guessed field is worse
than a missing one: a missing field says "go and find out", and a guessed one
says "no need to ask", which is how a rep ends up on a call quoting a timeline
nobody ever gave them.

The record is filled in by the conversation tools (`tools.py`), which the model
calls as it learns things, and by nothing else. It is deliberately not derived
from the transcript by a second LLM pass: that would be a whole extra inference
per turn, and a second opinion about what was said is not more reliable than the
first.

`qualification_status` is the one field the model does **not** set. It is
derived from the others by `_derive_status`, so that "qualified" always means
the same thing across every call and cannot be talked into existence by an
enthusiastic model.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class InterestLevel(StrEnum):
    """How the prospect is responding to the reason for the call."""

    UNKNOWN = "UNKNOWN"
    INTERESTED = "INTERESTED"
    """Actively engaged: asking questions, describing their situation."""

    CURIOUS = "CURIOUS"
    """Listening, not committed. The most common honest answer."""

    NEUTRAL = "NEUTRAL"
    RELUCTANT = "RELUCTANT"
    """Answering, but wants the call to end."""

    NOT_INTERESTED = "NOT_INTERESTED"
    """A clear no. Stops the pitch — see `states._ALLOWED`."""


class QualificationStatus(StrEnum):
    """Whether this prospect is worth a salesperson's time.

    Derived, never asserted. See `_derive_status`.
    """

    UNKNOWN = "UNKNOWN"
    """Not enough was established to say. The default, and it stays that way
    unless the call actually produced the evidence."""

    QUALIFIED = "QUALIFIED"
    PARTIALLY_QUALIFIED = "PARTIALLY_QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"


class BuyingTimeline(StrEnum):
    """When they might act, if at all."""

    UNKNOWN = "UNKNOWN"
    IMMEDIATE = "IMMEDIATE"
    THIS_QUARTER = "THIS_QUARTER"
    THIS_YEAR = "THIS_YEAR"
    LATER = "LATER"
    NONE = "NONE"
    """They said explicitly that they are not going to act."""


class DecisionRole(StrEnum):
    """Their authority over a decision like this one."""

    UNKNOWN = "UNKNOWN"
    DECISION_MAKER = "DECISION_MAKER"
    INFLUENCER = "INFLUENCER"
    """Part of the decision without owning it — introduces us to who does."""

    NOT_INVOLVED = "NOT_INVOLVED"


class Intent(StrEnum):
    """Whether a specific next step was asked for, agreed, or refused.

    Used for both the meeting and the callback, because the three answers are
    the same shape for each and one enum with a clear name beats two identical
    ones.
    """

    UNKNOWN = "UNKNOWN"
    REQUESTED = "REQUESTED"
    """One side proposed it and the other has not answered yet."""

    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"


class NextAction(StrEnum):
    """What should happen after this call.

    `MEETING_REQUESTED` and `MEETING_BOOKED` are different values on purpose.
    The first means the prospect agreed and somebody still has to arrange it;
    the second means `book_meeting` returned success from a real calendar. A
    status that read as a booking when nothing was booked would be a lie told
    to whoever reads the CRM next — which is why, until Phase 7, only the first
    existed.
    """

    UNKNOWN = "UNKNOWN"
    NONE = "NONE"
    MEETING_REQUESTED = "MEETING_REQUESTED"
    MEETING_BOOKED = "MEETING_BOOKED"
    """A slot is in the calendar. Set only by a successful `book_meeting`."""

    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    """They want calling back. Whether a callback is actually *scheduled* is
    `QualificationRecord.callback_scheduled_for`, set only by a successful
    `schedule_callback`."""

    SEND_INFORMATION = "SEND_INFORMATION"
    HUMAN_FOLLOW_UP = "HUMAN_FOLLOW_UP"
    """They asked to speak to a person, and somebody should call them."""

    TRANSFERRED = "TRANSFERRED"
    """The call was handed to a person live. Set only by a successful `transfer_to_human`."""

    DO_NOT_CONTACT = "DO_NOT_CONTACT"


class ObjectionKind(StrEnum):
    """The objections a cold call actually meets, as a closed vocabulary.

    Closed on purpose. The model is asked for one of these rather than for free
    text, so that "too pricey", "that's expensive" and "we don't have budget"
    all become `PRICE` and a campaign report can count them. `OTHER` is the
    escape hatch, and the raw wording is kept alongside in `detail` either way.
    """

    NOT_INTERESTED = "NOT_INTERESTED"
    EXISTING_PROVIDER = "EXISTING_PROVIDER"
    PRICE = "PRICE"
    SEND_INFORMATION = "SEND_INFORMATION"
    NO_TIME = "NO_TIME"
    WHAT_DO_YOU_DO = "WHAT_DO_YOU_DO"
    WHY_SWITCH = "WHY_SWITCH"
    CALL_LATER = "CALL_LATER"
    WANTS_HUMAN = "WANTS_HUMAN"
    OTHER = "OTHER"


# What the model might say, mapped to the vocabulary above. This is string
# matching, and it is the *safe* kind: it runs over the model's own structured
# argument, which comes from a list we gave it, not over what a person said down
# a phone line. An unrecognised value becomes OTHER rather than being dropped.
_OBJECTION_ALIASES = {
    "not_interested": ObjectionKind.NOT_INTERESTED,
    "no_interest": ObjectionKind.NOT_INTERESTED,
    "existing_provider": ObjectionKind.EXISTING_PROVIDER,
    "already_have": ObjectionKind.EXISTING_PROVIDER,
    "competitor": ObjectionKind.EXISTING_PROVIDER,
    "price": ObjectionKind.PRICE,
    "cost": ObjectionKind.PRICE,
    "too_expensive": ObjectionKind.PRICE,
    "budget": ObjectionKind.PRICE,
    "send_information": ObjectionKind.SEND_INFORMATION,
    "send_info": ObjectionKind.SEND_INFORMATION,
    "email_me": ObjectionKind.SEND_INFORMATION,
    "no_time": ObjectionKind.NO_TIME,
    "busy": ObjectionKind.NO_TIME,
    "what_do_you_do": ObjectionKind.WHAT_DO_YOU_DO,
    "who_are_you": ObjectionKind.WHAT_DO_YOU_DO,
    "why_switch": ObjectionKind.WHY_SWITCH,
    "call_later": ObjectionKind.CALL_LATER,
    "bad_time": ObjectionKind.CALL_LATER,
    "wants_human": ObjectionKind.WANTS_HUMAN,
    "human": ObjectionKind.WANTS_HUMAN,
    "real_person": ObjectionKind.WANTS_HUMAN,
}


def parse_objection_kind(value: str | None) -> ObjectionKind:
    """Map whatever the model said to one of the known kinds.

    Tolerant about spelling — spaces, hyphens and case are normalised — because
    the cost of `OTHER` on a real price objection is a campaign report that
    undercounts price objections, and the cost of accepting a hyphen is nothing.
    """
    if not value:
        return ObjectionKind.OTHER
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _OBJECTION_ALIASES:
        return _OBJECTION_ALIASES[key]
    try:
        return ObjectionKind(key.upper())
    except ValueError:
        return ObjectionKind.OTHER


def parse_enum[T: StrEnum](enum: type[T], value: str | None, default: T) -> T:
    """Read a model-supplied string into `enum`, falling back to `default`.

    The fallback is always the enum's `UNKNOWN` member at every call site. That
    is the whole point: a value we could not understand must leave the field
    unknown rather than picking the nearest-looking member, because the nearest
    looking member is a guess and this module does not guess.
    """
    if not value:
        return default
    key = value.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return enum(key)
    except ValueError:
        return default


@dataclass
class Objection:
    """One thing the prospect pushed back on.

    Attributes:
        detail: Their own words, as the model reported them. Kept because the
            kind is for counting and the wording is for the human who reads the
            call afterwards.
        handled: Set once the agent has responded to it. An unhandled objection
            at the end of a call is worth seeing in a report.
    """

    kind: ObjectionKind
    detail: str = ""
    handled: bool = False
    at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        """Plain data, for the JSON line at the end of the call."""
        return {"kind": self.kind.value, "detail": self.detail, "handled": self.handled}


@dataclass
class QualificationRecord:
    """Everything the call established, with unknowns left unknown.

    Mutable, unlike most of the dataclasses in this project, because it is
    written to across the whole call by tool handlers reacting to what the
    prospect says. There is one of these per call.
    """

    interest_level: InterestLevel = InterestLevel.UNKNOWN
    pain_points: list[str] = field(default_factory=list)
    objections: list[Objection] = field(default_factory=list)
    buying_timeline: BuyingTimeline = BuyingTimeline.UNKNOWN
    decision_role: DecisionRole = DecisionRole.UNKNOWN
    existing_provider: str | None = None
    current_process: str | None = None
    impact: str | None = None
    desired_outcome: str | None = None
    next_action: NextAction = NextAction.UNKNOWN
    meeting_intent: Intent = Intent.UNKNOWN
    meeting_when: str | None = None
    """The time they suggested, in their words. Not a booking."""

    meeting_booked: bool = False
    """True only after `book_meeting` returned success. The field the CRM reads."""

    meeting_start: str | None = None
    """ISO 8601 start of the booked slot, when there is one."""

    meeting_reference: str | None = None
    """The calendar provider's id for the booking, when there is one."""

    callback_intent: Intent = Intent.UNKNOWN
    callback_when: str | None = None
    """When they said, in their words. Not a schedule."""

    callback_scheduled_for: str | None = None
    """ISO 8601 time of the callback actually created. Set only by a successful
    `schedule_callback`, and the field the queue reads to re-dial them."""

    human_requested: bool = False
    """They asked for a person. Recorded separately from the objection list
    because it is a routing instruction, not a sales objection."""

    transferred: bool = False
    """The call was handed to a person. Set only by a successful `transfer_to_human`."""

    notes: list[str] = field(default_factory=list)

    # --- Writing -----------------------------------------------------------

    def add_pain_point(self, text: str | None) -> bool:
        """Record a problem they described. Returns whether it was new.

        De-duplicated case-insensitively: a model that reports the same pain
        point on three consecutive turns should not produce three entries in a
        CRM record.
        """
        return _append_unique(self.pain_points, text)

    def add_note(self, text: str | None) -> bool:
        """Record any other fact worth keeping. Returns whether it was new."""
        return _append_unique(self.notes, text)

    def add_objection(self, kind: ObjectionKind, detail: str = "") -> Objection:
        """Record an objection, merging it with an unhandled one of the same kind.

        Merging matters because a prospect who says "it's too expensive" and
        then "seriously, that's way out of our budget" has raised one objection
        twice, and a report that counts two makes the call look worse than it
        was.
        """
        for existing in self.objections:
            if existing.kind is kind and not existing.handled:
                if detail and detail not in existing.detail:
                    existing.detail = f"{existing.detail}; {detail}" if existing.detail else detail
                return existing
        objection = Objection(kind=kind, detail=detail)
        self.objections.append(objection)
        return objection

    def mark_objections_handled(self) -> int:
        """Mark every open objection as answered. Returns how many there were."""
        open_ones = [o for o in self.objections if not o.handled]
        for objection in open_ones:
            objection.handled = True
        return len(open_ones)

    # --- Reading -----------------------------------------------------------

    @property
    def qualification_status(self) -> QualificationStatus:
        """Whether this is a fit, derived from what was actually established."""
        return _derive_status(self)

    @property
    def open_objections(self) -> list[Objection]:
        """Objections raised and not yet responded to."""
        return [o for o in self.objections if not o.handled]

    def unknown_fields(self) -> list[str]:
        """Which discovery fields are still unknown, in the order worth asking.

        Feeds the stage guidance the agent gets before each turn, which is what
        turns "ask discovery questions" from a slogan in the system prompt into
        a specific next question about the thing we still do not know.
        """
        missing = []
        if not self.pain_points:
            missing.append("their main problem or challenge")
        if self.current_process is None:
            missing.append("how they handle this today")
        if self.impact is None:
            missing.append("what that costs them")
        if self.existing_provider is None:
            missing.append("whether they already use a provider")
        if self.buying_timeline is BuyingTimeline.UNKNOWN:
            missing.append("their timing")
        if self.decision_role is DecisionRole.UNKNOWN:
            missing.append("who else is involved in a decision like this")
        return missing

    def to_dict(self) -> dict[str, Any]:
        """The record as plain JSON-able data.

        Enums render as their names, so `"UNKNOWN"` appears in the output
        explicitly rather than as a null that a reader might mistake for "the
        field does not exist".
        """
        data = asdict(self)
        data["objections"] = [o.to_dict() for o in self.objections]
        for name in (
            "interest_level",
            "buying_timeline",
            "decision_role",
            "next_action",
            "meeting_intent",
            "callback_intent",
        ):
            data[name] = getattr(self, name).value
        data["qualification_status"] = self.qualification_status.value
        return data

    def describe(self) -> str:
        """A short human-readable summary, for the end-of-call log line."""
        parts = [
            f"interest={self.interest_level.value}",
            f"qualified={self.qualification_status.value}",
            f"timeline={self.buying_timeline.value}",
            f"decision={self.decision_role.value}",
            f"next={self.next_action.value}",
        ]
        if self.pain_points:
            parts.append(f"pain={len(self.pain_points)}")
        if self.objections:
            kinds = ",".join(o.kind.value for o in self.objections)
            parts.append(f"objections={kinds}")
        if self.existing_provider:
            parts.append(f"provider={self.existing_provider!r}")
        if self.meeting_booked:
            parts.append(f"booked={self.meeting_start}")
        if self.callback_scheduled_for:
            parts.append(f"callback={self.callback_scheduled_for}")
        if self.transferred:
            parts.append("transferred")
        return " | ".join(parts)


def _append_unique(target: list[str], text: str | None) -> bool:
    """Append `text` to `target` unless a case-insensitive match is already there."""
    if not text:
        return False
    cleaned = text.strip()
    if not cleaned:
        return False
    if any(cleaned.lower() == existing.lower() for existing in target):
        return False
    target.append(cleaned)
    return True


def _derive_status(record: QualificationRecord) -> QualificationStatus:
    """Work out the qualification status from the evidence, or leave it unknown.

    The rules, in the order they are applied:

    1. A clear no, or a stated intention never to act, is `DISQUALIFIED`. This
       one is decisive on its own: nothing else about the record can make
       somebody who said no into a lead.
    2. Otherwise we need three things — a problem worth solving, some interest,
       and somebody who can act. All three present is `QUALIFIED`; some of them
       is `PARTIALLY_QUALIFIED`.
    3. None of them, and nothing negative either, is `UNKNOWN` — which is the
       honest answer for a call that ended before discovery got anywhere, and
       is why this is derived rather than defaulted to "disqualified".
    """
    if record.interest_level is InterestLevel.NOT_INTERESTED:
        return QualificationStatus.DISQUALIFIED
    if record.next_action is NextAction.DO_NOT_CONTACT:
        return QualificationStatus.DISQUALIFIED
    if record.buying_timeline is BuyingTimeline.NONE:
        return QualificationStatus.DISQUALIFIED
    if record.decision_role is DecisionRole.NOT_INVOLVED and not record.pain_points:
        return QualificationStatus.DISQUALIFIED

    signals = (
        bool(record.pain_points),
        record.interest_level in (InterestLevel.INTERESTED, InterestLevel.CURIOUS),
        record.decision_role in (DecisionRole.DECISION_MAKER, DecisionRole.INFLUENCER),
    )
    met = sum(1 for signal in signals if signal)
    if met == len(signals):
        return QualificationStatus.QUALIFIED
    if met:
        return QualificationStatus.PARTIALLY_QUALIFIED
    return QualificationStatus.UNKNOWN
