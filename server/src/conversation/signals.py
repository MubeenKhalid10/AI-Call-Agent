"""Deterministic detection of the few things that must not depend on the model.

**This is the safety net, not the mechanism.** The conversation is driven by the
LLM calling tools; that is what reads a sentence like "honestly, we're happy
where we are and I'd rather you didn't ring again" correctly, and no phrase list
ever will. The requirement is explicit that state must not depend on fragile
string matching alone — so this file is *alone with nothing*, it runs alongside.

What it adds is a floor under the cases where the model failing to act is worse
than a false positive:

* **A do-not-call request** is a promise, and in most jurisdictions a legal
  obligation. If the model does not call the tool, this forces the state anyway
  and triggers the same backend action.
* **"Am I talking to a robot?"** must be answered honestly and immediately. A
  model that dodges it once has broken the one rule this agent cannot break.
* **"Put me through to a person."** Continuing to sell after that is the thing
  people hate most about automated calls.
* **A plain rejection** is *advisory only* — it adds guidance, it does not force
  a state. "I'm not interested in switching right now, but tell me more" is a
  real sentence, and forcing `NOT_INTERESTED` on the word "interested" would end
  live calls that were going well.

The asymmetry is the design. Forcing a state is reserved for the case where a
false positive costs one lost sale and a false negative costs a person being
called again after they asked not to be. Everywhere else the model decides and
this only nudges.

Matching runs on the STT transcript, so the patterns tolerate what transcription
does to speech: missing apostrophes, "dont" and "don't", "do not" split apart,
and no punctuation at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Signal(StrEnum):
    """Something detected in the prospect's words that the agent must react to."""

    DO_NOT_CALL = "DO_NOT_CALL"
    """They asked never to be contacted again. Forces the state."""

    ASKED_IF_HUMAN = "ASKED_IF_HUMAN"
    """"Is this a real person?" — must be answered honestly, at once."""

    WANTS_HUMAN = "WANTS_HUMAN"
    """They want a person on the line, not this."""

    REJECTION = "REJECTION"
    """A plain no. Advisory: it guides the next turn, it does not force a state."""

    CALLBACK = "CALLBACK"
    """"Call me next week." Advisory — the tool is what records the time."""

    SEND_INFORMATION = "SEND_INFORMATION"
    """"Just email me something." Advisory, and recorded: the objection and the
    next action are written by the conversation the moment it is heard, so the
    record does not depend on the model calling `record_objection` — observed
    2026-09-10, the honest reply came and the tool call did not."""

    VAGUE = "VAGUE"
    """A short hedge with nothing in it — "I suppose", "it depends", "hard to
    say". Advisory: the next block asks for a narrower question instead of a
    restart. Observed 2026-09-10: three vague answers in a row got the
    introduction and "is now a bad time?" again."""

    MENTIONED_TIME = "MENTIONED_TIME"
    """A weekday, "tomorrow", a clock time. Advisory, Phase 7: when a session can
    book or schedule, the next block tells the model to act on the day rather
    than say it will. Measured 2026-09-04: without it, "would Monday morning
    work?" got "let me see what's free" and no tool call."""

    END_CALL = "END_CALL"
    """They asked to end the call now — "cut the call", "hang up", "end the call",
    "I have to go", "goodbye". Advisory: the override tells the agent to say a
    short goodbye and call end_call, instead of pressing on with another
    question. Observed 2026-09-15: "can you please cut off the call?" was
    ignored and the agent asked a discovery question. A caller-asked end also
    lets the call hang up on the next goodbye even if the model forgets the
    tool (see `SalesConversation.closing_line_needs_hangup`)."""


