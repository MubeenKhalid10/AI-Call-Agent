#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""A controlled real phone call, checked end to end. Phase 12.

Run it from the `server/` directory with the bot up and reachable through the
tunnel in `TELEPHONY_PUBLIC_URL`, and your own phone in your hand::

    uv run bot.py                                         # terminal 1
    ngrok http 7860                                       # terminal 2 (URL into .env)
    uv run python tests/live_call.py --to +92300XXXXXXX   # terminal 3

**What this is for.** Everything else in `tests/` runs without a carrier, and
everything that matters about a *real* call — a real line, a real handset,
a real person's timing and accent, the carrier's own answer detection — can
only be checked by placing one. This places exactly one call, to a number you
name, through whichever carrier `.env` configures, and then checks what both
sides saw:

* **the carrier's side**, by polling the call as `call.py` does: was it
  placed, did it ring, was it answered, how long was it billed, and what did
  answering-machine detection say, when it was on;
* **the bot's side**, by reading the report `bot.py` writes to
  `CALL_REPORT_DIR/<call id>.json` when the call ends: did it greet, how many
  turns each side took, did every turn get a reply, how fast it stopped when
  you interrupted it, the per-turn latency, whether it mistook you for a
  machine, and how the call ended.

It prints a script for you to follow on the phone — greet, answer briefly,
interrupt mid-sentence, stay silent, hang up — so the report has something to
measure, and then a PASS/FAIL checklist. Exit status is 0 when every required
check passed; 2 when the call happened but a check failed; 1 when it could not
be placed.

**It spends money and rings a phone.** It asks for confirmation unless you
pass `--yes`, and it refuses without `--to`. Nothing here touches the campaign
tables: the call is a manual one, like `call.py`, with no prospect behind it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(SERVER / ".env", override=True)

from src.config import ConfigError, TelephonyConfig, VoiceQualityConfig  # noqa: E402
from src.telephony import (  # noqa: E402
    PARAM_DIRECTION,
    PARAM_FROM,
    PARAM_TO,
    CallRequest,
    TelephonyError,
    make_provider,
    stream_url,
)
from src.telephony.session import DIRECTION_OUTBOUND  # noqa: E402
from src.voice_quality import load_call_report  # noqa: E402

EXIT_OK = 0
EXIT_CANNOT_PLACE = 1
EXIT_CHECK_FAILED = 2

SCRIPT = """
What to do on the phone (the checks below look for each of these):

  1. Answer, and say "hello" once. Wait for the agent to finish its opening.
  2. Answer its first question with ONE word ("yes", "no", "sure").
  3. Ask it something with a long answer ("tell me how it works"), and while
     it is mid-sentence, interrupt: "sorry, hang on, who did you say you were
     with?"  It must stop within a second and answer the new question.
  4. Say a whole sentence with a pause in the middle: "we run about forty
     trucks ... mostly long haul."  It should treat it as one turn.
  5. Say nothing for fifteen seconds. It should check whether you are there.
  6. Say goodbye and let the agent end the call, or hang up yourself.
"""


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str, bool]] = []

    def check(self, label: str, ok: bool, detail: str = "", *, required: bool = True) -> None:
        self.results.append((label, ok, detail, required))
        tag = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"  {tag}  {label}{('  -- ' + detail) if detail else ''}")

    @property
    def failed(self) -> list[str]:
        return [label for label, ok, _, required in self.results if required and not ok]


async def wait_for_report(directory: str | None, call_id: str, timeout: float) -> dict | None:
    if not directory:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = load_call_report(directory, call_id)
        if report is not None:
            return report
        await asyncio.sleep(1.0)
    return None


def _confirm(to_number: str, telephony: TelephonyConfig) -> bool:
    print(f"\nThis will place a REAL call from {telephony.from_number} to {to_number} via {telephony.provider}.")
    print("It costs money and rings that phone. Type 'yes' to continue: ", end="", flush=True)
    try:
        return input().strip().lower() == "yes"
    except EOFError:
        return False


