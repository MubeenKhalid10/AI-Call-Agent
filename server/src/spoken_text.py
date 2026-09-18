"""What the caller may hear: the last gate between the model and the voice.

The LLM streams text to the TTS service in small chunks. Two things a model
can put into that stream must never be spoken:

* **Its own reasoning.** A reasoning model may write its chain of thought into
  the answer channel — either between ``<think>`` tags or, on some
  providers when tools are advertised, as plain narration ("I need to figure
  out which day next week refers to…"). On 2026-09-08 a real call spoke
  exactly that aloud. The provider-side fix is in `services.make_llm`
  (Groq's ``reasoning_format: hidden``); this module is the belt to that
  brace, and it also catches the tagged form that other providers stream.
* **Machine text.** Tool-call markup a model emits as text when it fails to
  use the tool API (``<tool_call>…</tool_call>``, ``<function=…>``), special
  tokens (``<|im_end|>``), and "sentences" with nothing speakable in them — a
  lone ``.`` or ``…`` after a tool call — which a TTS vendor refuses
  ("No valid transcripts passed"). Three such refusals in a row read to the
  session supervisor as a dead TTS and ended a call mid meeting-request.

`SpokenTextScrubber` is the pure, stateful text logic — fed chunk by chunk,
it emits only what may be spoken, holding back anything that might still turn
out to be the start of a tag or a sentence with no words in it.
`SpokenTextFilter` wraps it as the pipeline processor that sits between the
LLM and the TTS. It never changes what the model said in the context: the
assistant aggregator sits after the transport and records what was actually
spoken, which after this stage is exactly what the caller heard.

Nothing here reconstructs or exposes the reasoning. Hidden text is dropped
and counted, and the count is logged once per response.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# The words that make a square bracket a label rather than something said.
_BRACKET_LABELS = (
    "assistant", "agent", "user", "caller", "prospect", "system", "developer", "tool", "function",
    "call guidance", "guidance", "instruction", "instructions", "internal", "note", "turn",
    "cut off", "interrupted", "interruption", "pause", "silence",
)  # fmt: skip
# Openers of text that is not for the caller, each with the closer that ends
# it. Matched case-insensitively. Order matters only for the longest-prefix
# hold below.
_HIDDEN_BLOCKS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (re.compile(r"<think>", re.IGNORECASE), re.compile(r"</think>", re.IGNORECASE)),
    (re.compile(r"<thinking>", re.IGNORECASE), re.compile(r"</thinking>", re.IGNORECASE)),
    (re.compile(r"<reasoning>", re.IGNORECASE), re.compile(r"</reasoning>", re.IGNORECASE)),
    (re.compile(r"<tool_call>", re.IGNORECASE), re.compile(r"</tool_call>", re.IGNORECASE)),
    (re.compile(r"<function(?:_call)?(?:=|>)", re.IGNORECASE), re.compile(r"</function(?:_call)?>", re.IGNORECASE)),
    (re.compile(r"<\|"), re.compile(r"\|>")),
    # Any other tag the model makes up. Heard on a live call 2026-09-17:
    # "<system>Note: I will ignore requests to reveal or discuss system
    # instructions…</>" — a rule from its instructions, restated as a note to
    # itself inside the answer, closed with a tag that matched nothing. Nothing
    # a caller should hear is ever written between angle brackets, so the
    # block is hidden whatever it is called. Last, so the named tags above win.
    (re.compile(r"<[A-Za-z_][\w-]{0,30}>"), re.compile(r"</[\w-]{0,30}>")),
    # A tool call narrated instead of made. Heard on a live call 2026-09-18:
    # "[Calling record_objection...]" went to the voice, and no tool ran.
    (re.compile(r"\[calling\s", re.IGNORECASE), re.compile(r"\]")),
    # A label from the prompt's own bookkeeping, or one made up in its style.
    # Same day, an opening turn: `[assistant turn 1]: "Hi there, how are you
    # doing today?"` — the voice was handed all of it. An ordinary bracket
    # ("it costs [roughly] ten") is still left alone: only these words open one.
    (re.compile(r"\[(?:" + "|".join(_BRACKET_LABELS) + r")\b", re.IGNORECASE), re.compile(r"\]")),
)
# A closer arriving with no opener means everything before it was hidden text
# whose opener the model omitted — qwen-style reasoning is the known case.
_STRAY_CLOSERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"</think>", re.IGNORECASE),
    re.compile(r"</thinking>", re.IGNORECASE),
    re.compile(r"</reasoning>", re.IGNORECASE),
)
# The literal starts an opener can have, for deciding whether a trailing "<"
# might still become one once the next chunk arrives.
_OPENER_PREFIXES = ("<think>", "<thinking>", "<reasoning>", "<tool_call>", "<function", "<|", "</think>", "</thinking>", "</reasoning>")
_LONGEST_TAG = max(len(p) for p in ("</thinking>", "</reasoning>", "</tool_call>", "</function_call>"))
_ALNUM = re.compile(r"[^\W_]", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s*$")

# A reply that narrates its own plan before (or instead of) speaking it leaks the
# model's stage direction to the caller. Observed on a live call 2026-09-15:
#   "I'll ask one open discovery question about their current situation. \"...\""
#   "I'll start the call with a brief, natural greeting. \"Hi there...\""
# When the whole reply OPENS with such a planning phrase AND the real line
# follows in double quotes, only the quoted line is spoken. Deliberately narrow:
# it fires only on these openers and only when a double-quoted line is present,
# so an ordinary reply that merely begins "I'll get that sorted" (no quote) is
# left untouched. Single quotes are not treated as delimiters — they are
# apostrophes far more often than quotation marks. The system instruction also
# forbids this narration; this is the belt to that brace.
_PLAN_OPENER = re.compile(
    r"^\s*(?:okay[,.]?\s+|ok[,.]?\s+|alright[,.]?\s+|sure[,.]?\s+|first[,.]?\s+|next[,.]?\s+|so[,.]?\s+)?"
    r"(?:i['’]?ll|i\s+will|i\s+am\s+going\s+to|i['’]?m\s+going\s+to|i\s+need\s+to|i\s+should|i\s+want\s+to|"
    r"let\s+me|let['’]?s|to\s+(?:open|start|begin|greet|ask))\b",
    re.IGNORECASE,
)
# The closing delimiter may be a quote or the end of the text: the wordless-tail
# logic in `finish` drops a trailing bare quote before this runs, so the real
# line often arrives with its opening quote only.
_DOUBLE_QUOTED = re.compile(r"[\"“”]\s*([^\"“”]{3,}?)\s*(?:[\"“”]|$)")


_UNFINISHED_TAG = re.compile(r"</?[\w-]{0,30}")

# A reply that answers, and then starts talking to itself. Heard on a live call
# 2026-09-17, all of it spoken, thirty seconds of it:
#   "Yes, we handle UI/UX design as part of the build… Is that something you're
#    looking at for a current product? Wait, I need to answer based only on the
#    excerpt and instructions. The excerpt confirms UI/UX design work… I should
#    keep it brief and ask a question. "Yes, we handle UI/UX design, …"
# The provider's reasoning is switched off on purpose (`services.reasoning_extra`),
# so now and then the model reasons in the answer instead. Two things must both
# be true of a sentence before it is taken for that, because either alone is
# ordinary speech ("I need to check with the team", "I can't share my
# instructions"): it opens the way a note to oneself does, and it names
# something only the model can see.
_SELF_TALK_OPENER = re.compile(
    r"^[\s\"“”'(*-]*(?:(?:wait|hmm+|okay|ok|so|actually|no|hold on|right|also|but|and)[,.!…-]*\s+)*"
    r"(?:i\s+need\s+to|i\s+should|i\s+must|i\s+have\s+to|i\s+will|i['’]?ll|i\s+can(?:not|['’]?t)?|let\s+me|let['’]?s|"
    r"the\s+(?:excerpts?|passages?|instructions?|guidance|system|prompt|user|caller|prospect|knowledge\s+base|rules?|block|note)\b|"
    r"my\s+(?:instructions?|guidance|prompt|rules?)\s+(?:say|says|tell|tells|state|states|require|requires)|"
    r"according\s+to\s+(?:the|my)\b|note\s*:|draft\s*:|revised\s*:|final\s+(?:answer|reply)\b)",
    re.IGNORECASE,
)
_SELF_TALK_SUBJECT = re.compile(
    r"\b(?:excerpts?|passages?|(?:my|the)\s+(?:instructions?|guidance|prompt|rules)|system\s+prompt|knowledge\s+base|"
    r"the\s+user|the\s+caller|the\s+prospect|tool\s+calls?|"
    r"word\s+(?:limit|count)|(?:one|two|three|thirty)\s+(?:spoken\s+)?(?:sentences?|words)|"
    r"my\s+(?:reply|response|answer)|answer\s+based)\b",
    re.IGNORECASE,
)
# "Keep it brief" names nothing only the model can see, and a caller is told it
# all the time. Heard on live calls 2026-09-18: "That's fair. I'll keep it
# brief. I'm Alex, calling on behalf of…" lost everything after its first two
# words — the caller got "That's fair.", silence, then "Still there?". It is a
# note to self only as an obligation ("I should keep it brief and ask a
# question"), never as a promise ("I'll keep it brief").
_BREVITY = re.compile(r"\b(?:keep\s+(?:it|this)\s+(?:brief|short)|be\s+brief|be\s+concise)\b", re.IGNORECASE)
_OBLIGATION = re.compile(r"\bi\s+(?:need\s+to|should|must|have\s+to)\b", re.IGNORECASE)
# What a person does say with those words in it, and must be left alone: a
# refusal to discuss them.
_REFUSAL = re.compile(
    r"\b(?:can(?:not|['’]?t)|(?:am|['’]m)\s+not\s+able\s+to|won['’]?t|unable\s+to|not\s+allowed\s+to)\s+"
    r"(?:really\s+)?(?:share|go\s+into|discuss|reveal|talk\s+about|get\s+into|read|give|tell)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])[\"”')\]]*\s+")


def strip_self_talk(text: str) -> tuple[str, int]:
    """Cut a reply at the first sentence the model addressed to itself.

    Returns `(spoken, dropped)`. Everything from that sentence on goes: what
    follows a note-to-self is a second draft of what was already said, and
    speaking both is how a caller heard the same offer twice. When the note
    comes first and nothing was said before it, the reply's last double-quoted
    line — the model's own final draft — is what is spoken; with no such line,
    nothing is, which the turn monitor reports and is still better than the
    reasoning read aloud.
    """
    if not text:
        return text, 0
    sentences = _SENTENCE_SPLIT.split(text)
    for index, sentence in enumerate(sentences):
        opener = _SELF_TALK_OPENER.match(sentence)
        if opener is None or _REFUSAL.search(sentence):
            continue
        if _SELF_TALK_SUBJECT.search(sentence) or (_BREVITY.search(sentence) and _OBLIGATION.search(opener.group(0))):
            break
    else:
        return text, 0
    # A "sentence" with no words in it — the lone "." a model leaves between a
    # reply and its second thoughts — is not kept: a TTS vendor refuses one.
    kept = " ".join(s.strip() for s in sentences[:index] if _ALNUM.search(s)).strip()
    if _ALNUM.search(kept) is None:
        quoted = [q.strip() for q in _DOUBLE_QUOTED.findall(" ".join(sentences[index:])) if q.strip()]
        kept = quoted[-1] if quoted and len(quoted[-1]) >= 3 else ""
    return kept, max(0, len(text) - len(kept))


# Heard on a live call 2026-09-18, in the read-back of a phone number: "that's
# zero three零零, one two three, four five six七" — the model slipped into
# Chinese numerals mid-number. A numeral is said as the English digit it is, so
# the number survives; any other ideograph has no reading in this voice and goes.
_CJK_DIGITS = dict(zip("零〇一二三四五六七八九", ("zero", "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"), strict=True))
_CJK_DIGIT_RUN = re.compile("[" + "".join(_CJK_DIGITS) + "]+")
_CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]+")


def spell_foreign_digits(text: str) -> tuple[str, int]:
    """Chinese numerals as English digit words; other ideographs removed. Returns `(spoken, changed)`."""
    if not text or _CJK.search(text) is None:
        return text, 0
    out = _CJK_DIGIT_RUN.sub(lambda m: " " + " ".join(_CJK_DIGITS[c] for c in m.group(0)) + " ", text)
    out = _CJK.sub(" ", out)
    out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out, sum(1 for c in text if _CJK.match(c))


# A reply written as a line of a script: `Assistant: "Hi there."`, or what is
# left of `[assistant turn 1]: "Hi there."` once the bracket is hidden — a
# colon and a line in quotes. The label and the quotes are not words to say,
# and the assistant aggregator would otherwise write them into the transcript.
_SPEAKER_LABEL = re.compile(
    r"^\s*(?:(?:assistant|agent|ai|bot)(?:\s+turn)?(?:\s+\d+)?\s*)?[:：]\s*", re.IGNORECASE
)
_WHOLLY_QUOTED = re.compile(r"^[\"“]([^\"“”]+)[\"”]?$")


def strip_speaker_label(text: str) -> tuple[str, int]:
    """Drop a leading speaker label, and the quotes around a reply that is one quoted line.

    Returns `(spoken, dropped)`. Quotes are only removed together with a label:
    a reply that merely quotes something is left as it is.
    """
    match = _SPEAKER_LABEL.match(text or "")
    if match is None:
        return text, 0
    spoken = text[match.end() :].strip()
    quoted = _WHOLLY_QUOTED.match(spoken)
    if quoted:
        spoken = quoted.group(1).strip()
    return spoken, len(text) - len(spoken)


def strip_plan_narration(text: str) -> tuple[str, int]:
    """If `text` opens with a plan preamble and carries the real line in quotes.

    Returns `(spoken, dropped)`: the quoted line(s) joined, and how many
    characters were removed. When the reply does not match, returns it unchanged
    with a dropped count of zero.
    """
    if not text or not _PLAN_OPENER.match(text):
        return text, 0
    quoted = [q.strip() for q in _DOUBLE_QUOTED.findall(text) if q.strip()]
    spoken = " ".join(quoted)
    if len(spoken) < 3:
        return text, 0
    return spoken, max(0, len(text) - len(spoken))


@dataclass
class ScrubStats:
    """What one response had removed, for the log line."""

    hidden_chars: int = 0
    hidden_blocks: int = 0
    unspeakable_chars: int = 0

    @property
    def removed_anything(self) -> bool:
        return bool(self.hidden_chars or self.unspeakable_chars)

    def describe(self) -> str:
        parts = []
        if self.hidden_blocks:
            parts.append(f"{self.hidden_blocks} hidden block(s), {self.hidden_chars} char(s)")
        if self.unspeakable_chars:
            parts.append(f"{self.unspeakable_chars} unspeakable char(s)")
        return "; ".join(parts) or "nothing removed"


@dataclass
class SpokenTextScrubber:
    """Stateful, chunk-by-chunk: in goes what the model streamed, out goes what may be spoken.

    Call `feed` for every chunk of one response and `finish` when the response
    ends; `reset` when a response is abandoned (an interruption). The output of
    the calls, concatenated, is the spoken text.
    """

    # Hold the whole reply until the model has finished it. Observed 2026-09-10
    # with Groq's `reasoning_format: hidden` on: a tool-result turn streamed
    # the model's draft answer, then a bare `</think>`, then the real answer —
    # the provider strips a block only when its opener is there. Released
    # sentence by sentence, the draft was already at the voice when the
    # closer arrived and the caller heard the offer twice. Held, the closer
    # discards the draft before anything is synthesised. The cost is the
    # model's own generation time for the reply, a few hundred milliseconds
    # on Groq; the first token's wait, which dominates, is unchanged.
    hold_all: bool = True
    stats: ScrubStats = field(default_factory=ScrubStats)
    _pending: str = ""
    _closer: re.Pattern[str] | None = None
    _held: str = ""
    _spoken_since_boundary: bool = False
    _visible: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """Forget everything about the current response."""
        self.stats = ScrubStats()
        self._pending = ""
        self._closer = None
        self._held = ""
        self._spoken_since_boundary = False
        self._visible = []

    def feed(self, chunk: str) -> str:
        """Take one streamed chunk; return what may be spoken now (nothing until `finish` when `hold_all`)."""
        if not chunk:
            return ""
        self._pending += chunk
        spoken = self._speakable(self._scrub())
        if self.hold_all:
            if spoken:
                self._visible.append(spoken)
            return ""
        return spoken

    def finish(self) -> str:
        """The response is over: release what may still be spoken, drop the rest."""
        out = ""
        if self._closer is not None:
            # An unterminated hidden block: everything since its opener was
            # never for the caller.
            self.stats.hidden_chars += len(self._pending)
            self._pending = ""
            self._closer = None
        elif self._pending:
            visible, self._pending = self._pending, ""
            out = self._speakable(visible)
        if self._held:
            # A sentence with nothing to say in it, right at the end.
            self.stats.unspeakable_chars += len(self._held.strip())
            self._held = ""
        self._spoken_since_boundary = False
        if self.hold_all:
            out = "".join(self._visible) + out
            self._visible = []
            # The whole reply is assembled now: a speaker label in front of it
            # goes first, so what follows is judged as the reply it is.
            spoken, dropped = strip_speaker_label(out)
            if dropped:
                self.stats.unspeakable_chars += dropped
                out = spoken
            spoken, changed = spell_foreign_digits(out)
            if changed:
                self.stats.unspeakable_chars += changed
                out = spoken
            # …then a plan-narration preamble can be recognised and dropped
            # before anything reaches the voice.
            spoken, dropped = strip_plan_narration(out)
            if dropped:
                self.stats.unspeakable_chars += dropped
                out = spoken
            # And a reply that answers and then starts reasoning with itself.
            spoken, dropped = strip_self_talk(out)
            if dropped:
                self.stats.unspeakable_chars += dropped
                out = spoken
        return out

    # --- hidden blocks ------------------------------------------------------

    def _scrub(self) -> str:
        """Remove hidden blocks from `_pending`; return the text that is definitely visible."""
        out: list[str] = []
        while True:
            if self._closer is not None:
                match = self._closer.search(self._pending)
                if match is None:
                    # Still hidden. Keep only a tail that could be the start
                    # of the closer; everything before it is gone for good.
                    keep = _LONGEST_TAG - 1
                    if len(self._pending) > keep:
                        self.stats.hidden_chars += len(self._pending) - keep
                        self._pending = self._pending[-keep:]
                    return "".join(out)
                self.stats.hidden_chars += match.start()
                self._pending = self._pending[match.end():]
                self._closer = None
                continue

            stray = _earliest(self._pending, _STRAY_CLOSERS)
            opener = _earliest_block(self._pending)
            if stray is not None and (opener is None or stray.start() < opener[0].start()):
                # Reasoning whose opener never came: drop all of it, including
                # anything already collected for this call (it preceded the
                # closer, so it was reasoning too).
                dropped = "".join(self._visible) + self._held + "".join(out) + self._pending[: stray.end()]
                self.stats.hidden_chars += len(dropped)
                self.stats.hidden_blocks += 1
                out = []
                self._visible = []
                self._held = ""
                self._spoken_since_boundary = False
                self._pending = self._pending[stray.end():]
                continue
            if opener is not None:
                match, closer = opener
                out.append(self._pending[: match.start()])
                self._pending = self._pending[match.end():]
                self._closer = closer
                self.stats.hidden_blocks += 1
                continue

            # No tag in sight. A trailing "<…" that could still become one is
            # held back until the next chunk settles it.
            cut = _possible_opener_start(self._pending)
            out.append(self._pending[:cut])
            self._pending = self._pending[cut:]
            return "".join(out)

    # --- speakability ---------------------------------------------------------

    def _speakable(self, text: str) -> str:
        """Hold back text with no letters or digits until there is something to attach it to."""
        if not text:
            return ""
        if _ALNUM.search(text) is None:
            if self._spoken_since_boundary:
                out = text
            else:
                self._held += text
                return ""
        else:
            out = self._held + text
            if self.hold_all and not self._visible:
                # The first words of the reply: whatever whitespace or
                # punctuation came before them (a discarded draft's tail, a
                # blank line after a tool call) is not for the voice.
                out = out.lstrip()
            self._held = ""
            self._spoken_since_boundary = True
        if _SENTENCE_END.search(out):
            self._spoken_since_boundary = False
        return out


def _earliest(text: str, patterns: tuple[re.Pattern[str], ...]) -> re.Match[str] | None:
    found = [m for m in (p.search(text) for p in patterns) if m is not None]
    return min(found, key=lambda m: m.start()) if found else None


def _earliest_block(text: str) -> tuple[re.Match[str], re.Pattern[str]] | None:
    found = [(m, closer) for opener, closer in _HIDDEN_BLOCKS if (m := opener.search(text)) is not None]
    return min(found, key=lambda pair: pair[0].start()) if found else None


def _possible_opener_start(text: str) -> int:
    """Index from which `text` might be the start of an opener, or len(text)."""
    bracket = text.rfind("[")
    if bracket != -1:
        tail = text[bracket + 1 :].lower()
        if any(label.startswith(tail) for label in ("calling ", *_BRACKET_LABELS)):
            return min(bracket, _possible_opener_start(text[:bracket]))
    start = text.rfind("<")
    if start == -1:
        return len(text)
    tail = text[start:].lower()
    if any(prefix.startswith(tail) for prefix in _OPENER_PREFIXES):
        return start
    # A tag of the model's own making, still arriving: "<sys" in this chunk,
    # "tem>" in the next.
    if _UNFINISHED_TAG.fullmatch(tail):
        return start
    return len(text)


class SpeechTally:
    """What the model has said in each response, for a tool handler that has to know. Phase 32.

    Fed by `SpeechObserver`, which Pipecat calls inside every ``push_frame``,
    so the record is exactly as far along as the pipeline is: the LLM
    service's own pushes number the responses (`started`) and say how many
    tool calls each made; the spoken-text filter's pushes say what was let
    through to the voice and when the response ended (`ended`). A tool
    handler runs while its response is still in flight — Pipecat hands the
    calls off before it pushes the end frame — so it reads `current_response`
    (the response it belongs to), waits for that response to end at the
    filter, and only then reads whether the model spoke.

    Pure state on the pipeline's asyncio loop; no threads.
    """

    def __init__(self) -> None:
        self.started = 0  # Responses the LLM has begun.
        self.ended = 0  # Responses the filter has seen the end of.
        self._filter_response = 0  # The response the filter is inside.
        self._spoke: dict[int, bool] = {}
        self._calls: dict[int, int] = {}
        self._changed = asyncio.Event()

    # --- What the LLM service pushed ---------------------------------------------

    def llm_started(self) -> None:
        self.started += 1
        self._calls.setdefault(self.started, 0)
        self._notify()

    def llm_calls(self, count: int) -> None:
        self._calls[self.started] = count

    # --- What the spoken-text filter pushed ---------------------------------------

    def filter_started(self) -> None:
        self._filter_response += 1
        self._spoke.setdefault(self._filter_response, False)

    def filter_spoken(self, text: str) -> None:
        if text and text.strip():
            self._spoke[self._filter_response] = True

    def filter_ended(self) -> None:
        self.ended = max(self.ended, self._filter_response)
        self._notify()

    def interrupted(self) -> None:
        """Every response in flight is over: nothing more of it will be spoken."""
        self._filter_response = max(self._filter_response, self.started)
        self.ended = max(self.ended, self.started)
        self._notify()

    # --- What a tool handler reads --------------------------------------------------

    @property
    def current_response(self) -> int:
        """The response the LLM most recently began: the one a running tool call belongs to."""
        return self.started

    @property
    def open(self) -> bool:
        return self.ended < self.started

    def spoke_in(self, response: int) -> bool:
        return self._spoke.get(response, False)

    def calls_in(self, response: int) -> int:
        return self._calls.get(response, 0)

    async def wait_ended(self, response: int, timeout: float) -> bool:
        """Wait until `response` has ended at the filter. False if it has not within `timeout` seconds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.ended < response:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                return False
        return True

    def _notify(self) -> None:
        self._changed.set()

    def describe(self) -> str:
        return f"{self.started} response(s), {self.ended} ended, spoke in {sum(self._spoke.values())}"


