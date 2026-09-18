#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the two things that only go wrong on a real call with a real person.

Run it from the `server/` directory::

    uv run python tests/test_realtime.py

Both of the behaviours here were added after live testing found them, and both
are invisible to every other test in the project:

* **Echo suppression.** On a laptop with no headphones the agent hears itself
  through the speakers, transcribes its own voice as the caller, and answers it —
  a conversation with no human in it. The eval suite never sees this, because
  synthesized audio has no acoustic path back.
* **The peer watchdog.** A browser that vanishes without closing — a slept
  laptop, dead wifi — reports nothing at all, so the session leaks and the next
  connection attaches to it instead of starting a bot. The eval suite never sees
  this either, because it only ever makes one connection.

What is checked here is the *logic*: which strategies get chosen, and when the
watchdog decides a caller is gone. The end-to-end behaviour needs a bot running
and lives in `tests/fake_browser.py`, which is where the reconnect bug is
actually reproduced.

It is a plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

# Set before importing config: nothing here reaches a vendor, but `Config`
# validates that the keys exist before it will build.
for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"  # No database is opened here.

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult  # noqa: E402
from pipecat.turns.user_start.external_user_turn_start_strategy import (  # noqa: E402
    ExternalUserTurnStartStrategy,
)
from pipecat.turns.user_stop.external_user_turn_stop_strategy import (  # noqa: E402
    ExternalUserTurnStopStrategy,
)

