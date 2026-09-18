"""Noticing that a machine answered, and deciding what to do about it. Phase 12.

Until this phase an answering machine was a person as far as the agent could
tell: it greeted the voicemail, waited politely through the recorded message,
and pitched fleet tracking to a beep. The call summary flagged "the other end
never spoke" afterwards, which was true and too late.

Two sources of evidence, and they are deliberately independent:

* **What the line sounds like.** A machine's greeting is unlike a person's in
  two measurable ways: it says things people do not say when they pick up
  ("leave a message after the tone", "the person you are calling is not
  available"), and it runs on. A person who answers a cold call says "hello?"
  and stops; a greeting that talks for eight seconds without pausing for an
  answer is a recording. `VoicemailDetector` watches the first caller turns
  for either sign. It needs nothing from the carrier, so it works over
  `tests/fake_carrier.py` and on any carrier at all.
* **What the carrier says.** Twilio and SignalWire can run their own answering
  machine detection when the call is placed and report `answered_by` on the
  call resource. `machine_answered` reads that vocabulary. Nothing here polls
  the carrier — `bot.py` and `campaigns/dialer.py` hand the value in — but the
  verdict it produces is the same shape, so the rest of the system does not
  care which source spoke first.

`VoicemailHandler` is the half with side effects: it takes the verdict and
either ends the call at once or waits for the greeting to finish, leaves a
short message, and ends after the message has played. It talks to the pipeline
through three callables, exactly as `reliability/supervisor.py` does, so it can
be driven in a test with none of Pipecat present.

**What this does not do.** It does not detect the beep. A beep is a tone that
Flux does not transcribe, so the only signals here are words and duration, and
the "leave a message" mode waits a configurable moment after the greeting turn
ends rather than listening for the tone. That is documented on the setting.
And it never fires on a browser or eval session — a person testing in a browser
who reads a voicemail greeting aloud is the one false positive nobody needs —
because `bot.py` builds it only for a phone call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .reliability.observability import event

# Phrases a recorded greeting says and a person answering the phone does not.
# Matched case-insensitively as substrings of the caller's transcript. Kept
# short and specific: "not available" on its own would match "I'm not available
# next week", so every entry here is something only a recording says.
DEFAULT_VOICEMAIL_PHRASES: tuple[str, ...] = (
    "leave a message",
    "leave your message",
    "leave me a message",
    "leave your name and number",
    "after the tone",
    "after the beep",
    "at the tone",
    "at the beep",
    "can't take your call",
    "cannot take your call",
    "can't come to the phone",
    "cannot come to the phone",
    "unable to take your call",
    "not available to take your call",
    "is not available right now",
    "you have reached the voicemail",
    "you've reached the voicemail",
    "you have reached the voice mail",
    "reached the mailbox",
    "voicemail box",
    "voice mailbox",
    "mailbox is full",
    "the person you are calling",
    "the number you have dialed",
    "the number you have dialled",
    "please record your message",
    "record your message",
    "when you have finished recording",
    "to leave a callback number",
)

#: The carrier's `answered_by` vocabulary, normalised. Twilio and SignalWire
#: report `human`, `machine_start`, `machine_end_beep`, `machine_end_silence`,
#: `machine_end_other`, `fax` and `unknown`; anything that starts with
#: `machine` is a machine.
ANSWERED_BY_HUMAN = "human"
ANSWERED_BY_MACHINE = "machine"
ANSWERED_BY_FAX = "fax"
ANSWERED_BY_UNKNOWN = "unknown"

METHOD_PHRASES = "phrases"
METHOD_GREETING_LENGTH = "greeting_length"
METHOD_CARRIER = "carrier_amd"

ACTION_HANGUP = "hangup"
ACTION_MESSAGE = "message"


def normalize_answered_by(value: Any) -> str | None:
    """Read a carrier's `answered_by` field into one of four words, or None.

    None means the carrier said nothing — detection was off, or has not
    finished yet — and is different from `unknown`, which means it ran and
    could not decide. Both leave the call alone; only `machine` acts.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text == ANSWERED_BY_HUMAN:
        return ANSWERED_BY_HUMAN
    if text.startswith(ANSWERED_BY_MACHINE):
        return ANSWERED_BY_MACHINE
    if text == ANSWERED_BY_FAX:
        return ANSWERED_BY_FAX
    return ANSWERED_BY_UNKNOWN