# Every pattern below is matched against the transcript after it has been
# lower-cased and stripped of punctuation, so apostrophes and full stops cannot
# make a match fail. `\b` boundaries keep "call me later" from firing inside
# "recall me later".
_DO_NOT_CALL = (
    r"\bdo ?n[o']?t (ever )?(call|ring|phone|contact) me\b",
    r"\bdo not (ever )?(call|ring|phone|contact)\b",
    r"\bstop (calling|ringing|phoning|contacting)\b",
    r"\bnever (call|ring|phone|contact) (me|this number|us)\b",
    r"\b(remove|take) (me|us|this number|my number) off( (your|the) (list|database|records))?\b",
    r"\b(remove|delete) (me|us|my (number|details)) from (your |the )?(list|database|records)\b",
    r"\bunsubscribe\b",
    r"\bdo not call (list|register|registry)\b",
    r"\bopt (me )?out\b",
    r"\blose (my|this) number\b",
    r"\b(dont|do not) (call|ring|phone) (here|this number|again)\b",
    r"\btake me off (your |the )?list\b",
)

_ASKED_IF_HUMAN = (
    r"\bare you (a )?(real|actual|human|live)?\s?(person|human|being)\b",
    r"\bare you (a )?(robot|bot|machine|recording|computer|ai|a i)\b",
    r"\bis this (a )?(robot|bot|recording|machine|real person|human|ai|a i)\b",
    r"\bam i (talking|speaking) to (a )?(robot|bot|machine|computer|real person|human|ai|a i)\b",
    r"\byou(re| are) (a )?(robot|bot|recording|machine|ai|a i)\b",
    r"\bis (this|that) (an? )?(ai|a i|automated|recorded)\b",
)

_WANTS_HUMAN = (
    r"\b(put me through|transfer me|connect me) to (a |an )?(real )?(person|human|agent|someone)\b",
    r"\b(let me|can i|i want to|id like to|i would like to) (speak|talk) to (a |an )?(real )?(person|human|manager|somebody|someone)\b",
    r"\bget me (a )?(real )?(person|human|manager)\b",
    r"\bi want (a )?(real )?(person|human)\b",
    r"\bspeak to (a )?(real )?(person|human being)\b",
)

_REJECTION = (
    r"\b(im|i am|were|we are) not interested\b",
    r"\bnot interested\b",
    r"\bno thank ?(you|s)\b",
    r"\bwe(re| are)? (all set|good|fine|happy) (with|where)\b",
    r"\bwe do ?n[o']?t need\b",
    r"\bno[,.]? (im|i am|we are|were) (good|fine|all set)\b",
    r"\bplease do ?n[o']?t\b",
)

_CALLBACK = (
    r"\bcall (me|us|back) (later|another time|tomorrow|next week|next month|in the morning|this afternoon)\b",
    r"\bcall me back\b",
    r"\b(can|could) you (call|ring|phone) (me|us) (back|later|tomorrow|next week)\b",
    r"\btry (me|us) (again )?(later|tomorrow|next week|another time)\b",
    r"\b(ring|phone) me (later|back|tomorrow|next week)\b",
    r"\bnot a good time\b.*\b(call|later|tomorrow)\b",
)

# Being asked to send something instead of talking. "Email me", "send me some
# information", "put it in an email", "just send it over".
_SEND_INFORMATION = (
    r"\b(send|email|mail|forward|shoot) (me|us) (some |the |an? |your |more |a bit of )?(info|information|details|brochure|deck|pricing|literature|pdf|link|something|stuff|material)\b",
    r"\b(send|email|mail|forward) (it|that|something|some (info|information|details)) (over|through|across|to me|to us)\b",
    r"\bput (it|that|the details|something) in an? (email|e mail)\b",
    r"\b(can|could|would) you (just )?(send|email|mail) (me|us|it|that|something)\b",
    r"\bjust (send|email|mail) (me|us|it|something|the details)\b",
    r"\b(drop|fire|ping) (me|us) an? (email|e mail|message|line)\b",
    r"\bemail (me|us)( (it|that|something|the details|some info|some information))?\b",
    r"\bsend (me|us) an? (email|e mail)\b",
)