async def run(args: argparse.Namespace) -> int:
    problems: list[str] = []
    telephony = TelephonyConfig.from_env(problems)
    quality = VoiceQualityConfig.from_env(problems)
    if problems:
        print("Configuration problems:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return EXIT_CANNOT_PLACE
    try:
        telephony.require_outbound()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_CANNOT_PLACE

    url = stream_url(telephony.public_url or "", telephony.stream_path)
    machine_detection = args.machine_detection or telephony.machine_detection
    print("=== the plan ===")
    print(f"  carrier            {telephony.provider}")
    print(f"  from               {telephony.from_number}")
    print(f"  to                 {args.to}")
    print(f"  audio streams to   {url}")
    print(f"  carrier AMD        {machine_detection}")
    print(f"  bot report dir     {quality.call_report_dir or 'OFF — set CALL_REPORT_DIR; the bot side cannot be checked without it'}")
    if not url.startswith("wss://"):
        print("  WARNING: the stream URL is not wss://; most carriers refuse a plain ws:// stream")

    if not args.yes and not _confirm(args.to, telephony):
        print("Not placing the call.")
        return EXIT_CANNOT_PLACE

    provider = make_provider(telephony)
    checks = Checks()
    try:
        print("\n=== preflight ===")
        try:
            account = await provider.check_credentials()
            checks.check("carrier credentials accepted", True, account)
        except (TelephonyError, NotImplementedError) as exc:
            checks.check("carrier credentials accepted", False, str(exc)[:120])
            return EXIT_CANNOT_PLACE

        print(SCRIPT)
        request = CallRequest(
            to_number=args.to,
            from_number=telephony.from_number or "",
            stream_url=url,
            answer_timeout_secs=args.answer_timeout or telephony.answer_timeout_secs,
            parameters={
                PARAM_DIRECTION: DIRECTION_OUTBOUND,
                PARAM_FROM: telephony.from_number or "",
                PARAM_TO: args.to,
                "live_check": "1",
            },
            machine_detection=machine_detection,
        )

        print("=== placing the call ===")
        started = time.monotonic()
        try:
            snapshot = await provider.place_call(request)
        except TelephonyError as exc:
            checks.check("the carrier accepted the call", False, str(exc).splitlines()[0][:140])
            print(f"\n{exc}\n", file=sys.stderr)
            return EXIT_CANNOT_PLACE
        call_id = snapshot.call_id
        checks.check("the carrier accepted the call", True, f"call={call_id} status={snapshot.status.value}")

        # Reuse call.py's poller: the same transitions, the same log lines.
        import call as call_cli

        final = await call_cli.watch(
            provider,
            call_id,
            until="end",
            poll_secs=1.0,
            max_secs=args.max_secs,
            hang_up_machines=False,
        )
        elapsed = time.monotonic() - started
        print("\n=== the carrier's side ===")
        checks.check("the call reached a final status", final.status.is_final, f"{final.status.value} after {elapsed:.0f}s")
        checks.check("somebody (or something) answered", final.status.reached_person, final.status.explain())
        if final.duration_secs is not None:
            print(f"  billed duration      {final.duration_secs:.0f}s")
        if machine_detection != "off":
            checks.check(
                "the carrier's answer detection reported a verdict",
                final.answered_by is not None,
                str(final.answered_by),
                required=False,
            )
            if args.expect_voicemail:
                checks.check("and it said machine", final.machine_answered, str(final.answered_by), required=False)
            else:
                checks.check("and it said human", final.answered_by in (None, "human", "unknown"), str(final.answered_by), required=False)
        if final.error_code or final.error_message:
            checks.check("the carrier reported no error", False, f"{final.error_code} {final.error_message}", required=False)

        print("\n=== the bot's side ===")
        report = await wait_for_report(quality.call_report_dir, call_id, timeout=args.report_wait)
        checks.check("the bot wrote a report for this call", report is not None, "" if report else "no CALL_REPORT_DIR/<call id>.json appeared; check the bot log for 'CALL | audio connected'")
        if report is None:
            return EXIT_CHECK_FAILED

        print(f"  ended by             {report.get('ended_by')}")
        print(f"  bot's call duration  {report.get('call_duration_secs')}s")
        checks.check("the agent greeted", report.get("greeted") is True, f"first audio at {report.get('greeting_at_secs')}s")
        turns = report.get("turns", [])
        spoken = [t for t in turns if t.get("transcript")]
        print("  what it heard:")
        for turn in spoken:
            flags = []
            if turn.get("interrupted_bot"):
                flags.append("interrupted the agent")
            if turn.get("spurious"):
                flags.append("no words")
            if turn.get("failed"):
                flags.append(f"FAILED: {turn['failed']}")
            latency = f"{turn['release_to_audio_ms']}ms to reply" if turn.get("release_to_audio_ms") is not None else "no reply"
            print(f"    {turn['turn']:>2}. {turn['transcript']!r}  ({latency}{'; ' + ', '.join(flags) if flags else ''})")
        checks.check(f"at least {args.min_turns} caller turn(s) were heard", len(spoken) >= args.min_turns, f"{len(spoken)} turn(s) with words")
        checks.check("every caller turn got a reply", report.get("failed_turn_count", 0) == 0, f"{report.get('failed_turn_count')} failed")
        barge_ins = report.get("barge_ins", [])
        real = [b for b in barge_ins if not b.get("spurious")]
        spurious = [b for b in barge_ins if b.get("spurious")]
        checks.check("an interruption was observed", len(real) >= 1, f"{len(real)} real, {len(spurious)} spurious", required=args.expect_barge_in)
        if real:
            latencies = [b["stop_latency_ms"] for b in real if b.get("stop_latency_ms") is not None]
            checks.check("the agent stopped within 1.5s when interrupted", bool(latencies) and max(latencies) <= 1500, f"stop latency {latencies}ms")
            for b in real:
                if b.get("spoken_before_cut"):
                    print(f"  cut off after: {b['spoken_before_cut']!r}")
        checks.check("no interruption was taken for noise", not spurious, f"{len(spurious)} spurious; noise resumes {report.get('noise_resumes')}", required=False)
        stages = report.get("latency", {}).get("stages", {})
        total = stages.get("total", {})
        if total.get("n"):
            print("  latency (from the caller's silence):")
            for name in ("total", "turn-end", "stt", "llm", "tts"):
                figures = stages.get(name, {})
                if figures.get("n"):
                    print(f"    {name:<9} p50 {figures['p50_ms']}ms  p95 {figures['p95_ms']}ms  max {figures['max_ms']}ms  n={figures['n']}")
            checks.check(f"p95 response latency under {args.max_total_ms}ms", total["p95_ms"] <= args.max_total_ms, f"p95 {total['p95_ms']}ms", required=False)
        else:
            checks.check("response latency was measured", False, "no responses measured", required=False)
        verdict = report.get("voicemail", {})
        if args.expect_voicemail:
            checks.check("the bot detected the machine", verdict.get("detected") is True, str(verdict)[:140])
        else:
            checks.check("the bot did not mistake you for a machine", verdict.get("detected") is not True, str(verdict)[:140])
        checks.check("no service errors during the call", not report.get("errors"), str(report.get("errors"))[:140])
        checks.check("no stop timeouts", report.get("stop_timeouts", 0) == 0, str(report.get("stop_timeouts")), required=False)
        if report.get("service_failures"):
            checks.check("no service failures", False, str(report["service_failures"]), required=False)
    finally:
        await provider.close()

    print()
    failed = checks.failed
    if failed:
        print(f"{len(failed)} required check(s) FAILED:")
        for label in failed:
            print(f"  - {label}")
        return EXIT_CHECK_FAILED
    print("All required checks passed.")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live_call.py",
        description="Place one real call to your own phone and check both sides of it.",
    )
    parser.add_argument("--to", required=True, help="the number to call, in E.164: +923001234567. Your own phone.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--machine-detection", choices=("off", "async", "sync"), help="override TELEPHONY_MACHINE_DETECTION for this call")
    parser.add_argument("--answer-timeout", type=int, help="seconds to let it ring")
    parser.add_argument("--max-secs", type=float, default=300.0, help="stop watching after this long (default 300)")
    parser.add_argument("--report-wait", type=float, default=45.0, help="how long to wait for the bot's report after the call ends (default 45)")
    parser.add_argument("--min-turns", type=int, default=2, help="caller turns the report must show (default 2)")
    parser.add_argument("--expect-barge-in", action="store_true", help="fail unless an interruption was observed")
    parser.add_argument("--expect-voicemail", action="store_true", help="you are calling a voicemail on purpose")
    parser.add_argument("--max-total-ms", type=int, default=4000, help="p95 response latency to warn above (default 4000)")
    return parser


def main() -> int:
    args = _parser().parse_args()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss.SSS}</green> | {message}")
    try:
        return asyncio.run(run(args))
    except (ConfigError, TelephonyError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_CANNOT_PLACE
    except KeyboardInterrupt:
        print("\nStopped. If the call was placed it may still be up: uv run call.py --hang-up <id>", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
