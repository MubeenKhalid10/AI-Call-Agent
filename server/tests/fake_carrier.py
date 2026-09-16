#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pretend to be a phone carrier, so the telephony path can be tested for free.

Run it from the `server/` directory against a running bot::

    uv run bot.py                                   # terminal 1
    uv run python tests/fake_carrier.py             # terminal 2

**What this is for.** Everything else about a phone call can be checked without
a carrier — `tests/test_telephony.py` covers placing the call and reading its
outcome — but the audio cannot, and the audio is where the interesting failures
live. This connects to the bot's `/ws` endpoint and speaks Twilio's Media
Streams protocol at it: the `connected` and `start` handshake with the same
custom parameters `call.py` attaches, then 8kHz μ-law frames, exactly as a
carrier would. The bot cannot tell the difference, which is the point.

It answers, for free and in fifteen seconds, the questions that otherwise need
an account and a phone number:

* Does the handshake parse, and does the bot know who it is talking to?
* Does the bot **speak first**? On a phone call this hangs off a different event
  than in a browser, and getting it wrong is silent — see `bot.py`'s
  `greet_once`. This is the check for that.
* Does audio come back, encoded correctly, loud enough to be heard?
* Do the call-level log lines and the end-of-call summary appear?

It does **not** replace a real call. It cannot tell you whether your carrier
accepts the TwiML, whether a real number rings, or how the agent sounds down a
real line with a real person's accent — a genuine 8kHz phone line is a harsher
input than anything here. Make a real call before trusting any of that.

Options::

    --url ws://localhost:7860/ws    where the bot is listening
    --seconds 15                    how long to stay on the "call"
    --say caller.wav                stream this WAV as the caller instead of silence
    --record heard.wav              save what the bot said, to listen back
    --inbound                       present as an inbound call rather than one we placed

Exit status is 0 when the bot spoke.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
import wave
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import websockets  # noqa: E402
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm  # noqa: E402

from src.conversation.sources import (  # noqa: E402
    PARAM_ATTEMPT_ID,
    PARAM_CAMPAIGN_ID,
    PARAM_PROSPECT_ID,
)
from src.telephony.session import (  # noqa: E402
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    PARAM_DIRECTION,
    PARAM_FROM,
    PARAM_TO,
)

# What a carrier's media stream runs at. Both Twilio and SignalWire default
# here, and the bot's serializer resamples to the pipeline's rate either way.
CARRIER_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = CARRIER_RATE * FRAME_MS // 1000

# Below this the "audio" the bot sent back is silence or noise, not speech.
AUDIBLE_PEAK = 500