class SpeechObserver(BaseObserver):
    """Feeds a `SpeechTally` from the frames the LLM service and the spoken-text filter push. Phase 32.

    An observer rather than a hook inside the filter because Pipecat calls
    observers synchronously in ``push_frame``: the LLM's start frame is
    counted the moment the LLM pushes it, before any tool handler can run,
    and the filter's end frame the moment the filter pushes it, after every
    word of that response has gone through. A hook in the filter's own
    processing would lag the LLM by a queue hop — the first cut of this phase
    read "not spoken" because the handler ran before the filter had seen the
    response start.
    """

    def __init__(self, tally: SpeechTally, *, llm: object | None, spoken_text: object) -> None:
        """Create the observer.

        Args:
            tally: The record to keep.
            llm: The LLM service whose pushes number the responses. None means
                any start or calls frame not pushed by the filter counts (a
                test with no LLM service in the pipeline).
            spoken_text: The `SpokenTextFilter` whose pushes say what was spoken.
        """
        super().__init__()
        self._tally = tally
        self._llm = llm
        self._filter = spoken_text
        self._seen_interruptions: set[int] = set()
        # The same frame is pushed once per hop; the LLM's own push is the one
        # that counts, and with no LLM named, the first sighting is.
        self._seen_starts: set[int] = set()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        source = data.source
        if isinstance(frame, InterruptionFrame):
            if frame.id not in self._seen_interruptions:
                self._seen_interruptions.add(frame.id)
                if len(self._seen_interruptions) > 256:
                    self._seen_interruptions = {frame.id}
                self._tally.interrupted()
            return
        if source is self._filter:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._tally.filter_started()
            elif isinstance(frame, LLMTextFrame):
                self._tally.filter_spoken(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._tally.filter_ended()
            return
        if self._llm is not None and source is not self._llm:
            return
        if isinstance(frame, (LLMFullResponseStartFrame, FunctionCallsStartedFrame)):
            if frame.id in self._seen_starts:
                return
            self._seen_starts.add(frame.id)
            if len(self._seen_starts) > 512:
                self._seen_starts = {frame.id}
        if isinstance(frame, LLMFullResponseStartFrame):
            self._tally.llm_started()
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._tally.llm_calls(len(frame.function_calls))


class SpokenTextFilter(FrameProcessor):
    """The pipeline stage between the LLM and the TTS that applies `SpokenTextScrubber`.

    Text frames from the model are rewritten to their speakable part and
    dropped when nothing of them may be spoken. Every other frame passes
    through untouched — `TTSSpeakFrame`s included, since those are written by
    this code, not by the model.
    """

    def __init__(self, *, complete: Callable[[str], str] | None = None, **kwargs) -> None:
        """Create the filter.

        Args:
            complete: Given the finished, scrubbed reply, returns the reply to
                speak. The conversation layer uses it to add a read-back the
                model owed and left out (`SalesConversation.complete_reply`).
                The whole reply is held until the model has finished it, so
                this costs no extra wait. Not called for a reply with nothing
                speakable in it.
        """
        super().__init__(**kwargs)
        self._scrubber = SpokenTextScrubber()
        self._complete = complete
        self._in_response = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Scrub the model's text on its way to the voice."""
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            logger.debug("SPEECH | interrupted: the held reply is discarded")
            self._scrubber.reset()
            self._in_response = False
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._scrubber.reset()
            self._in_response = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            tail = self._scrubber.finish()
            if tail and self._complete is not None:
                try:
                    tail = self._complete(tail)
                except Exception as exc:  # The reply is spoken as the model wrote it.
                    logger.warning(f"SPEECH | completing the reply failed ({exc!r}); spoken as written")
            if tail:
                # The held reply takes the response's own skip-TTS flag: an
                # eval in text mode asks for no voice on every frame, and a
                # reply that ignored that would be synthesised in part — the
                # end frame it ignores is what flushes the last sentence.
                out = LLMTextFrame(text=tail)
                out.skip_tts = frame.skip_tts
                await self.push_frame(out, direction)
            logger.debug(f"SPEECH | response complete: {len(tail)} char(s) released{' (skip_tts)' if frame.skip_tts else ''}")
            stats = self._scrubber.stats
            if stats.removed_anything:
                logger.warning(f"SPEECH | kept from the caller: {stats.describe()}")
            self._in_response = False
        elif isinstance(frame, LLMTextFrame):
            spoken = self._scrubber.feed(frame.text)
            if not spoken:
                return
            if spoken != frame.text:
                frame = _rewritten(frame, spoken)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


def _rewritten(frame: LLMTextFrame, text: str) -> LLMTextFrame:
    """The same frame with different words."""
    out = LLMTextFrame(text=text)
    out.skip_tts = frame.skip_tts
    out.append_to_context = frame.append_to_context
    out.includes_inter_frame_spaces = frame.includes_inter_frame_spaces
    return out


__all__ = ["ScrubStats", "SpokenTextFilter", "SpokenTextScrubber", "strip_speaker_label"]
