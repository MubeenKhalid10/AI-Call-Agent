#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pretend to be the browser client, so the WebRTC path can be tested headlessly.

Run it from the `server/` directory against a running bot::

    uv run bot.py                                    # terminal 1
    uv run python tests/fake_browser.py              # terminal 2

**Why this exists.** The eval suite drives the bot over the *eval* transport and
`tests/fake_carrier.py` drives it over the *telephony* one, which leaves the
transport people actually develop against — WebRTC in a browser — as the only
one nothing tests. That gap hid a real bug: after a caller dropped without
closing cleanly, the next connection attached to a dead session and the page sat
there in silence until it was reloaded. Nothing automated could have caught it,
because catching it needs a second connection.

So this speaks the runner's `/api/offer` protocol the way the browser client
does — SDP offer, answer, a data channel with the keep-alive pings the bot uses
to notice a drop, and an audio track — and reports whether the bot actually
spoke.

    --reconnect         connect, drop, connect again: the regression test
    --abandon           drop without closing (a slept laptop, dead wifi)
    --seconds 15        how long to stay on each connection
    --record heard.wav  save what the bot said

**What `--abandon` reproduces.** A clean close tells the server, and everything
tidies up. A caller who simply stops — closed laptop, dropped wifi — tells it
nothing, and without `PEER_TIMEOUT_SECS` the session leaks and its `pc_id` stays
registered forever. Run with `--reconnect --abandon` and the second connection
must still get a bot; with `PEER_TIMEOUT_SECS=0` it will not, which is the bug
this guards.

Exit status is 0 when every connection got a bot that spoke.
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import sys
import time
import wave
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import aiohttp  # noqa: E402
import av  # noqa: E402  (an aiortc dependency; installed with the webrtc extra)
import numpy as np  # noqa: E402
from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.mediastreams import MediaStreamTrack  # noqa: E402

# The browser client's own rate. The bot resamples to whatever the pipeline runs
# at, so this only has to be something a real client would send.
CLIENT_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = CLIENT_RATE * FRAME_MS // 1000

# Enough audio frames to be sure the bot said something rather than emitting a
# few frames of silence while it warmed up.
SPOKE_THRESHOLD = 50


class SilenceTrack(MediaStreamTrack):
    """A microphone that sends silence, paced in real time.

    Real time matters: VAD and Deepgram Flux both judge turn-taking from the
    rate audio arrives at, so a track that runs as fast as the CPU allows
    produces behaviour no real client would show.
    """

    kind = "audio"

    def __init__(self) -> None:
        """Create the track."""
        super().__init__()
        self._timestamp = 0
        self._start = time.time()

    async def recv(self):
        """Produce the next 20ms of silence, on schedule."""
        target = self._start + self._timestamp / CLIENT_RATE
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame = av.AudioFrame.from_ndarray(
            np.zeros((1, FRAME_SAMPLES), dtype=np.int16), format="s16", layout="mono"
        )
        frame.sample_rate = CLIENT_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, CLIENT_RATE)
        self._timestamp += FRAME_SAMPLES
        return frame


class FakeBrowser:
    """One connection to the bot, as the browser client would make it."""

    def __init__(self, base_url: str) -> None:
        """Create the client for a runner at `base_url`."""
        self._base = base_url.rstrip("/")
        self.pc: RTCPeerConnection | None = None
        self.pc_id: str | None = None
        self.heard = bytearray()
        self._ping: asyncio.Task | None = None

    async def connect(self, pc_id: str | None = None) -> str | None:
        """Offer, take the answer, and start the keep-alive.

        Args:
            pc_id: A previous connection's id, which is what the browser client
                sends when you press connect again without reloading the page.
                Passing it is what exercises the runner's connection reuse.

        Returns:
            The peer connection id the runner answered with, or None if the
            offer was rejected. **A returned id equal to `pc_id` means the
            runner reused the old connection**, which is the failure mode this
            tool exists to catch: reuse renegotiates, and renegotiating does not
            start a bot.
        """
        self.pc = RTCPeerConnection()
        self.pc.addTrack(SilenceTrack())
        channel = self.pc.createDataChannel("chat")

        @channel.on("open")
        def _on_open():
            self._ping = asyncio.ensure_future(self._keep_alive(channel))

        @self.pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                asyncio.ensure_future(self._collect(track))

        await self.pc.setLocalDescription(await self.pc.createOffer())
        body = {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type}
        if pc_id:
            body["pc_id"] = pc_id

        async with aiohttp.ClientSession() as http:
            async with http.post(f"{self._base}/api/offer", json=body) as response:
                if response.status != 200:
                    print(
                        f"  offer rejected: HTTP {response.status} {(await response.text())[:200]}"
                    )
                    return None
                answer = await response.json()

        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        self.pc_id = answer.get("pc_id")
        return self.pc_id

    async def _keep_alive(self, channel) -> None:
        """Send the pings the bot uses to tell a live client from a vanished one."""
        while True:
            try:
                channel.send("ping")
            except Exception:  # noqa: BLE001 - the channel is gone; so are we
                return
            await asyncio.sleep(1)

    async def _collect(self, track) -> None:
        """Accumulate the bot's audio so we can tell whether it spoke."""
        while True:
            try:
                frame = await track.recv()
            except Exception:  # noqa: BLE001 - the track ended with the call
                return
            self.heard.extend(frame.to_ndarray().astype(np.int16).tobytes())

    async def listen(self, seconds: float) -> int:
        """Wait for the bot to speak, and report how many frames arrived."""
        before = len(self.heard)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if len(self.heard) - before > SPOKE_THRESHOLD * FRAME_SAMPLES * 2:
                break
        return (len(self.heard) - before) // (FRAME_SAMPLES * 2)

    async def close(self, *, graceful: bool = True) -> None:
        """Hang up, either politely or by vanishing.

        `graceful=False` is the interesting one: it stops the keep-alive and the
        outgoing audio but never closes the peer connection, so the bot is given
        nothing at all to notice — a laptop lid closing, or wifi dropping.
        """
        if self._ping:
            self._ping.cancel()
        if not self.pc:
            return
        if graceful:
            await self.pc.close()
            return
        for sender in self.pc.getSenders():
            if sender.track:
                sender.track.stop()