class FakeCall:
    """One simulated call: the carrier's side of a Media Streams websocket."""

    def __init__(self, args: argparse.Namespace) -> None:
        """Create the simulated call from parsed command-line arguments."""
        self._args = args
        self._stream_sid = f"MZ{uuid.uuid4().hex}"
        self._call_sid = f"CA{uuid.uuid4().hex}"
        self._sequence = 0
        self._events: dict[str, int] = {}
        self._heard = bytearray()
        self._peak = 0
        self._first_audio_at: float | None = None
        self._started = 0.0
        self._to_bot = create_stream_resampler()
        self._from_bot = create_stream_resampler()

    async def run(self) -> int:
        """Place the simulated call and report what happened."""
        print(f"Connecting to {self._args.url} as a {self._args.direction} call")
        print(f"  call={self._call_sid}\n  stream={self._stream_sid}")

        async with websockets.connect(self._args.url) as websocket:
            await self._handshake(websocket)
            self._started = time.monotonic()

            speaking = asyncio.create_task(self._speak(websocket))
            try:
                await self._listen(websocket)
            finally:
                speaking.cancel()

            # A carrier tells the bot the stream is over before dropping the
            # socket; the bot treats it as the caller hanging up either way, but
            # this exercises the polite path.
            await websocket.send(json.dumps({"event": "stop", "streamSid": self._stream_sid}))

        return self._report()

    async def _handshake(self, websocket) -> None:
        """Send the two messages that open a Media Streams call.

        The shape here is Twilio's, which is also SignalWire's — Pipecat detects
        the carrier from `event`, `start.streamSid` and `start.callSid`, and
        those three fields are identical between them.

        `customParameters` is how an outbound call tells the bot who it dialled;
        `call.py` puts the same three keys in its TwiML, and `session.py` reads
        them back. Sending them here is what makes the bot's log line look like
        a real outbound call's.
        """
        await websocket.send(
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        )
        await websocket.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": self._stream_sid,
                    "start": {
                        "streamSid": self._stream_sid,
                        "accountSid": "ACfakecarrier",
                        "callSid": self._call_sid,
                        "tracks": ["inbound"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": CARRIER_RATE,
                            "channels": 1,
                        },
                        "customParameters": {
                            PARAM_DIRECTION: self._args.direction,
                            PARAM_FROM: self._args.from_number,
                            PARAM_TO: self._args.to_number,
                            # The campaign ids `dialer.py` attaches to a real
                            # call, when asked for. With them the bot resolves
                            # the prospect and writes the call's outcome and
                            # result (Phase 8) onto the attempt row.
                            **{
                                name: str(value)
                                for name, value in (
                                    (PARAM_PROSPECT_ID, self._args.prospect),
                                    (PARAM_CAMPAIGN_ID, self._args.campaign),
                                    (PARAM_ATTEMPT_ID, self._args.attempt),
                                )
                                if value is not None
                            },
                        },
                    },
                }
            )
        )

    async def _speak(self, websocket) -> None:
        """Stream the caller's audio, in real time.

        Real time matters: the bot's VAD and Deepgram Flux both judge from the
        rate audio arrives at, so blasting a file down the socket as fast as it
        will go produces turn-taking behaviour that no real call would show.
        """
        pcm = _read_wav(self._args.say) if self._args.say else b""
        frame_bytes = FRAME_SAMPLES * 2
        offset = 0
        deadline = time.monotonic() + self._args.seconds

        while time.monotonic() < deadline:
            if pcm:
                chunk = pcm[offset : offset + frame_bytes]
                offset += frame_bytes
                if len(chunk) < frame_bytes:
                    # Past the end of the file: hold the line open in silence
                    # rather than hanging up, so the bot's reply can be heard.
                    chunk = b"\x00" * frame_bytes
            else:
                chunk = b"\x00" * frame_bytes

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
            await asyncio.sleep(FRAME_MS / 1000)

    async def _listen(self, websocket) -> None:
        """Collect what the bot sends back until the call's time is up."""
        deadline = self._started + self._args.seconds

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                # A short timeout with `continue`, not one long one: the bot
                # takes a couple of seconds to open websockets to three vendors
                # before it can say anything, and a receive loop that gives up
                # on the first quiet moment reports "no audio" from a bot that
                # is working perfectly.
                raw = await asyncio.wait_for(websocket.recv(), timeout=min(1.0, remaining))
            except TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                print("  the bot closed the connection")
                return

            await self._handle(raw)

    async def _handle(self, raw: str | bytes) -> None:
        """Record one message from the bot."""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            self._events["<not json>"] = self._events.get("<not json>", 0) + 1
            return

        event = str(message.get("event", "?"))
        self._events[event] = self._events.get(event, 0) + 1

        if event == "clear":
            # The bot was interrupted and is telling the carrier to drop audio
            # it has buffered but not yet played. Worth seeing in the output:
            # it is barge-in working at the wire level.
            print(f"  [{time.monotonic() - self._started:5.1f}s] bot cleared buffered audio")
            return

        if event != "media":
            return

        audio = base64.b64decode(message["media"]["payload"])
        pcm = await ulaw_to_pcm(audio, CARRIER_RATE, CARRIER_RATE, self._from_bot)
        self._heard.extend(pcm)
        peak = _peak_amplitude(pcm)
        if peak > self._peak:
            self._peak = peak
        if self._first_audio_at is None and peak > AUDIBLE_PEAK:
            self._first_audio_at = time.monotonic()
            print(f"  [{self._first_audio_at - self._started:5.1f}s] the bot started speaking")

    def _report(self) -> int:
        """Print what was observed and return the exit status."""
        seconds = len(self._heard) / (CARRIER_RATE * 2)
        print("\nWhat the carrier saw:")
        print(f"  events from the bot   {self._events or 'none'}")
        print(f"  audio received        {seconds:.1f}s, peak amplitude {self._peak}")
        if self._first_audio_at is not None:
            print(f"  bot spoke after       {self._first_audio_at - self._started:.1f}s")

        if self._args.record and self._heard:
            _write_wav(self._args.record, bytes(self._heard))
            print(f"  saved                 {self._args.record}")

        if self._peak > AUDIBLE_PEAK:
            print("\nPASS — the bot answered and spoke over the media stream.")
            return 0

        print(
            "\nFAIL — no audible audio came back.\n"
            "  Check the bot's own log: it says whether the handshake was recognised\n"
            "  (`CALL | audio connected`) and whether it tried to speak."
        )
        return 1