# A hedge. Only counts as vague when the whole turn is short and says nothing
# else (see `detect`): "maybe Thursday" is a time, "not really, we run forty
# trucks" is an answer.
_VAGUE = (
    r"\bi suppose\b",
    r"\bit depends\b",
    r"\bhard to say\b",
    r"\bnot (really |quite |entirely )?sure\b",
    r"\bi guess\b",
    r"\bmaybe\b",
    r"\bperhaps\b",
    r"\bpossibly\b",
    r"\b(kind|sort) of\b",
    r"\bid have to (look|check|think|see|ask)\b",
    r"\b(dunno|dont know|couldnt say|cant say|no idea)\b",
    r"\bnot really\b",
    r"\bwe ll see\b",
)
_VAGUE_MAX_WORDS = 12
_DIGIT = re.compile(r"\d")

# A day or a time of day, named. Deliberately not "morning" or "afternoon" on
# their own — "good morning" is a greeting — and not "later", which is a
# brush-off; a weekday, "tomorrow", a dated week, or a clock time is somebody
# proposing when.
_MENTIONED_TIME = (
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\b(tomorrow|day after tomorrow)\b",
    r"\bnext (week|month|monday|tuesday|wednesday|thursday|friday)\b",
    r"\b(this|next) (morning|afternoon|evening)\b",
    r"\b(end|start|beginning) of (the|next) (week|month)\b",
    r"\b\d{1,2}(:\d{2})? ?(am|pm|a m|p m|oclock|o clock)\b",
    r"\b(half|quarter) past \w+\b",
    r"\bin (a couple of|two|three|a few) (days|weeks)\b",
)

# An explicit request to end the call now. Kept to clear end-of-call phrases so
# a mid-sentence "bye the way" or "I have to say" does not fire it; "later" and
# "call me back" are a callback, handled above, not an end.
_END_CALL = (
    r"\b(cut|end|drop|disconnect|kill|stop|finish) (off )?(the|this) call\b",
    r"\bcut (the|this) call off\b",
    r"\bcut off (the|this) call\b",
    r"\bhang ?up\b",
    r"\bend (the |this )?call now\b",
    r"\bi (have|need|gotta|got) to (go|run|leave|jump off|get going)\b",
    r"\bi(m| am) (gonna |going to )?(gonna )?(head off|get going|hang up)\b",
    r"\bthat ?s (all|it|everything)( for now| thanks| thank you)?\b",
    r"\bwe(re| are) (all )?done( here)?\b",
    r"\b(lets|let us|we can|can we|shall we) (wrap|finish|end) (this|it|the call)?\s?(up)?\b",
    r"\bgood ?bye\b",
    r"\bbye bye\b",
)

_COMPILED: dict[Signal, tuple[re.Pattern[str], ...]] = {
    Signal.DO_NOT_CALL: tuple(re.compile(p) for p in _DO_NOT_CALL),
    Signal.ASKED_IF_HUMAN: tuple(re.compile(p) for p in _ASKED_IF_HUMAN),
    Signal.WANTS_HUMAN: tuple(re.compile(p) for p in _WANTS_HUMAN),
    Signal.REJECTION: tuple(re.compile(p) for p in _REJECTION),
    Signal.CALLBACK: tuple(re.compile(p) for p in _CALLBACK),
    Signal.MENTIONED_TIME: tuple(re.compile(p) for p in _MENTIONED_TIME),
    Signal.SEND_INFORMATION: tuple(re.compile(p) for p in _SEND_INFORMATION),
    Signal.VAGUE: tuple(re.compile(p) for p in _VAGUE),
    Signal.END_CALL: tuple(re.compile(p) for p in _END_CALL),
}

# Only these force a state change. See the module docstring for why the list is
# this short.
FORCING = frozenset({Signal.DO_NOT_CALL})