def machine_answered(answered_by: Any) -> bool:
    """Whether a carrier's `answered_by` value means a machine (or a fax) picked up."""
    return normalize_answered_by(answered_by) in (ANSWERED_BY_MACHINE, ANSWERED_BY_FAX)


@dataclass(frozen=True)
class VoicemailVerdict:
    """The decision that a machine answered, and the evidence for it.

    Attributes:
        method: Which detector fired — `phrases`, `greeting_length` or
            `carrier_amd`.
        evidence: What it saw, in a sentence a person can check against the
            transcript or the carrier's record.
        at_secs: Seconds into the call when it was decided.
        transcript: The caller turn that triggered it, when there was one.
    """

    method: str
    evidence: str
    at_secs: float
    transcript: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain data, for the outcome record and the call report."""
        return {
            "detected": True,
            "method": self.method,
            "evidence": self.evidence,
            "at_secs": round(self.at_secs, 1),
            "transcript": self.transcript,
        }


def matched_phrase(text: str, phrases: Sequence[str]) -> str | None:
    """The first voicemail phrase found in `text`, or None."""
    haystack = " ".join(str(text or "").lower().split())
    for phrase in phrases:
        needle = " ".join(phrase.lower().split())
        if needle and needle in haystack:
            return phrase
    return None


class VoicemailDetector:
    """Decides, from the first caller turns, whether a machine answered.

    Pure: it is told what happened and when, and it answers. Every timestamp
    comes from the `clock` it was given, so a test can drive a whole call in
    milliseconds.

    Two rules, both confined to the opening of the call:

    * a caller turn whose transcript contains one of `phrases` is a recording;
    * a caller turn that began **over the agent's greeting** — or before the
      agent had said anything — and has been running for longer than
      `max_greeting_secs` without ending is a recording. A machine does not
      wait for the agent to finish; it starts its greeting the moment the call
      connects and talks through whatever the agent says. A person who lets
      the agent finish and then answers at length is a prospect talking, and
      the length rule leaves them alone however long they go on. Measured on
      this phase's drills: a fast eight-second answer to the agent's opening
      question tripped the rule before it was made to require the overlap.

    Both apply only within `window_secs` of the call connecting and only to
    the first `max_turns` caller turns. A person who launches into a long
    story two minutes in is a prospect talking, not a machine.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        phrases: Sequence[str] = DEFAULT_VOICEMAIL_PHRASES,
        max_greeting_secs: float = 8.0,
        window_secs: float = 30.0,
        max_turns: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the detector.

        Args:
            enabled: False makes every method return None: the detector is
                wired but says nothing.
            phrases: What only a recording says.
            max_greeting_secs: A caller turn longer than this, at the start of
                the call, is a recording. 0 disables the length rule.
            window_secs: Only the first this-many seconds of the call are
                judged.
            max_turns: Only the first this-many caller turns are judged.
            clock: Monotonic seconds. Injected for tests.
        """
        self._enabled = enabled
        self._phrases = tuple(p for p in phrases if p and p.strip())
        self._max_greeting = max(0.0, max_greeting_secs)
        self._window = max(0.0, window_secs)
        self._max_turns = max(1, max_turns)
        self._clock = clock
        self._connected_at: float | None = None
        self._turn_started_at: float | None = None
        self._turn_over_agent = False
        self._turns = 0
        # A caller turn that waited for the agent to finish and had words in
        # it: a person. A machine never waits, so from then on talking over
        # the agent is a barge-in, not a recording, and the length rule is off.
        # Found 2026-09-17: a live caller who answered the greeting and then
        # interrupted for ten seconds was hung up on as a machine.
        self._person_answered = False
        self._verdict: VoicemailVerdict | None = None

    @property
    def verdict(self) -> VoicemailVerdict | None:
        """The decision, once one has been made."""
        return self._verdict

    @property
    def detected(self) -> bool:
        """Whether a machine has been detected."""
        return self._verdict is not None

    def note_connected(self) -> None:
        """The call's audio is flowing. Starts the window."""
        if self._connected_at is None:
            self._connected_at = self._clock()

    def note_user_turn_started(self, *, over_agent_audio: bool = True) -> None:
        """The caller (or the recording) started a turn.

        Args:
            over_agent_audio: Whether the agent was speaking, or had not yet
                spoken, when the turn began. Only such a turn can trip the
                length rule; a turn that waited for the agent to finish is a
                person answering.
        """
        self._turn_started_at = self._clock()
        self._turn_over_agent = over_agent_audio

    def note_user_turn_stopped(self, transcript: str | None) -> VoicemailVerdict | None:
        """The caller's turn ended with this transcript. Returns a verdict if it decides."""
        started = self._turn_started_at
        over_agent = self._turn_over_agent
        self._turn_started_at = None
        self._turn_over_agent = False
        self._turns += 1
        if not self._judging():
            return None
        text = str(transcript or "").strip()
        if text and not over_agent and matched_phrase(text, self._phrases) is None:
            self._person_answered = True
        if text:
            phrase = matched_phrase(text, self._phrases)
            if phrase is not None:
                return self._decide(
                    METHOD_PHRASES,
                    f"the greeting said {phrase!r}",
                    transcript=text,
                )
        if self._max_greeting and started is not None and over_agent and not self._person_answered:
            length = self._clock() - started
            if length > self._max_greeting:
                return self._decide(
                    METHOD_GREETING_LENGTH,
                    f"the first thing said talked over the agent for {length:.1f}s without "
                    f"pausing (limit {self._max_greeting:g}s)",
                    transcript=text or None,
                )
        return None

    def check_open_turn(self) -> VoicemailVerdict | None:
        """A turn still in progress has run too long. Call from a periodic watchdog.

        Deciding *during* the greeting rather than at its end is what lets the
        call be dropped before a thirty-second recording has finished playing.
        """
        if self._turn_started_at is None or not self._max_greeting or not self._turn_over_agent:
            return None
        if self._person_answered:
            return None
        # The open turn is the (turns + 1)th; it must still be within the count.
        if not self._judging(open_turn=True):
            return None
        length = self._clock() - self._turn_started_at
        if length <= self._max_greeting:
            return None
        return self._decide(
            METHOD_GREETING_LENGTH,
            f"the first thing said has talked over the agent for {length:.1f}s without "
            f"pausing (limit {self._max_greeting:g}s)",
        )

    def note_carrier_answered_by(self, answered_by: Any) -> VoicemailVerdict | None:
        """The carrier's own detection reported `answered_by`."""
        if not self._enabled or self._verdict is not None:
            return None
        if not machine_answered(answered_by):
            return None
        normalised = normalize_answered_by(answered_by)
        return self._decide(
            METHOD_CARRIER,
            f"the carrier reported answered_by={str(answered_by).strip().lower()!r} ({normalised})",
        )

    def to_dict(self) -> dict[str, Any]:
        """The verdict as plain data, or the fact that there is none."""
        if self._verdict is None:
            return {"detected": False, "method": None, "evidence": None, "at_secs": None, "transcript": None}
        return self._verdict.to_dict()

    # --- Internals -----------------------------------------------------------

    def _judging(self, *, open_turn: bool = False) -> bool:
        if not self._enabled or self._verdict is not None:
            return False
        turns = self._turns + (1 if open_turn else 0)
        if turns > self._max_turns:
            return False
        if self._connected_at is None:
            return True
        return (self._clock() - self._connected_at) <= self._window

    def _elapsed(self) -> float:
        if self._connected_at is None:
            return 0.0
        return self._clock() - self._connected_at

    def _decide(self, method: str, evidence: str, *, transcript: str | None = None) -> VoicemailVerdict:
        self._verdict = VoicemailVerdict(
            method=method, evidence=evidence, at_secs=self._elapsed(), transcript=transcript
        )
        return self._verdict