def _read_wav(path: str) -> bytes:
    """Read a WAV file as 8kHz mono 16-bit PCM, or explain why it cannot be."""
    with wave.open(path, "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise SystemExit(f"{path} must be mono 16-bit PCM WAV.")
        if source.getframerate() != CARRIER_RATE:
            raise SystemExit(
                f"{path} is {source.getframerate()}Hz; a carrier sends {CARRIER_RATE}Hz. "
                f"Convert it first, e.g.  ffmpeg -i {path} -ar 8000 -ac 1 caller.wav"
            )
        return source.readframes(source.getnframes())


def _write_wav(path: str, pcm: bytes) -> None:
    """Save what the bot said, so a person can listen to it."""
    with wave.open(path, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(CARRIER_RATE)
        target.writeframes(pcm)


def _peak_amplitude(pcm: bytes) -> int:
    """Loudest sample in a 16-bit PCM buffer.

    Written out rather than using `audioop`, which is deprecated in 3.12 and
    gone in 3.13 — this tool should not be the reason a Python upgrade breaks.
    """
    peak = 0
    for index in range(0, len(pcm) - 1, 2):
        value = int.from_bytes(pcm[index : index + 2], "little", signed=True)
        peak = max(peak, abs(value))
    return peak


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="fake_carrier.py",
        description="Simulate a phone carrier against a running bot, with no account needed.",
    )
    parser.add_argument(
        "--url", default="ws://localhost:7860/ws", help="the bot's telephony websocket"
    )
    parser.add_argument(
        "--seconds", type=float, default=15.0, help="how long to stay on the call (default 15)"
    )
    parser.add_argument(
        "--say",
        metavar="WAV",
        help="stream this file as the caller's voice (mono 16-bit 8kHz WAV). Default: silence.",
    )
    parser.add_argument("--record", metavar="WAV", help="save what the bot said to this file")
    parser.add_argument(
        "--inbound",
        action="store_true",
        help="present as a call coming in, rather than one this project placed",
    )
    parser.add_argument("--from", dest="from_number", default="+15550001111", help="caller ID")
    parser.add_argument("--to", dest="to_number", default="+923001234567", help="number dialled")
    parser.add_argument(
        "--prospect", type=int, help="present as a campaign call to this prospect id (Phase 8)"
    )
    parser.add_argument("--campaign", type=int, help="the campaign id to present with --prospect")
    parser.add_argument(
        "--attempt",
        type=int,
        help="the call attempt id; with it the bot writes the call's record and result onto that row",
    )
    return parser


def main() -> int:
    """Parse arguments and run one simulated call."""
    args = _parser().parse_args()
    args.direction = DIRECTION_INBOUND if args.inbound else DIRECTION_OUTBOUND
    try:
        return asyncio.run(FakeCall(args).run())
    except OSError as exc:
        print(f"\nCould not reach {args.url}: {exc}\nIs the bot running?", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
