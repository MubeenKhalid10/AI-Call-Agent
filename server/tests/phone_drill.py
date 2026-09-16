#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Scripted callers that stress the phone path, with no carrier account. Phase 12.

Run it from the `server/` directory against a running bot::

    uv run bot.py                                            # terminal 1
    uv run python tests/phone_drill.py barge_in              # terminal 2
    uv run python tests/phone_drill.py all --record drills

`tests/fake_carrier.py` proved the plumbing: the handshake, 8kHz μ-law both
ways, the greeting. This builds on it and proves *behaviour* under the
conditions a real caller creates and the eval harness cannot: it speaks the
caller's lines with Kokoro (the local voice the evals already use), sends them
over the carrier's own wire protocol in real time, listens for what the bot
does at the wire level, and then reads the bot's own report of the call — the
JSON `bot.py` writes to `CALL_REPORT_DIR` — to check what happened inside.

The drills, each a separate call:

    short_answers   "Yes." "No, not really." "Okay." — every one must get a reply
    barge_in        interrupt mid-sentence; the bot must stop fast and answer the new thing
    overlap         interrupt and keep talking for seconds; the bot must wait, not talk over
    pauses          a sentence with a 0.7s hole in it must arrive as one turn
    rapid           a long sentence at 1.35x speed must arrive whole
    noise           steady background noise must not cut the bot off or lose turns
    voicemail       a recorded greeting and a beep; the bot must notice and hang up

Each drill prints PASS/FAIL lines and the numbers behind them (how fast the
bot stopped, the per-turn latency, what was transcribed). Exit status is 0
when every check in every drill passed.

What this cannot tell you: how a real line, a real handset and a real accent
transcribe. That is `tests/live_call.py`, which places a real call and reads
the same report.

The first run of a Kokoro voice synthesises and caches each line under
`~/.cache/pipecat`; later runs are instant. Kokoro's own model (~310 MB) is
downloaded once by the evals or by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import numpy as np  # noqa: E402
import websockets  # noqa: E402
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm  # noqa: E402

from fake_carrier import (  # noqa: E402
    AUDIBLE_PEAK,
    CARRIER_RATE,
    FRAME_MS,
    FRAME_SAMPLES,
    FakeCall,
    _peak_amplitude,
    _write_wav,
)
from src.telephony.session import DIRECTION_OUTBOUND  # noqa: E402
from src.voice_quality import load_call_report  # noqa: E402

DRILLS = ("short_answers", "barge_in", "overlap", "pauses", "rapid", "noise", "voicemail")

# The caller's lines, per drill. Synthesised once each and cached.
LINES = {
    # Neutral one-to-three-word answers: none of them is a no, a callback
    # request or a goodbye, any of which the playbook is allowed to act on by
    # ending the call. ("No, not really" ended the first run of this drill.)
    "hello": "Hello?",
    "go_ahead": "Sure, go ahead.",
    "okay": "Okay.",
    "tell_me": "Tell me a bit about what you do and how it works.",
    "who_with": "Sorry, hang on. Who did you say you were with?",
    "long_interrupt": (
        "Sorry, hang on a second, I've got someone at the door, and actually I can't really "
        "talk right now, so could you do me a favour and call me back on my office number "
        "tomorrow morning instead?"
    ),
    "pause_a": "We run about forty trucks out of Lahore,",
    "pause_b": "mostly long haul, and half of them are refrigerated.",
    "rapid": (
        "Right so quickly we've got forty trucks running out of Lahore, half of them "
        "refrigerated, fuel is by far our biggest cost, I'm the one who signs off on "
        "suppliers, so what exactly does your tracking actually do?"
    ),
    "hi_sarah": "Hi, yes, this is Sarah.",
    "forty": "We run about forty trucks, mostly long haul.",
    "voicemail": (
        "Hi, you've reached Sarah Khan at Ravi Logistics. I can't take your call right now, "
        "so please leave a message after the tone and I'll get back to you."
    ),
}