# Apostrophes are *deleted* rather than turned into a space, and every other
# punctuation mark is turned into a space. The distinction matters: "Don't" has
# to become "dont" so that one pattern covers "dont", "don't" and "do not",
# whereas "list, and" has to become two words rather than one.
_APOSTROPHES = re.compile(r"['‘’ʼ`]")
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SignalReport:
    """What was detected in one utterance.

    Attributes:
        matched: Which signals fired, in the order this module checks them.
        evidence: Signal -> the phrase that matched, so a forced do-not-call can
            be explained to whoever reviews it later. "The regex fired" is not
            an answer anybody can act on; "they said 'take me off your list'"
            is.
    """

    matched: frozenset[Signal] = frozenset()
    evidence: dict[Signal, str] = field(default_factory=dict)

    def __contains__(self, signal: object) -> bool:
        """Whether a signal fired, so callers can write `if Signal.X in report`."""
        return signal in self.matched

    def __bool__(self) -> bool:
        """Truthy when anything at all was detected."""
        return bool(self.matched)

    @property
    def forces_do_not_call(self) -> bool:
        """Whether this utterance must move the call to `DO_NOT_CALL` on its own."""
        return Signal.DO_NOT_CALL in self.matched

    def reason(self, signal: Signal) -> str:
        """The matched phrase for a signal, for a log line or an audit trail."""
        return self.evidence.get(signal, "")


def normalize(text: str) -> str:
    """Reduce an utterance to the form the patterns are written against.

    Lower case, apostrophes deleted, other punctuation replaced by a space,
    whitespace collapsed. Normalising here rather than escaping punctuation in
    every pattern is what makes "Don't call me!", "dont call me" and
    "Do not — call me" the same string, and STT output varies across all three.

    Deleting apostrophes and spacing everything else is the part that is easy to
    get wrong: replacing an apostrophe with a space turns "don't" into "don t",
    which no reasonable pattern matches.
    """
    lowered = _APOSTROPHES.sub("", text.lower())
    stripped = _PUNCTUATION.sub(" ", lowered)
    return _WHITESPACE.sub(" ", stripped).strip()


def detect(text: str) -> SignalReport:
    """Scan one utterance from the prospect.

    Args:
        text: What they said, as the STT transcribed it.

    Returns:
        A `SignalReport`. Empty for anything that matched nothing, which is the
        overwhelming majority of turns.
    """
    if not text or not text.strip():
        return SignalReport()

    normalized = normalize(text)
    matched: set[Signal] = set()
    evidence: dict[Signal, str] = {}

    for signal, patterns in _COMPILED.items():
        for pattern in patterns:
            found = pattern.search(normalized)
            if found:
                matched.add(signal)
                evidence[signal] = found.group(0)
                break

    if Signal.VAGUE in matched:
        other = matched - {Signal.VAGUE}
        words = normalized.split()
        if other or len(words) > _VAGUE_MAX_WORDS or _DIGIT.search(normalized):
            matched.discard(Signal.VAGUE)
            evidence.pop(Signal.VAGUE, None)
    # A do-not-call request usually contains a rejection too ("I'm not
    # interested, don't call again"). Reporting both would put two overrides in
    # front of the model, the weaker one contradicting the stronger; the
    # do-not-call is the only one that matters.
    if Signal.DO_NOT_CALL in matched:
        matched.discard(Signal.REJECTION)
        matched.discard(Signal.CALLBACK)
        matched.discard(Signal.MENTIONED_TIME)
        matched.discard(Signal.END_CALL)
    # A do-not-call and a callback both end the call in their own way, so an
    # end-call request adds nothing when one of them fired; drop it so a single,
    # unambiguous override is in front of the model.
    if Signal.CALLBACK in matched:
        matched.discard(Signal.MENTIONED_TIME)
        matched.discard(Signal.END_CALL)
    # "Cut the call" ends things now; a rejection, a hedge or a time mentioned in
    # the same breath would only add a second, weaker instruction.
    if Signal.END_CALL in matched:
        matched.discard(Signal.REJECTION)
        matched.discard(Signal.VAGUE)
        matched.discard(Signal.MENTIONED_TIME)

    return SignalReport(matched=frozenset(matched), evidence=evidence)