class VoicemailHandler:
    """Acts on a voicemail verdict: hang up, or leave a message and then hang up.

    Driven by `bot.py`'s event handlers and its own watchdog, and talking to
    the pipeline through callables so that a test can drive it end to end
    without Pipecat. The same shape as `SessionSupervisor`, and for the same
    reason: an ending decided here still goes through the ordinary teardown,
    so the conversation record and the call result are written.
    """

    def __init__(
        self,
        detector: VoicemailDetector,
        *,
        action: str = ACTION_HANGUP,
        message: str | None = None,
        message_delay_secs: float = 2.0,
        message_grace_secs: float = 20.0,
        end_session: Callable[[], Awaitable[None]],
        cancel_session: Callable[[], Awaitable[None]],
        speak: Callable[[str], Awaitable[None]] | None = None,
        on_detected: Callable[[VoicemailVerdict, str], None] | None = None,
    ) -> None:
        """Create the handler.

        Args:
            detector: What decides. Shared with whoever feeds it events.
            action: `hangup` ends the call the moment a machine is detected;
                `message` waits for the greeting to end, then speaks `message`
                and ends once it has played.
            message: What to say on the machine. Required for `message`.
            message_delay_secs: How long after the greeting's turn ends to
                wait before speaking — the beep is not detected, so this is
                the guess that clears it.
            message_grace_secs: How long to wait for the message to finish
                playing before ending anyway.
            end_session: Ends the session gracefully. `PipelineWorker.stop_when_done`.
            cancel_session: Ends it immediately. `PipelineWorker.cancel`.
            speak: Speaks fixed text. `None` degrades `message` to `hangup`.
            on_detected: Called with the verdict and the action taken, so the
                caller can note it on the call's record.
        """
        self.detector = detector
        self._action = action if action in (ACTION_HANGUP, ACTION_MESSAGE) else ACTION_HANGUP
        self._message = (message or "").strip()
        self._delay = max(0.0, message_delay_secs)
        self._grace = max(0.1, message_grace_secs)
        self._end = end_session
        self._cancel = cancel_session
        self._speak = speak
        self._on_detected = on_detected
        self._acted = False
        self._ended = False
        self._awaiting_greeting_end = False
        self._message_task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._carrier_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        """Whether a machine was detected and the call is being ended because of it."""
        return self._acted

    @property
    def action(self) -> str:
        """What happens on detection: `hangup` or `message`."""
        if self._action == ACTION_MESSAGE and (self._speak is None or not self._message):
            return ACTION_HANGUP
        return self._action

    # --- Feeding it -------------------------------------------------------------

    def on_connected(self) -> None:
        """Audio is flowing. Starts the detector's window and the watchdog."""
        self.detector.note_connected()
        self.start()

    def start(self) -> None:
        """Begin watching for a greeting that runs too long. Idempotent."""
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Stop every background task. Safe to call more than once."""
        for name in ("_watchdog", "_message_task", "_grace_task", "_carrier_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                setattr(self, name, None)

    def on_user_turn_started(self, *, over_agent_audio: bool = True) -> None:
        """The caller started a turn. See `VoicemailDetector.note_user_turn_started`."""
        self.detector.note_user_turn_started(over_agent_audio=over_agent_audio)

    async def on_user_turn_stopped(self, transcript: str | None) -> None:
        """The caller's turn ended. May decide, or may be the greeting ending before a message."""
        verdict = self.detector.note_user_turn_stopped(transcript)
        if verdict is not None:
            # The turn that gave it away has just ended, so the greeting is over.
            await self._act(verdict, greeting_ended=True)
            return
        if self._awaiting_greeting_end:
            self._awaiting_greeting_end = False
            self._schedule_message()

    async def on_carrier_answered_by(self, answered_by: Any) -> None:
        """The carrier's detection reported a value."""
        verdict = self.detector.note_carrier_answered_by(answered_by)
        if verdict is not None:
            await self._act(verdict, greeting_ended=True)

    def watch_carrier(
        self,
        fetch_answered_by: Callable[[], Awaitable[Any]],
        *,
        poll_secs: float = 2.0,
        window_secs: float = 20.0,
    ) -> None:
        """Poll the carrier's `answered_by` for a while after the call connects.

        For carriers whose detection runs asynchronously and reports on the
        call resource: no webhook is needed, only a few reads. Stops at the
        first definite answer, or when the window closes.

        Args:
            fetch_answered_by: Reads the current value; None while undecided.
            poll_secs: Seconds between reads.
            window_secs: Give up after this long.
        """
        if self._carrier_task is not None:
            return
        self._carrier_task = asyncio.create_task(
            self._poll_carrier(fetch_answered_by, max(0.5, poll_secs), max(1.0, window_secs))
        )

    async def on_bot_stopped_speaking(self) -> None:
        """The message has finished playing; end the call."""
        if self._acted and self.action == ACTION_MESSAGE and self._message_task is None and not self._ended:
            await self._finish("voicemail message delivered")

    # --- Acting -----------------------------------------------------------------

    async def _act(self, verdict: VoicemailVerdict, *, greeting_ended: bool = False) -> None:
        """Do what the action says. `greeting_ended` says whether there is still a turn to wait for."""
        if self._acted:
            return
        self._acted = True
        action = self.action
        logger.warning(
            event(
                "voicemail.detected",
                provider=verdict.method,
                latency_ms=int(verdict.at_secs * 1000),
                outcome=action,
                error=verdict.evidence,
            )
        )
        if self._on_detected is not None:
            try:
                self._on_detected(verdict, action)
            except Exception:  # noqa: BLE001 - a note must not stop the ending
                logger.exception("VOICEMAIL | could not note the detection")

        if action == ACTION_HANGUP:
            await self._finish("machine answered; hanging up", graceful=False)
            return

        # Leave a message: wait for the greeting to end, then the beep, then speak.
        if greeting_ended:
            # The turn that gave it away has ended, or the carrier decided and
            # there is no turn boundary to wait for. The delay alone clears the
            # beep.
            self._schedule_message()
        else:
            # Decided mid-greeting by the watchdog: wait for the turn to end.
            # If that never comes — the recording runs on — speak anyway after
            # the grace period rather than sitting silent.
            self._awaiting_greeting_end = True
            self._message_task = asyncio.create_task(self._speak_after(self._grace))

    def _schedule_message(self) -> None:
        if self._message_task is not None:
            self._message_task.cancel()
        self._message_task = asyncio.create_task(self._speak_after(self._delay))

    async def _speak_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._message_task = None
        if self._ended or self._speak is None:
            return
        logger.info(event("voicemail.message", outcome="speaking", latency_ms=int(delay * 1000)))
        try:
            await self._speak(self._message)
        except Exception:  # noqa: BLE001 - if it cannot speak, it still has to end
            logger.exception("VOICEMAIL | could not speak the message")
            await self._finish("voicemail message failed", graceful=False)
            return
        self._grace_task = asyncio.create_task(self._finish_after_grace())

    async def _finish_after_grace(self) -> None:
        try:
            await asyncio.sleep(self._grace)
        except asyncio.CancelledError:
            return
        if not self._ended:
            logger.warning(event("voicemail.message_timeout", outcome=f"ending after {self._grace:g}s anyway"))
            await self._finish("voicemail message did not finish playing", graceful=False)

    async def _finish(self, detail: str, *, graceful: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        self.stop()
        logger.info(event("voicemail.ended", outcome=detail))
        try:
            await (self._end() if graceful else self._cancel())
        except Exception:  # noqa: BLE001 - the session is going away regardless
            logger.exception("VOICEMAIL | could not end the session cleanly")

    async def _watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                if self._acted:
                    return
                verdict = self.detector.check_open_turn()
                if verdict is not None:
                    await self._act(verdict)
                    return
        except asyncio.CancelledError:
            return

    async def _poll_carrier(
        self, fetch: Callable[[], Awaitable[Any]], poll_secs: float, window_secs: float
    ) -> None:
        deadline = time.monotonic() + window_secs
        try:
            while time.monotonic() < deadline and not self._acted:
                try:
                    value = await fetch()
                except Exception as exc:  # noqa: BLE001 - a failed read is not a verdict
                    logger.debug(f"VOICEMAIL | carrier answered_by read failed: {exc}")
                    value = None
                normalised = normalize_answered_by(value)
                if normalised is not None:
                    logger.info(event("voicemail.carrier_answered_by", outcome=normalised))
                    if normalised != ANSWERED_BY_UNKNOWN:
                        await self.on_carrier_answered_by(value)
                        return
                await asyncio.sleep(poll_secs)
        except asyncio.CancelledError:
            return
        finally:
            self._carrier_task = None


__all__ = [
    "ACTION_HANGUP",
    "ACTION_MESSAGE",
    "ANSWERED_BY_HUMAN",
    "ANSWERED_BY_MACHINE",
    "ANSWERED_BY_UNKNOWN",
    "DEFAULT_VOICEMAIL_PHRASES",
    "METHOD_CARRIER",
    "METHOD_GREETING_LENGTH",
    "METHOD_PHRASES",
    "VoicemailDetector",
    "VoicemailHandler",
    "VoicemailVerdict",
    "machine_answered",
    "matched_phrase",
    "normalize_answered_by",
]