class Speech:
    """Kokoro, at the carrier's rate. The same model the eval harness uses."""

    def __init__(self, voice: str = "af_heart") -> None:
        from kokoro_onnx import Kokoro
        from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR, _ensure_model_files

        model = KOKORO_CACHE_DIR / "kokoro-v1.0.onnx"
        voices = KOKORO_CACHE_DIR / "voices-v1.0.bin"
        _ensure_model_files(model, voices)
        self._kokoro = Kokoro(str(model), str(voices))
        self._voice = voice
        self._resampler = create_stream_resampler()
        self._cache_dir = Path.home() / ".cache" / "pipecat" / "phone-drill"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def say(self, text: str, *, speed: float = 1.0) -> bytes:
        """Return `text` as 8kHz mono 16-bit PCM."""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        key = f"{self._voice}-{speed:g}-{digest}.pcm"
        cached = self._cache_dir / key
        if cached.exists():
            return cached.read_bytes()
        samples, rate = await asyncio.to_thread(
            self._kokoro.create, text, voice=self._voice, speed=speed, lang="en-us"
        )
        pcm24 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        pcm8 = await self._resampler.resample(pcm24, rate, CARRIER_RATE)
        cached.write_bytes(pcm8)
        return pcm8


def beep(secs: float = 0.5, hz: float = 1000.0, db: float = -6.0) -> bytes:
    """A voicemail beep: a sine at `hz` for `secs`."""
    n = int(CARRIER_RATE * secs)
    t = np.arange(n) / CARRIER_RATE
    amplitude = 32767 * 10 ** (db / 20)
    return (np.sin(2 * math.pi * hz * t) * amplitude).astype(np.int16).tobytes()


def white_noise(secs: float, db: float, seed: int = 1) -> np.ndarray:
    """Steady background noise at `db` dBFS, as int16 samples."""
    amplitude = 32767 * 10 ** (db / 20)
    noise = np.random.default_rng(seed).standard_normal(int(CARRIER_RATE * secs))
    return np.clip(noise * amplitude, -32768, 32767).astype(np.int16)