def _write_wav(path: str, pcm: bytes) -> None:
    """Save what the bot said, so a person can listen to it."""
    with wave.open(path, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(CLIENT_RATE)
        target.writeframes(pcm)


async def run(args: argparse.Namespace) -> int:
    """Make one or two connections and report what the bot did."""
    failures: list[str] = []

    print(f"Connecting to {args.url}")
    first = FakeBrowser(args.url)
    pc_id = await first.connect()
    print(f"  pc_id={pc_id}")
    frames = await first.listen(args.seconds)
    print(f"  first connection: {frames} audio frames -> {'SPOKE' if frames else 'SILENT'}")
    if not frames:
        failures.append("the bot never spoke on the first connection")

    if not args.reconnect:
        await first.close()
        if args.record and first.heard:
            _write_wav(args.record, bytes(first.heard))
            print(f"  saved {args.record}")
        return _report(failures)

    await first.close(graceful=not args.abandon)
    print(f"  {'vanished without closing' if args.abandon else 'disconnected cleanly'}")

    print(f"Waiting {args.gap:.0f}s for the bot to notice and let the session go")
    await asyncio.sleep(args.gap)

    print("Connecting again, reusing the previous pc_id (as the browser does)")
    second = FakeBrowser(args.url)
    got = await second.connect(pc_id)
    print(f"  pc_id={got}")
    if got and got == pc_id:
        # The runner handed back the same connection, which means it renegotiated
        # rather than creating one — and renegotiation starts no bot.
        failures.append("the runner reused the stale connection instead of making a new one")

    frames = await second.listen(args.seconds)
    print(f"  second connection: {frames} audio frames -> {'SPOKE' if frames else 'SILENT'}")
    if not frames:
        failures.append("the bot never spoke on the second connection")
    await second.close()

    if args.record and second.heard:
        _write_wav(args.record, bytes(second.heard))
        print(f"  saved {args.record}")

    return _report(failures)


def _report(failures: list[str]) -> int:
    """Print the verdict and return the exit status."""
    print()
    if not failures:
        print("PASS — the bot answered and spoke on every connection.")
        return 0
    print("FAIL:")
    for failure in failures:
        print(f"  - {failure}")
    print(
        "\n  If the second connection failed, check PEER_TIMEOUT_SECS: the bot has to notice\n"
        "  a caller who left without closing before it will release the connection."
    )
    return 1


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="fake_browser.py",
        description="Drive the bot's WebRTC path headlessly, as the browser client would.",
    )
    parser.add_argument("--url", default="http://localhost:7860", help="the running bot")
    parser.add_argument(
        "--seconds", type=float, default=15.0, help="how long to stay connected (default 15)"
    )
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help="connect, drop, and connect again — the regression test for the reconnect bug",
    )
    parser.add_argument(
        "--abandon",
        action="store_true",
        help="with --reconnect: drop without closing, as a slept laptop or dead wifi would",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=15.0,
        help="seconds between the two connections (default 15; must exceed "
        "PEER_TIMEOUT_SECS + DISCONNECT_GRACE_SECS)",
    )
    parser.add_argument("--record", metavar="WAV", help="save what the bot said to this file")
    return parser


def main() -> int:
    """Parse arguments and run."""
    args = _parser().parse_args()
    try:
        return asyncio.run(run(args))
    except OSError as exc:
        print(f"\nCould not reach {args.url}: {exc}\nIs the bot running?", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