from src.config import Config, ConfigError  # noqa: E402
from src.resilience import PeerWatchdog  # noqa: E402
from src.turns import (  # noqa: E402
    VADBargeInStartStrategy,
    make_mute_strategies,
    make_user_aggregator_params,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def config_with(**env: str) -> Config:
    """Build a config with some environment variables set, then put them back."""
    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        return Config.from_env()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def transcript(text: str) -> TranscriptionFrame:
    """A transcription frame, as the STT would produce."""
    return TranscriptionFrame(text=text, user_id="caller", timestamp="now")


class StubConnection:
    """A peer connection whose liveness the test controls."""

    def __init__(self) -> None:
        self.alive = True

    def is_connected(self) -> bool:
        return self.alive


class StubGuard:
    """Records that the watchdog reported a disconnect."""

    def __init__(self) -> None:
        self.disconnects = 0

    async def on_disconnected(self) -> None:
        self.disconnects += 1


async def check_echo_suppression() -> None:
    """Which mute strategy each setting picks, and what it actually does."""
    print("\n=== echo suppression ===")

    for mode, strategies in (("off", 0), ("greeting", 1), ("always", 1)):
        config = config_with(ECHO_SUPPRESSION=mode)
        chosen = make_mute_strategies(config)
        check(f"{mode:<8} selects {strategies} strategy", len(chosen) == strategies, str(chosen))

        # The strategies are useless unless they reach the aggregator, and that
        # wiring is one keyword away from being silently dropped.
        params = make_user_aggregator_params(config)
        check(
            f"{mode:<8} reaches the aggregator",
            len(params.user_mute_strategies) == strategies,
        )

    # Barge-in is a Phase 2 feature the eval suite checks, so the default must
    # not quietly disable it.
    check("the default is off, so barge-in survives", Config.from_env().echo_suppression == "off")

    # A typo has to fail loudly: "ECHO_SUPPRESION=always" that silently falls
    # back to off would look exactly like the bug it was set to fix.
    try:
        config_with(ECHO_SUPPRESSION="yes")
        check("an unrecognised mode is rejected", False, "did not raise")
    except ConfigError as exc:
        check("an unrecognised mode is rejected", "ECHO_SUPPRESSION" in str(exc))

    print("\n=== what 'always' does to a turn ===")
    strategy = make_mute_strategies(config_with(ECHO_SUPPRESSION="always"))[0]
    check(
        "open line: the caller is heard", await strategy.process_frame(transcript("hello")) is False
    )
    await strategy.process_frame(BotStartedSpeakingFrame())
    # This is the whole point: while the agent is talking, what the microphone
    # picks up is the agent, and it must not reach the conversation.
    check(
        "agent speaking: the caller is muted",
        await strategy.process_frame(transcript("echo")) is True,
    )
    await strategy.process_frame(BotStoppedSpeakingFrame())
    check(
        "agent finished: the caller is heard again",
        await strategy.process_frame(transcript("real")) is False,
    )


async def check_barge_in_trigger() -> None:
    """What cuts the agent off on the Flux path, and that Flux still ends the turn."""
    print("\n=== barge-in trigger (Flux path) ===")

    config = config_with(STT_PROVIDER="deepgram_flux")
    check("the default is the VAD", config.barge_in_trigger == "vad")
    strategies = make_user_aggregator_params(config).user_turn_strategies
    check(
        "the VAD barge-in goes in front of Flux's own start strategy",
        strategies is not None
        and [type(s) for s in strategies.start] == [VADBargeInStartStrategy, ExternalUserTurnStartStrategy],
        str(strategies),
    )
    # The thing `turns.py` warns about: strategies of our own must not switch
    # Flux's end-of-turn detection off.
    check(
        "Flux still decides where the turn ends",
        strategies is not None and [type(s) for s in strategies.stop] == [ExternalUserTurnStopStrategy],
    )

    flux_only = config_with(STT_PROVIDER="deepgram_flux", BARGE_IN_TRIGGER="flux")
    check(
        "BARGE_IN_TRIGGER=flux hands everything back to the service's recommendation",
        make_user_aggregator_params(flux_only).user_turn_strategies is None,
    )
    try:
        config_with(BARGE_IN_TRIGGER="loud")
        check("an unknown trigger is refused", False, "no error raised")
    except ConfigError as exc:
        check("an unknown trigger is refused", "BARGE_IN_TRIGGER" in str(exc))

    class Recording(VADBargeInStartStrategy):
        def __init__(self):
            super().__init__()
            self.started = 0

        async def trigger_user_turn_started(self, **kwargs):
            self.started += 1

    strategy = Recording()
    result = await strategy.process_frame(VADUserStartedSpeakingFrame())
    check(
        "speech while the agent is quiet opens nothing: that is Flux's call",
        strategy.started == 0 and result == ProcessFrameResult.CONTINUE,
    )
    await strategy.process_frame(BotStartedSpeakingFrame())
    result = await strategy.process_frame(VADUserStartedSpeakingFrame())
    check(
        "speech over the agent's voice opens the turn at once",
        strategy.started == 1 and result == ProcessFrameResult.STOP,
    )
    await strategy.process_frame(BotStoppedSpeakingFrame())
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    check("and once the agent has stopped, it is Flux's call again", strategy.started == 1)


async def check_barge_in_gate() -> None:
    """An interruption ends the reply it interrupted: nothing resumes, a noise starts no inference."""
    print("\n=== after a barge-in ===")
    from pipecat.frames.frames import InterruptionFrame, LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    from src.turns import BargeInGate, is_meaningful_speech

    for noise in ("m", "Mm.", "Uh, um.", "Hmm", ""):
        check(f"{noise!r} is not speech", not is_meaningful_speech(noise))
    for words in ("Wait.", "Mhm.", "Uh, no.", "Okay", "Where is your office?"):
        check(f"{words!r} is speech", is_meaningful_speech(words))

    context = LLMContext(messages=[{"role": "user", "content": "Tell me what you do."}])
    passed: list = []
    told: list[str] = []
    dropped: list[str] = []
    gate = BargeInGate(context, on_barge_in_turn=lambda: told.append("alone"), on_discarded_turn=dropped.append)

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        passed.append(frame)

    gate.push_frame = push  # type: ignore[method-assign] - the stub downstream

    async def turn(text: str) -> bool:
        """The aggregator appends the turn and sends the context on; did it reach the LLM?"""
        context.add_message({"role": "user", "content": text})
        before = len(passed)
        await gate.process_frame(LLMContextFrame(context=context), FrameDirection.DOWNSTREAM)
        return len(passed) > before

    await gate.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    check("a turn that starts while the agent is quiet is an ordinary turn", await turn("m") and not told)
    context.transform_messages(lambda messages: messages[:1])

    await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await gate.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    check("a noise that cut the agent off starts no inference", not await turn("M"))
    check("and leaves the conversation as it was", [m["content"] for m in context.messages] == ["Tell me what you do."], str(context.messages))
    check("the monitor is told it was discarded", dropped == ["M"])
    check("the next real words are still the interrupting turn", await turn("Wait.") and told == ["alone"])
    check("only the new words were added", [m["content"] for m in context.messages] == ["Tell me what you do.", "Wait."])
    check("and the turn after that is ordinary again", await turn("So where is your office?") and told == ["alone"])


async def check_peer_watchdog() -> None:
    """When the watchdog decides a browser has gone, and when it holds off."""
    print("\n=== peer watchdog ===")

    connection, guard = StubConnection(), StubGuard()
    watchdog = PeerWatchdog(connection, guard, timeout_secs=0.3, poll_secs=0.05)
    watchdog.start()
    await asyncio.sleep(0.5)
    check("a live connection is left alone", guard.disconnects == 0)

    connection.alive = False
    await asyncio.sleep(0.8)
    check("a dead connection is reported", guard.disconnects == 1, f"{guard.disconnects} times")

    await asyncio.sleep(0.5)
    check("and reported only once", guard.disconnects == 1, f"{guard.disconnects} times")
    watchdog.stop()

    # A blip is not a disconnect. WebRTC recovers from brief interruptions all
    # the time, and tearing the call down on the first missed ping would end
    # calls that were about to carry on fine.
    connection, guard = StubConnection(), StubGuard()
    watchdog = PeerWatchdog(connection, guard, timeout_secs=0.5, poll_secs=0.05)
    watchdog.start()
    connection.alive = False
    await asyncio.sleep(0.2)
    connection.alive = True
    await asyncio.sleep(0.6)
    check("a brief blip does not end the call", guard.disconnects == 0, f"{guard.disconnects}")
    watchdog.stop()

    # Stopping has to actually stop it: the session's teardown runs while the
    # connection is dead, and a watchdog still ticking would report a disconnect
    # into a guard whose worker is already gone.
    connection, guard = StubConnection(), StubGuard()
    watchdog = PeerWatchdog(connection, guard, timeout_secs=0.2, poll_secs=0.05)
    watchdog.start()
    watchdog.stop()
    connection.alive = False
    await asyncio.sleep(0.5)
    check("a stopped watchdog stays quiet", guard.disconnects == 0)

    # Telephony passes 0 here, because a phone call's websocket closing is
    # already an unambiguous disconnect signal.
    connection, guard = StubConnection(), StubGuard()
    watchdog = PeerWatchdog(connection, guard, timeout_secs=0.0, poll_secs=0.05)
    watchdog.start()
    connection.alive = False
    await asyncio.sleep(0.3)
    check("timeout 0 disables it", guard.disconnects == 0)
    watchdog.stop()

    # Every other transport has no peer connection to watch, and must not crash.
    watchdog = PeerWatchdog(None, StubGuard(), timeout_secs=1.0)
    watchdog.start()
    watchdog.stop()
    check("no connection is a harmless no-op", True)

    check("the default timeout is set", Config.from_env().peer_timeout_secs > 0)


async def main() -> int:
    """Run every check and report."""
    started = time.monotonic()
    await check_echo_suppression()
    await check_barge_in_trigger()
    await check_barge_in_gate()
    await check_peer_watchdog()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"All checks passed. ({time.monotonic() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