class DrillCall(FakeCall):
    """One scripted call over the fake carrier's wire protocol."""

    def __init__(self, args: argparse.Namespace, script: Callable[[DrillCall], Awaitable[None]], *, noise_db: float | None) -> None:
        super().__init__(args)
        self._script = script
        self._outgoing = bytearray()
        self._noise = white_noise(10.0, noise_db) if noise_db is not None else None
        self._noise_at = 0
        self._last_audible_at: float | None = None
        self._speaking_since: float | None = None
        self._speaking_since_call: float | None = None
        self._audible_frames: list[float] = []
        self._clears: list[float] = []
        self._closed_at: float | None = None
        self._closed_by_bot = False
        self.notes: list[str] = []
        self.marks: dict[str, float] = {}
        self.line_starts: list[float] = []

    # --- Timing -----------------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self._started

    def mark(self, name: str) -> None:
        self.marks[name] = self.now()

    # --- What the script can do ------------------------------------------------

    async def say(self, pcm: bytes) -> float:
        """Queue speech and wait until it has all gone down the wire. Returns when it finished."""
        self.line_starts.append(self.now())
        self._outgoing.extend(pcm)
        while self._outgoing and self._closed_at is None:
            await asyncio.sleep(0.02)
        return self.now()

    async def silence(self, secs: float) -> None:
        end = time.monotonic() + secs
        while time.monotonic() < end and self._closed_at is None:
            await asyncio.sleep(0.02)

    async def wait_bot_quiet(self, *, after: float = 0.0, min_quiet: float = 1.5, timeout: float = 60.0) -> bool:
        """Wait until the bot has spoken since `after` and then been quiet for `min_quiet`.

        Generous by default: on the free Groq tier a throttled turn takes tens
        of seconds, and the drill should measure that rather than give up on
        it. `min_quiet` is longer than the gap the TTS leaves between two
        sentences of one reply (measured at just under a second), so the
        caller does not talk into the middle of a reply by accident. False on
        timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._closed_at is None:
            if self.first_audible_after(after) is not None and self._last_audible_at is not None:
                if time.monotonic() - self._last_audible_at >= min_quiet:
                    return True
            await asyncio.sleep(0.05)
        self.notes.append(f"waited {timeout:g}s for the bot to reply after {after:.1f}s")
        return False

    async def wait_bot_speaking(self, *, after: float = 0.0, min_secs: float = 1.5, timeout: float = 60.0) -> bool:
        """Wait until the bot has been speaking continuously for `min_secs`, starting after `after`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._closed_at is None:
            since = self._speaking_since_call
            if (
                since is not None
                and since >= after
                and self.now() - since >= min_secs
                and self._last_audible_at is not None
                and time.monotonic() - self._last_audible_at < 0.3
            ):
                return True
            await asyncio.sleep(0.02)
        self.notes.append(f"waited {timeout:g}s for the bot to speak for {min_secs:g}s")
        return False

    async def wait_closed(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._closed_at is None:
            await asyncio.sleep(0.05)
        return self._closed_at is not None

    def bot_audio_between(self, start: float, end: float) -> float:
        """Seconds of audible bot audio between two call-relative times."""
        frames = sum(1 for t in self._audible_frames if start <= t <= end)
        return frames * FRAME_MS / 1000

    def last_audible_before(self, moment: float) -> float | None:
        before = [t for t in self._audible_frames if t <= moment]
        return before[-1] if before else None

    def first_audible_after(self, moment: float) -> float | None:
        after = [t for t in self._audible_frames if t >= moment]
        return after[0] if after else None

    # --- The call -----------------------------------------------------------------

    async def run(self) -> int:  # type: ignore[override]
        """Place the simulated call and run the script."""
        print(f"Connecting to {self._args.url} as a {self._args.direction} call")
        print(f"  call={self._call_sid}\n  stream={self._stream_sid}")
        try:
            async with websockets.connect(self._args.url) as websocket:
                await self._handshake(websocket)
                self._started = time.monotonic()
                listener = asyncio.create_task(self._listen_forever(websocket))
                pump = asyncio.create_task(self._pump(websocket))
                try:
                    await asyncio.wait_for(self._script(self), timeout=self._args.max_secs)
                except TimeoutError:
                    self.notes.append(f"the script did not finish within {self._args.max_secs:g}s")
                except websockets.exceptions.ConnectionClosed:
                    pass
                finally:
                    pump.cancel()
                    if self._closed_at is None:
                        try:
                            await websocket.send(json.dumps({"event": "stop", "streamSid": self._stream_sid}))
                        except websockets.exceptions.ConnectionClosed:
                            pass
                    listener.cancel()
        except OSError as exc:
            print(f"\nCould not reach {self._args.url}: {exc}\nIs the bot running?", file=sys.stderr)
            return 1
        return 0

    async def _pump(self, websocket) -> None:
        """Send one 20ms frame every 20ms: queued speech, else silence, plus any noise."""
        frame_bytes = FRAME_SAMPLES * 2
        next_at = time.monotonic()
        try:
            while True:
                if self._outgoing:
                    chunk = bytes(self._outgoing[:frame_bytes])
                    del self._outgoing[:frame_bytes]
                    if len(chunk) < frame_bytes:
                        chunk += b"\x00" * (frame_bytes - len(chunk))
                else:
                    chunk = b"\x00" * frame_bytes
                if self._noise is not None:
                    chunk = self._mix_noise(chunk)
                payload = await pcm_to_ulaw(chunk, CARRIER_RATE, CARRIER_RATE, self._to_bot)
                self._sequence += 1
                await websocket.send(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "sequenceNumber": str(self._sequence),
                            "media": {
                                "track": "inbound",
                                "chunk": str(self._sequence),
                                "timestamp": str(self._sequence * FRAME_MS),
                                "payload": base64.b64encode(payload).decode(),
                            },
                        }
                    )
                )
                next_at += FRAME_MS / 1000
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            return

    def _mix_noise(self, chunk: bytes) -> bytes:
        assert self._noise is not None
        n = len(chunk) // 2
        if self._noise_at + n > len(self._noise):
            self._noise_at = 0
        noise = self._noise[self._noise_at : self._noise_at + n]
        self._noise_at += n
        speech = np.frombuffer(chunk, dtype=np.int16).astype(np.int32)
        mixed = np.clip(speech + noise.astype(np.int32), -32768, 32767).astype(np.int16)
        return mixed.tobytes()

    async def _listen_forever(self, websocket) -> None:
        try:
            async for raw in websocket:
                await self._handle(raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            return
        if self._closed_at is None:
            self._closed_at = self.now()
            self._closed_by_bot = True
            print(f"  [{self._closed_at:5.1f}s] the bot closed the connection")

    async def _handle(self, raw: str | bytes) -> None:  # type: ignore[override]
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        event = str(message.get("event", "?"))
        self._events[event] = self._events.get(event, 0) + 1
        now = self.now()
        if event == "clear":
            self._clears.append(now)
            print(f"  [{now:5.1f}s] bot cleared buffered audio")
            return
        if event != "media":
            return
        audio = base64.b64decode(message["media"]["payload"])
        pcm = await ulaw_to_pcm(audio, CARRIER_RATE, CARRIER_RATE, self._from_bot)
        self._heard.extend(pcm)
        peak = _peak_amplitude(pcm)
        self._peak = max(self._peak, peak)
        if peak > AUDIBLE_PEAK:
            if self._first_audio_at is None:
                self._first_audio_at = time.monotonic()
                print(f"  [{now:5.1f}s] the bot started speaking")
            if self._last_audible_at is None or time.monotonic() - self._last_audible_at > 0.5:
                self._speaking_since = time.monotonic()
                self._speaking_since_call = now
            self._last_audible_at = time.monotonic()
            self._audible_frames.append(now)

    def save_recording(self, path: str) -> None:
        if self._heard:
            _write_wav(path, bytes(self._heard))
            print(f"  saved {path}")


# --- The drills -------------------------------------------------------------------


class Drill:
    """One named drill: its lines, its script, and its checks."""

    def __init__(self, name: str, *, noise_db: float | None = None) -> None:
        self.name = name
        self.noise_db = noise_db
        self.lines: dict[str, bytes] = {}
        self.results: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.results.append((label, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")

    async def synthesise(self, speech: Speech) -> None:
        if self.name == "short_answers":
            keys = ("hello", "go_ahead", "okay")
        elif self.name in ("barge_in",):
            keys = ("tell_me", "who_with")
        elif self.name == "overlap":
            keys = ("tell_me", "long_interrupt")
        elif self.name == "pauses":
            keys = ("pause_a", "pause_b")
        elif self.name == "rapid":
            keys = ()
            self.lines["rapid"] = await speech.say(LINES["rapid"], speed=1.35)
        elif self.name == "noise":
            keys = ("hi_sarah", "forty")
        else:
            keys = ("voicemail",)
        for key in keys:
            self.lines[key] = await speech.say(LINES[key])

    async def script(self, call: DrillCall) -> None:
        lines = self.lines
        if self.name == "short_answers":
            spoke_at = 0.0
            for key in ("hello", "go_ahead", "okay"):
                await call.wait_bot_quiet(after=spoke_at)
                await call.silence(0.4)
                call.mark(f"said_{key}")
                spoke_at = await call.say(lines[key])
            await call.wait_bot_quiet(after=spoke_at)
            await call.silence(1.0)
        elif self.name in ("barge_in", "overlap"):
            await call.wait_bot_quiet()
            await call.silence(0.4)
            asked_at = await call.say(lines["tell_me"])
            spoke = await call.wait_bot_speaking(after=asked_at, min_secs=1.5)
            call.mark("interrupt")
            if not spoke:
                call.notes.append("the bot never spoke long enough to interrupt")
            spoke_at = await call.say(lines["who_with" if self.name == "barge_in" else "long_interrupt"])
            call.mark("interrupt_end")
            await call.wait_bot_quiet(after=spoke_at, min_quiet=1.2)
            await call.silence(1.0)
        elif self.name == "pauses":
            await call.wait_bot_quiet()
            await call.silence(0.4)
            call.mark("turn_start")
            await call.say(lines["pause_a"])
            await call.silence(0.7)
            spoke_at = await call.say(lines["pause_b"])
            await call.wait_bot_quiet(after=spoke_at)
            await call.silence(1.0)
        elif self.name == "rapid":
            await call.wait_bot_quiet()
            await call.silence(0.4)
            spoke_at = await call.say(lines["rapid"])
            await call.wait_bot_quiet(after=spoke_at)
            await call.silence(1.0)
        elif self.name == "noise":
            await call.wait_bot_quiet()
            await call.silence(0.4)
            spoke_at = await call.say(lines["hi_sarah"])
            await call.wait_bot_quiet(after=spoke_at)
            await call.silence(0.4)
            spoke_at = await call.say(lines["forty"])
            await call.wait_bot_quiet(after=spoke_at)
            await call.silence(2.0)
        elif self.name == "voicemail":
            # A machine does not wait for the caller. Greeting, beep, silence.
            await call.silence(0.5)
            call.mark("greeting")
            await call.say(lines["voicemail"])
            call.mark("greeting_end")
            await call.say(beep())
            call.mark("beep")
            closed = await call.wait_closed(30.0)
            if not closed:
                call.notes.append("the bot did not hang up within 30s of the greeting")

    def evaluate(self, call: DrillCall, report: dict | None) -> None:
        print(f"\n--- {self.name}: what the carrier saw ---")
        print(f"  events {call._events or 'none'} | bot audio {len(call._heard) / (CARRIER_RATE * 2):.1f}s | clears at {[round(t, 1) for t in call._clears]}")
        for note in call.notes:
            print(f"  note: {note}")
        print(f"--- {self.name}: checks ---")
        if self.name == "voicemail":
            # A recording talks over the greeting from the first second, so the
            # agent may be cut off before its first audio frame. Not a failure.
            print(f"  bot audio peak {call._peak} (a greeting cut off by the recording is expected)")
        else:
            self.check("the bot answered and spoke", call._peak > AUDIBLE_PEAK, f"peak {call._peak}")
        self.check("the bot wrote its report", report is not None, "set CALL_REPORT_DIR and check the bot log" if report is None else "")
        if report is None:
            return
        turns = report.get("turns", [])
        spoken = [t for t in turns if t.get("transcript")]
        print("  transcripts: " + " | ".join(repr(t["transcript"]) for t in spoken) if spoken else "  transcripts: none")
        for record in report.get("latency", {}).get("per_response", []):
            print(f"  latency response {record['response']}: total {record['total_ms']}ms (turn-end {record['turn_end_ms']} · llm {record['llm_first_token_ms']} · tts {record['tts_first_audio_ms']})")
        self._detection_lag(call, report)
        failed = report.get("failed_turn_count", 0)
        late = report.get("late_turn_count", 0)
        errors = report.get("errors", [])
        self.check("no failed turns", failed == 0, f"{failed} failed: {[t.get('failed') for t in report.get('failed_turns', [])]}")
        if late:
            print(f"  note: {late} turn(s) answered late (after TURN_RESPONSE_TIMEOUT_SECS) — a throttled LLM, most likely")
        self.check("no service errors", not errors, str(errors)[:120])

        if self.name == "short_answers":
            self.check("three short answers were heard", len(spoken) >= 3, f"{len(spoken)} turn(s) with words")
            self.check("every one of them got a reply", all(t.get("responded") for t in spoken), str([t.get("responded") for t in spoken]))
        elif self.name in ("barge_in", "overlap"):
            barge_ins = report.get("barge_ins", [])
            self.check("the interruption was seen as a barge-in", len(barge_ins) >= 1, f"{len(barge_ins)} barge-in(s)")
            interrupt_at = call.marks.get("interrupt")
            if interrupt_at is not None:
                cleared = [t for t in call._clears if t >= interrupt_at - 0.1]
                self.check("the bot told the carrier to drop buffered audio", bool(cleared), f"clears after interrupt: {[round(t - interrupt_at, 2) for t in cleared]}")
                last = call.last_audible_before(interrupt_at + 3.0)
                stop_ms = None if last is None else int((last - interrupt_at) * 1000)
                self.check("the bot's audio stopped within 1.5s of the interruption", stop_ms is not None and stop_ms <= 1500, f"last audible frame {stop_ms}ms after the interruption began")
            real = [b for b in barge_ins if b.get("stop_latency_ms") is not None]
            if real:
                self.check("the pipeline stopped the bot within 1.5s", max(b["stop_latency_ms"] for b in real) <= 1500, f"stop latency {[b['stop_latency_ms'] for b in real]}ms")
            self.check("no interruption was taken for noise", not any(b.get("spurious") for b in barge_ins), str([b.get("spurious") for b in barge_ins]))
            interrupted = [t for t in turns if t.get("interrupted_bot") and t.get("transcript")]
            self.check("the interrupting turn was transcribed and answered", bool(interrupted) and all(t.get("responded") for t in interrupted), str([(t.get("transcript"), t.get("responded")) for t in interrupted])[:160])
            if self.name == "overlap":
                start, end = call.marks.get("interrupt"), call.marks.get("interrupt_end")
                if start is not None and end is not None and end - start > 2.0:
                    talked_over = call.bot_audio_between(start + 1.5, end)
                    self.check("the bot did not talk over the caller", talked_over < 0.5, f"{talked_over:.1f}s of bot audio while the caller was still speaking")
        elif self.name == "pauses":
            both = [t for t in spoken if "forty" in t["transcript"].lower() and "refrigerated" in t["transcript"].lower()]
            self.check("the sentence with a pause in it arrived as one turn", bool(both), f"{len(spoken)} turn(s): " + " | ".join(repr(t["transcript"]) for t in spoken)[:200])
            self.check("and it was answered", bool(both) and both[0].get("responded"))
        elif self.name == "rapid":
            keywords = ("forty", "lahore", "refrigerated", "fuel", "suppliers", "tracking")
            text = " ".join(t["transcript"].lower() for t in spoken)
            heard = [k for k in keywords if k in text]
            self.check("the fast sentence arrived whole", len(heard) >= 4, f"heard {heard}")
            self.check("as one turn, not several", len(spoken) == 1, f"{len(spoken)} turn(s)")
            self.check("and was answered", bool(spoken) and spoken[0].get("responded"))
        elif self.name == "noise":
            self.check("the noise never cut the bot off", report.get("spurious_interruptions", 0) == 0 and report.get("barge_in_count", 0) == 0, f"{report.get('barge_in_count')} barge-in(s), {report.get('spurious_interruptions')} spurious")
            self.check("both sentences were heard through it", len(spoken) >= 2, f"{len(spoken)} turn(s) with words")
            self.check("and answered", all(t.get("responded") for t in spoken), str([t.get("responded") for t in spoken]))
        elif self.name == "voicemail":
            verdict = report.get("voicemail", {})
            self.check("the machine was detected", verdict.get("detected") is True, str(verdict)[:160])
            self.check("by what it said or how long it ran", verdict.get("method") in ("phrases", "greeting_length"), str(verdict.get("method")))
            self.check("the call was ended because of it", report.get("ended_by") == "voicemail", str(report.get("ended_by")))
            greeting = call.marks.get("greeting", 0.0)
            self.check("and the bot hung up on the recording", call._closed_by_bot and call._closed_at is not None and call._closed_at - greeting <= 25.0, f"closed at {call._closed_at}s" if call._closed_at else "the connection was never closed by the bot")

    def _detection_lag(self, call: DrillCall, report: dict) -> None:
        """How long after the caller started talking the bot noticed, per turn.

        The measurement that exposed the audio backlog: Flux reporting the
        start of a turn seconds after the words began. Anchored on the
        greeting, which both clocks saw.
        """
        greeting_bot = report.get("greeting_at_secs")
        first_audio = call._first_audio_at
        if greeting_bot is None or first_audio is None or not call.line_starts:
            return
        offset = (first_audio - call._started) - greeting_bot  # bot clock + offset = drill clock
        detected = [t["started_at_secs"] + offset for t in report.get("turns", []) if t.get("started_at_secs") is not None]
        lags = []
        for said_at in call.line_starts:
            after = [d for d in detected if d >= said_at - 0.3]
            if after:
                lags.append(int((after[0] - said_at) * 1000))
        if not lags:
            return
        print(f"  turn-start detection lag: {lags} ms after the caller began each line")
        self.check("the bot noticed each line within 1.5s of it starting", max(lags) <= 1500, f"{lags} ms")

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.results)


async def wait_for_report(directory: str, call_id: str, timeout: float = 25.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = load_call_report(directory, call_id)
        if report is not None:
            return report
        await asyncio.sleep(0.5)
    return None


async def run_drill(name: str, args: argparse.Namespace, speech: Speech) -> Drill:
    print(f"\n=== drill: {name} ===")
    drill = Drill(name, noise_db=args.noise_db if name == "noise" else None)
    await drill.synthesise(speech)
    call_args = SimpleNamespace(
        url=args.url,
        direction=DIRECTION_OUTBOUND,
        from_number=args.from_number,
        to_number=args.to_number,
        prospect=None,
        campaign=None,
        attempt=None,
        seconds=args.max_secs,
        say=None,
        record=None,
        max_secs=args.max_secs,
    )
    call = DrillCall(call_args, drill.script, noise_db=drill.noise_db)
    status = await call.run()
    if status != 0:
        drill.check("the bot was reachable", False, "connection failed")
        return drill
    if args.record:
        Path(args.record).mkdir(parents=True, exist_ok=True)
        call.save_recording(str(Path(args.record) / f"{name}.wav"))
    report = await wait_for_report(args.report_dir, call._call_sid)
    drill.evaluate(call, report)
    return drill


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone_drill.py",
        description="Scripted callers over the carrier's wire protocol, against a running bot.",
    )
    parser.add_argument("drill", choices=(*DRILLS, "all"), help="which drill to run")
    parser.add_argument("--url", default="ws://localhost:7860/ws", help="the bot's telephony websocket")
    parser.add_argument("--report-dir", default="call-reports", help="the bot's CALL_REPORT_DIR (default call-reports)")
    parser.add_argument("--record", metavar="DIR", help="save what the bot said, one WAV per drill")
    parser.add_argument("--noise-db", type=float, default=-28.0, help="background noise level for the noise drill, in dBFS (default -28)")
    parser.add_argument("--max-secs", type=float, default=150.0, help="give up on a drill after this long (default 150; a throttled LLM turn can take a minute)")
    parser.add_argument("--voice", default="af_heart", help="Kokoro voice for the caller (default af_heart)")
    parser.add_argument("--from", dest="from_number", default="+15550001111", help="caller ID")
    parser.add_argument("--to", dest="to_number", default="+923001234567", help="number dialled")
    parser.add_argument("--pause", type=float, default=3.0, help="seconds between drills when running all (default 3)")
    return parser


async def main() -> int:
    args = _parser().parse_args()
    names = list(DRILLS) if args.drill == "all" else [args.drill]
    print("Loading Kokoro...")
    speech = Speech(args.voice)
    drills = []
    for index, name in enumerate(names):
        drills.append(await run_drill(name, args, speech))
        if index + 1 < len(names):
            await asyncio.sleep(args.pause)

    print("\n=== summary ===")
    failed = 0
    for drill in drills:
        bad = [label for label, ok, _ in drill.results if not ok]
        failed += len(bad)
        print(f"  {'PASS' if drill.passed else 'FAIL'}  {drill.name}" + (f"  -- {', '.join(bad)}" if bad else ""))
    if failed:
        print(f"\n{failed} check(s) FAILED.")
        return 1
    print("\nAll drills passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
