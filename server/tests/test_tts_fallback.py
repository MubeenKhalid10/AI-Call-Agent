#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 30: the TTS fallback.

Run it from the `server/` directory::

    uv run python tests/test_tts_fallback.py

No keys and no network. The switcher under test is the real
`TTSFallbackSwitcher` inside a real Pipecat pipeline run by a real
`PipelineWorker` (Pipecat's own `run_test` helper), holding two scripted TTS
services that speak — audio frames whose bytes name the service and the
sentence — or fail the way a provider does: an HTTP 402 at connect, a
connection reset after some audio, a rejected key, an application error.
What is asserted is what came out: which service spoke which sentence, in
what order, how many times, and which errors escaped the switcher.

Timing: the fakes close an audio context after 150 ms of silence (Cartesia
waits 3 s), so a run takes under a second; nothing asserts on wall-clock
figures.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
)
from pipecat.observers.base_observer import FramePushed  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.services.cartesia.tts import CartesiaTTSService  # noqa: E402
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService  # noqa: E402
from pipecat.services.tts_service import TTSService  # noqa: E402
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402
from pipecat.utils.errors import ErrorCategory  # noqa: E402

from src.config import Config, ConfigError  # noqa: E402
from src.reliability import HealthChecker, Status  # noqa: E402
from src.reliability.supervisor import Reason, SessionSupervisor  # noqa: E402
from src.services import make_tts  # noqa: E402
from src.tts_fallback import (  # noqa: E402
    STATE_FAILED,
    STATE_FALLBACK,
    STATE_PRIMARY,
    TTSFallbackSwitcher,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


class Captured:
    """A loguru sink that keeps every message, for asserting on what was logged."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._id = logger.add(lambda m: self.lines.append(m.record["message"]), level="DEBUG")

    def close(self) -> None:
        logger.remove(self._id)

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]


# --- The fakes --------------------------------------------------------------------------


class FakeHttpError(Exception):
    """An exception carrying an HTTP status, the shape Pipecat classifies."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"server rejected WebSocket connection: HTTP {status}")
        self.status_code = status


class FakeTTS(TTSService):
    """A TTS service scripted per sentence.

    Each entry of `script` is what the next `run_tts` does: `"ok"` speaks the
    sentence; `("error", exc)` fails before any audio the way a provider's
    error does (`push_error` with the exception, so Pipecat classifies it);
    `("partial", exc)` streams half the sentence, then fails; `("permanent",
    text)` fails in a way that leaves the service unusable; `("application",
    text)` reports a failure of application code. Past the end of the
    script, it speaks. `fail_on_start` fails at StartFrame, the way a
    websocket service fails when its connection is refused at the start of
    the call.
    """

    def __init__(self, label: str, *, script: list[Any] | None = None, fail_on_start: Exception | None = None) -> None:
        super().__init__(
            name=label,
            push_start_frame=True,
            push_stop_frames=True,
            stop_frame_timeout_s=0.15,
            sample_rate=16000,
        )
        self.label = label
        self.script = list(script or [])
        self.requested: list[str] = []
        self._fail_on_start = fail_on_start

    def can_generate_metrics(self) -> bool:
        return False

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._fail_on_start is not None:
            await self.push_error(
                error_msg=f"Unknown error occurred: {self._fail_on_start}", exception=self._fail_on_start
            )

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        self.requested.append(text.strip())
        action = self.script.pop(0) if self.script else "ok"
        if action == "ok":
            yield self._audio(text, context_id)
            return
        kind, payload = action
        if kind == "partial":
            yield self._audio(text[: len(text) // 2], context_id)
            # A provider streams for a while before the connection drops.
            await asyncio.sleep(0.05)
            kind = "error"
        if kind == "error":
            await self.push_error(error_msg=f"Unknown error occurred: {payload}", exception=payload)
        elif kind == "permanent":
            await self.push_error(error_msg=str(payload), force_treat_as_permanent=True)
        elif kind == "application":
            await self.push_error(error_msg=str(payload), category=ErrorCategory.APPLICATION)
        yield TTSStoppedFrame(context_id=context_id)

    def _audio(self, text: str, context_id: str) -> TTSAudioRawFrame:
        return TTSAudioRawFrame(
            audio=f"{self.label}:{text.strip()}".encode(),
            sample_rate=16000,
            num_channels=1,
            context_id=context_id,
        )


def spoken(down: list[Frame]) -> list[str]:
    """What was heard, in order: `label:sentence` per audio frame."""
    return [f.audio.decode() for f in down if isinstance(f, TTSAudioRawFrame)]


def escaped_errors(up: list[Frame]) -> list[ErrorFrame]:
    return [f for f in up if isinstance(f, ErrorFrame)]


def response(*sentences: str) -> list[Frame]:
    """One LLM response, streamed as one text frame per sentence."""
    return [LLMFullResponseStartFrame(), *(LLMTextFrame(s) for s in sentences), LLMFullResponseEndFrame()]


async def drive(switcher: TTSFallbackSwitcher, frames: list[Frame], *, settle: float = 0.7) -> tuple[list[Frame], list[Frame]]:
    """Run the switcher in a pipeline, send the frames, wait for the audio to come out."""
    # A generous start timeout: the first pipeline of the run pays for imports and warm-up.
    down, up = await run_test(switcher, frames_to_send=[*frames, SleepFrame(settle)], start_timeout=10.0)
    return list(down), list(up)


def build(
    primary_script: list[Any] | None = None,
    fallback_script: list[Any] | None = None,
    *,
    primary_fail_on_start: Exception | None = None,
    fallback_fail_on_start: Exception | None = None,
) -> tuple[TTSFallbackSwitcher, FakeTTS, FakeTTS]:
    primary = FakeTTS("PrimaryTTS", script=primary_script, fail_on_start=primary_fail_on_start)
    fallback = FakeTTS("FallbackTTS", script=fallback_script, fail_on_start=fallback_fail_on_start)
    return TTSFallbackSwitcher(primary, fallback), primary, fallback


# --- The checks -----------------------------------------------------------------------


async def check_primary_healthy() -> None:
    print("\n=== the primary works: the fallback is never used ===")
    switcher, primary, fallback = build()
    down, up = await drive(switcher, [*response("Hello there.", " How are you?"), *response(" Fine.")])
    heard = spoken(down)
    check("every sentence is spoken by the primary", heard == ["PrimaryTTS:Hello there.", "PrimaryTTS:How are you?", "PrimaryTTS:Fine."], str(heard))
    check("the fallback was never asked for anything", fallback.requested == [], str(fallback.requested))
    check("nothing switched", switcher.state == STATE_PRIMARY and switcher.active is primary and not switcher.switched)
    check("no error escaped", not escaped_errors(up))
    check("the record says so", switcher.summary()["switched"] is False and switcher.describe() == "PrimaryTTS throughout", switcher.describe())


async def check_quota_before_audio() -> None:
    print("\n=== HTTP 402 before any audio: the fallback speaks the failed sentence once, then the rest ===")
    captured = Captured()
    try:
        switcher, primary, fallback = build(primary_script=[("error", FakeHttpError(402))])
        down, up = await drive(switcher, [*response("Hello there.", " How are you?"), *response(" Second response.")])
    finally:
        captured.close()
    heard = spoken(down)
    check(
        "the failed sentence and every later one come from the fallback, in order, once each",
        heard == ["FallbackTTS:Hello there.", "FallbackTTS:How are you?", "FallbackTTS:Second response."],
        str(heard),
    )
    check("the primary was asked once and never again", primary.requested == ["Hello there."], str(primary.requested))
    check("the primary is retired", not primary.is_usable)
    check("the fallback is active for the rest of the call", switcher.state == STATE_FALLBACK and switcher.active is fallback)
    check("the 402 never escaped the switcher", not escaped_errors(up), str([e.error for e in escaped_errors(up)]))
    summary = switcher.summary()
    check("the record: category quota, the failed sentence re-spoken, the rest handed over", summary["category"] == "quota" and summary["respoken"] == 1 and summary["handed_over"] >= 1, json.dumps(summary))
    check("the flip waited for the response to finish", summary["activation"] == "response finished", str(summary["activation"]))
    engaged = captured.matching("tts.fallback.engaged")
    check("the switch was logged with the category and the providers", len(engaged) == 1 and "category=quota" in engaged[0] and "primary=PrimaryTTS" in engaged[0] and "fallback=FallbackTTS" in engaged[0], engaged[0] if engaged else "none")
    check("and the activation", len(captured.matching("tts.fallback.active")) == 1)


async def check_partial_audio_then_failure() -> None:
    print("\n=== some audio, then the connection drops: nothing is spoken twice ===")
    switcher, primary, fallback = build(primary_script=[("partial", ConnectionError("connection reset"))])
    down, up = await drive(switcher, response("Hello there.", " How are you?"))
    heard = spoken(down)
    check("the caller heard the primary's partial sentence", heard[:1] == ["PrimaryTTS:Hello"], str(heard))
    check("the fallback did not repeat it", "FallbackTTS:Hello there." not in heard, str(heard))
    check("but did speak the rest of the response", heard[1:] == ["FallbackTTS:How are you?"], str(heard))
    check("the connection failure retired the primary, with nothing re-spoken", switcher.state == STATE_FALLBACK and switcher.summary()["category"] == "connectivity" and switcher.summary()["respoken"] == 0, json.dumps(switcher.summary()))
    check("no error escaped", not escaped_errors(up))


async def check_failure_at_start() -> None:
    print("\n=== the connection is refused at the start of the call (today's 402) ===")
    switcher, primary, fallback = build(primary_fail_on_start=FakeHttpError(402))
    down, up = await drive(switcher, [TTSSpeakFrame("Good morning."), *response(" Hello there.")])
    heard = spoken(down)
    check("the greeting and the first response come from the fallback", heard == ["FallbackTTS:Good morning.", "FallbackTTS:Hello there."], str(heard))
    check("the primary was never asked to speak", primary.requested == [], str(primary.requested))
    check("the flip happened at once, with nothing in flight", switcher.summary()["activation"] == "no response in flight", json.dumps(switcher.summary()))
    check("no error escaped", not escaped_errors(up))


async def check_fixed_utterance_failure() -> None:
    print("\n=== a fixed utterance (no LLM response in flight) fails ===")
    switcher, primary, fallback = build(primary_script=[("error", TimeoutError("connect timed out"))])
    down, up = await drive(switcher, [TTSSpeakFrame("Please hold."), TTSSpeakFrame("Thank you.")])
    heard = spoken(down)
    check("the failed line is spoken once by the fallback, and the next line too", heard == ["FallbackTTS:Please hold.", "FallbackTTS:Thank you."], str(heard))
    check("a timeout counts as connectivity", switcher.summary()["category"] == "connectivity", str(switcher.summary()["category"]))
    check("no error escaped", not escaped_errors(up))


async def check_application_error_is_not_a_switch() -> None:
    print("\n=== a failure of application code is not the provider's ===")
    switcher, primary, fallback = build(primary_script=[("application", "text transformer raised")])
    down, up = await drive(switcher, response("Hello there.", " How are you?"))
    heard = spoken(down)
    check("the primary keeps the call", switcher.state == STATE_PRIMARY and heard == ["PrimaryTTS:How are you?"], str(heard))
    check("and the error travels on for the rest of the pipeline", len(escaped_errors(up)) >= 1 and escaped_errors(up)[0].category is ErrorCategory.APPLICATION)


async def check_fallback_failures() -> None:
    print("\n=== the fallback fails too ===")
    # (a) The fallback's key is rejected at the start of the call, before the
    # primary fails: there is nothing to switch to, so the primary's error is
    # the pipeline's to deal with, as it always was.
    switcher, primary, fallback = build(primary_script=[("error", FakeHttpError(402))], fallback_fail_on_start=FakeHttpError(401))
    down, up = await drive(switcher, response("Hello there.", " How are you?"))
    heard = spoken(down)
    check("(a) a rejected fallback key leaves the fallback unusable", not fallback.is_usable)
    check("(a) so the primary is not retired", switcher.state == STATE_PRIMARY and primary.is_usable)
    check("(a) its error escapes, as before", any(e.processor is primary and e.category is ErrorCategory.QUOTA for e in escaped_errors(up)), str([e.error for e in escaped_errors(up)]))
    check("(a) the fallback's own error did not escape (it is not the active service)", not any(e.processor is fallback for e in escaped_errors(up)))
    check("(a) the primary carried on with the next sentence", heard == ["PrimaryTTS:How are you?"], str(heard))

    # (b) The fallback fails after taking over: no switching back.
    switcher, primary, fallback = build(primary_script=[("error", FakeHttpError(402))], fallback_script=["ok", "ok", ("error", ConnectionError("reset"))])
    down, up = await drive(switcher, [*response("Hello there.", " How are you?"), *response(" Third."), *response(" Fourth.")])
    heard = spoken(down)
    check("(b) the fallback spoke until it failed, then the sentence after", heard == ["FallbackTTS:Hello there.", "FallbackTTS:How are you?", "FallbackTTS:Fourth."], str(heard))
    check("(b) its error escaped for the supervisor to count", any(e.processor is fallback for e in escaped_errors(up)), str([e.error for e in escaped_errors(up)]))
    check("(b) and nothing switched back", switcher.state == STATE_FALLBACK and switcher.active is fallback and primary.requested == ["Hello there."])

    # (c) The fallback becomes unusable while the primary's response is
    # draining: the flip is refused, and the switcher reports that nothing
    # can speak — once for the flip, then once per sentence nobody says.
    switcher, primary, fallback = build(primary_script=[("error", FakeHttpError(402))], fallback_script=[("permanent", "key revoked mid-call")])
    # The third response arrives after the flip was refused, so it meets a
    # switcher that has nothing left to speak with.
    down, up = await drive(switcher, [*response("Hello there.", " How are you?"), SleepFrame(0.5), *response(" Third.")])
    heard = spoken(down)
    check("(c) nothing was spoken", heard == [], str(heard))
    check("(c) the switcher gave up", switcher.state == STATE_FAILED)
    own = [e for e in escaped_errors(up) if e.processor is switcher]
    check("(c) and reported it as its own error, in the TTS stage", len(own) >= 2 and all("cannot take over" in e.error for e in own), str([e.error for e in own]))
    check("(c) with nothing from the two services leaking past it", not any(e.processor in (primary, fallback) for e in escaped_errors(up)))


async def check_interruption_while_draining() -> None:
    print("\n=== the caller interrupts while the failed response is draining ===")
    switcher, primary, fallback = build(primary_script=[("error", FakeHttpError(402))])
    frames = [
        LLMFullResponseStartFrame(),
        LLMTextFrame("Hello there."),
        LLMTextFrame(" How are"),  # no boundary yet: still in the primary's aggregator
        SleepFrame(0.1),
        InterruptionFrame(),
        *response("Second thing."),
    ]
    down, up = await drive(switcher, frames)
    heard = spoken(down)
    check("the interruption flipped the switch", switcher.state == STATE_FALLBACK and switcher.summary()["activation"] == "interrupted", json.dumps(switcher.summary()))
    check("the half sentence the caller cut off was never spoken", not any("How are" in h for h in heard), str(heard))
    check("the response after the interruption was spoken by the fallback, once", heard.count("FallbackTTS:Second thing.") == 1 and not any(h.startswith("PrimaryTTS") for h in heard), str(heard))
    check("no error escaped", not escaped_errors(up))


async def check_supervisor_and_membership() -> None:
    print("\n=== the supervisor counts the switcher, not what it holds ===")
    switcher, primary, fallback = build()
    check("the services are inside", switcher.contains(primary) and switcher.contains(fallback))
    inner = [p for branch in switcher.processors for p in branch.processors]
    check("and so are the filters, sources and sinks around them", inner and all(switcher.contains(p) for p in inner), str(len(inner)))
    check("the switcher itself is not", not switcher.contains(switcher))

    ended: list[str] = []
    noted: list[tuple[Reason, str]] = []

    async def end() -> None:
        ended.append("end")

    supervisor = SessionSupervisor(
        end_session=end,
        cancel_session=end,
        say=None,
        max_service_failures=3,
        on_terminated=lambda reason, detail: noted.append((reason, detail)),
        ignore_errors_from=switcher.contains,
    )

    def pushed(source: object, frame: Frame) -> FramePushed:
        return FramePushed(source=source, destination=source, frame=frame, direction=FrameDirection.UPSTREAM, timestamp=0)

    error = ErrorFrame(error="server rejected WebSocket connection: HTTP 402", processor=primary)
    await supervisor.observer.on_push_frame(pushed(primary, error))
    await supervisor.observer.on_push_frame(pushed(inner[0], error))
    check("an error inside the switcher is not a session failure, even before the agent has spoken", not ended and supervisor.terminated_by is None)
    await supervisor.observer.on_push_frame(pushed(switcher, error))
    check("the same frame pushed on by the switcher is", supervisor.terminated_by is Reason.SERVICE_FAILURE and noted and "tts" in noted[0][1], str(noted))

    plain = SessionSupervisor(end_session=end, cancel_session=end, say=None, on_terminated=lambda r, d: noted.append((r, d)))
    check("without the predicate every error counts, as before", not plain.error_is_internal(primary))


async def check_no_secrets_in_logs() -> None:
    print("\n=== a credential in the provider's error never reaches the log ===")
    captured = Captured()
    try:
        switcher, primary, fallback = build(primary_script=[("error", FakeHttpError(402, "refused: Authorization: Bearer sk-live-SECRET123456 (quota)"))])
        await drive(switcher, response("Hello there."))
    finally:
        captured.close()
    lines = captured.matching("TTS FALLBACK")
    check("the switch was logged", any("tts.fallback.engaged" in line for line in lines))
    check("without the token", not any("SECRET123456" in line for line in lines), next((l for l in lines if "engaged" in l), ""))
    check("and never a key", not any("not-used-by-these-checks" in line for line in captured.lines))


def check_config_and_factory() -> None:
    print("\n=== configuration: off by default, explicit providers untouched, the switcher only when asked ===")
    saved = {k: os.environ.get(k) for k in ("TTS_PROVIDER", "TTS_FALLBACK_ENABLED", "TTS_FALLBACK_PROVIDER", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")}

    def env(**values: str | None) -> None:
        for key in saved:
            os.environ.pop(key, None)
        for key, value in values.items():
            if value is not None:
                os.environ[key] = value

    try:
        env()
        config = Config.from_env()
        check("off by default", config.tts_fallback_enabled is False and config.tts_fallback_provider == "elevenlabs" and config.tts_fallback_api_key is None)
        check("TTS_PROVIDER=cartesia alone builds the one Cartesia service, as before", isinstance(make_tts(config), CartesiaTTSService))
        check("and the summary line is unchanged", "TTS=cartesia |" in config.describe(), config.describe().split(" | ")[2])

        env(TTS_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v")
        check("TTS_PROVIDER=elevenlabs builds ElevenLabs directly, no switcher", isinstance(make_tts(Config.from_env()), ElevenLabsTTSService))

        env(TTS_FALLBACK_ENABLED="true", ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v")
        config = Config.from_env()
        tts = make_tts(config)
        check("enabled: the switcher, Cartesia first, ElevenLabs on standby", isinstance(tts, TTSFallbackSwitcher) and isinstance(tts.primary, CartesiaTTSService) and isinstance(tts.fallback, ElevenLabsTTSService))
        check("the summary line says so", "TTS=cartesia->elevenlabs" in config.describe(), config.describe().split(" | ")[2])
        check("its name puts it in the TTS stage for the supervisor", "TTS" in tts.name, tts.name)

        env(TTS_FALLBACK_ENABLED="true", TTS_FALLBACK_PROVIDER="cartesia")
        try:
            Config.from_env()
            check("the same provider as a fallback is refused", False)
        except ConfigError as exc:
            check("the same provider as a fallback is refused", "TTS_FALLBACK_PROVIDER" in str(exc) and "same as TTS_PROVIDER" in str(exc))

        env(TTS_FALLBACK_ENABLED="true", ELEVENLABS_VOICE_ID="v")
        try:
            Config.from_env()
            check("a fallback without its key is refused", False)
        except ConfigError as exc:
            check("a fallback without its key is refused", "ELEVENLABS_API_KEY is not set (needed by TTS_FALLBACK_PROVIDER)" in str(exc))

        env(TTS_FALLBACK_ENABLED="true", ELEVENLABS_API_KEY="k")
        try:
            make_tts(Config.from_env())
            check("a fallback without its voice is refused when the service is built", False)
        except ConfigError as exc:
            check("a fallback without its voice is refused when the service is built", "ELEVENLABS_VOICE_ID" in str(exc))

        env(TTS_FALLBACK_ENABLED="maybe")
        try:
            Config.from_env()
            check("a value that is not a boolean is a configuration problem", False)
        except ConfigError as exc:
            check("a value that is not a boolean is a configuration problem", "TTS_FALLBACK_ENABLED" in str(exc))
    finally:
        env(**saved)


async def check_health_component() -> None:
    print("\n=== the health check: the fallback's key, without spending its credits ===")
    config = Config.from_env()
    checker = HealthChecker(config)
    component = await checker._check_tts_fallback()
    await checker.close()
    check("skipped when the fallback is off", component.name == "tts_fallback" and component.status is Status.SKIPPED, component.detail)
    source = (SERVER / "src" / "reliability" / "health.py").read_text(encoding="utf-8")
    check("the probe is the credential probe, on the fallback's provider", "_probe_tts(\n            \"tts_fallback\", self._config.tts_fallback_provider" in source)
    check("nothing in it synthesises", "/tts/bytes" not in source and "tts/websocket" not in source)
    check("and the CLI offers it", '"tts_fallback"' in (SERVER / "health.py").read_text(encoding="utf-8"))


def check_wiring() -> None:
    print("\n=== wired into the bot ===")
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("the bot builds its TTS through the factory, unchanged", "tts = make_tts(CONFIG)" in bot)
    check("the supervisor ignores what the switcher answers for", "ignore_errors_from=tts.contains if isinstance(tts, TTSFallbackSwitcher) else None" in bot)
    check("and the session log says which provider spoke", "TTS FALLBACK | {tts.describe()}" in bot)
    services = (SERVER / "src" / "services.py").read_text(encoding="utf-8")
    check("the factory returns the plain service unless the fallback is on", "if not config.tts_fallback_enabled:\n        return primary" in services)
    example = (SERVER / ".env.example").read_text(encoding="utf-8")
    check("the settings are documented", "TTS_FALLBACK_ENABLED=false" in example and "TTS_FALLBACK_PROVIDER=elevenlabs" in example)
    validate = (SERVER / "validate.py").read_text(encoding="utf-8")
    check("and the checks are in the validation run", '"test_tts_fallback"' in validate)


async def main() -> int:
    print("TTS fallback checks — no keys, no network.")
    await check_primary_healthy()
    await check_quota_before_audio()
    await check_partial_audio_then_failure()
    await check_failure_at_start()
    await check_fixed_utterance_failure()
    await check_application_error_is_not_a_switch()
    await check_fallback_failures()
    await check_interruption_while_draining()
    await check_supervisor_and_membership()
    await check_no_secrets_in_logs()
    check_config_and_factory()
    await check_health_component()
    check_wiring()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
