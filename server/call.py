#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Place an outbound phone call to the running agent, and watch what happens.

This is the command line for `src/telephony/`. `bot.py` answers calls; this one
starts them.

Run it from the `server/` directory, with the bot already running and reachable
from the internet::

    uv run bot.py                       # terminal 1
    ngrok http 7860                     # terminal 2 — copy the https URL into
                                        #   TELEPHONY_PUBLIC_URL in .env
    uv run call.py +923001234567        # terminal 3

**What actually happens.** This asks the carrier to dial the number and, when it
is answered, to open a websocket back to the bot's `/ws` endpoint and stream the
call's audio over it both ways. The carrier is the only party that talks to both
ends: this process places the call and then does nothing but watch it, and the
bot has no idea a call was placed until the audio arrives.

That separation is why this is a separate command rather than something inside
the bot. The bot is one process per session, started by the carrier's
connection; the thing that decides to make a call is upstream of that, and in a
later phase it will be a campaign runner rather than a person typing a number.

**Exit codes**, because a script driving this needs to tell the three outcomes
apart::

    0   the call was answered and ended normally
    1   the call could not be placed — a configuration or carrier problem
    2   the call was placed and nobody was reached (busy, no answer, failed)

Other useful invocations::

    uv run call.py +923001234567 --wait answer   # return as soon as it is picked up
    uv run call.py +923001234567 --wait none     # place it and exit immediately
    uv run call.py --hang-up CA1234...           # abandon a call that is still up
    uv run call.py +923001234567 --dry-run       # print what would be sent, dial nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from dotenv import load_dotenv
from loguru import logger

from src.config import ConfigError, TelephonyConfig
from src.reliability.observability import event
from src.telephony import (
    PARAM_DIRECTION,
    PARAM_FROM,
    PARAM_TO,
    CallRequest,
    CallSnapshot,
    CallStatus,
    TelephonyError,
    TelephonyProvider,
    build_stream_twiml,
    make_provider,
    stream_url,
)
from src.telephony.session import DIRECTION_OUTBOUND

load_dotenv(override=True)

EXIT_OK = 0
EXIT_CANNOT_PLACE = 1
EXIT_NOT_REACHED = 2


def build_request(config: TelephonyConfig, args: argparse.Namespace) -> CallRequest:
    """Assemble the call to place, from configuration and command-line overrides.

    The three `parameters` are the only channel an outbound call has for telling
    the agent anything about itself. Twilio's media-stream handshake carries the
    call id and the stream id and nothing else — not the number it dialled, not
    even that the call was outbound — so without these the bot would answer a
    call it placed and have no idea who it is talking to. `session.py` reads them
    back out at the other end.
    """
    url = args.stream_url or stream_url(config.public_url or "", config.stream_path)
    to_number = args.to
    from_number = args.from_number or config.from_number or ""

    return CallRequest(
        to_number=to_number,
        from_number=from_number,
        stream_url=url,
        answer_timeout_secs=args.answer_timeout or config.answer_timeout_secs,
        parameters={
            PARAM_DIRECTION: DIRECTION_OUTBOUND,
            PARAM_FROM: from_number,
            PARAM_TO: to_number,
        },
        # Phase 12: the carrier's answering-machine detection, from .env or
        # overridden for one call.
        machine_detection=args.machine_detection or config.machine_detection,
        # Phase 14: ask the carrier to push the call's events to the receiver
        # as well. This command still watches by polling — a one-off call has
        # no attempt row for a webhook to update — but the ledger records what
        # the carrier sent, which is how a receiver is checked end to end
        # before a campaign depends on it (`campaign.py webhooks`).
        status_callback_url=None if args.no_webhooks else config.webhook_url(),
    )


async def watch(
    provider: TelephonyProvider,
    call_id: str,
    *,
    until: str,
    poll_secs: float,
    max_secs: float,
    hang_up_machines: bool = False,
) -> CallSnapshot:
    """Poll the carrier until the call reaches the state we are waiting for.

    **Why this still polls, now that webhooks exist (Phase 14).** The carrier
    pushes a call's events to the receiver, which writes them onto the call's
    *attempt row* — and a call placed by this command has no attempt row: it
    is one person dialling one number, outside any campaign. So this process
    watches the call the way it always has, once a second, and the receiver
    records the pushed events as `unmatched` on the ledger. The campaign paths
    (`campaign.py call`, `campaign.py run`) are the ones the webhooks update.

    Every transition is logged with the time it took to get there, so a run
    leaves behind the answer to "how long did it ring" without any extra
    measurement.

    Args:
        provider: The carrier to ask.
        call_id: The call to watch.
        until: `"answer"` returns as soon as somebody picks up; `"end"` waits
            for the call to finish.
        poll_secs: Seconds between requests.
        max_secs: Give up watching after this long. The call itself is not
            affected — this only stops looking.
        hang_up_machines: Phase 12. End the call as soon as the carrier's
            detection reports a machine. The bot does the same on its own
            when it is polling; this covers a bot that is not.

    Returns:
        The last snapshot taken.
    """
    started = time.monotonic()
    previous: CallStatus | None = None
    answered_by_seen: str | None = None
    snapshot = await provider.fetch_call(call_id)

    while True:
        if snapshot.status is not previous:
            elapsed = time.monotonic() - started
            logger.info(
                f"CALL | {snapshot.status.value} after {elapsed:.1f}s — {snapshot.status.explain()}"
            )
            previous = snapshot.status

        # Phase 12: the carrier's answering-machine verdict, when detection was
        # requested. Logged once, as a structured event, the moment it appears.
        if snapshot.answered_by and snapshot.answered_by != answered_by_seen:
            answered_by_seen = snapshot.answered_by
            elapsed = time.monotonic() - started
            logger.info(
                event(
                    "call.answered_by",
                    call=call_id,
                    outcome=snapshot.answered_by,
                    latency_ms=int(elapsed * 1000),
                )
            )
            if snapshot.machine_answered and hang_up_machines and not snapshot.status.is_final:
                logger.warning(
                    event("voicemail.detected", call=call_id, provider="carrier_amd", outcome="hangup")
                )
                await provider.hang_up(call_id)

        if snapshot.status.is_final:
            return snapshot
        if until == "answer" and snapshot.status.reached_person:
            return snapshot
        if time.monotonic() - started > max_secs:
            logger.warning(
                f"CALL | stopped watching after {max_secs:.0f}s; the call may still be up. "
                f"Check it with:  uv run call.py --hang-up {call_id}"
            )
            return snapshot

        await asyncio.sleep(poll_secs)
        snapshot = await provider.fetch_call(call_id)


async def command_call(config: TelephonyConfig, args: argparse.Namespace) -> int:
    """Place one outbound call and report how it went."""
    config.require_outbound()
    request = build_request(config, args)

    if args.dry_run:
        print(f"Would call {request.to_number} from {request.from_number}")
        print(f"Audio would stream to {request.stream_url}")
        if request.status_callback_url:
            print(f"Call events would be POSTed to {request.status_callback_url}\n")
        else:
            print(f"No call events would be pushed: {config.describe_webhooks()}\n")
        print(build_stream_twiml(request))
        return EXIT_OK

    provider = make_provider(config)
    logger.info(
        f"CALL | dialling {request.to_number} from {request.from_number} "
        f"via {provider.describe()}; audio to {request.stream_url}"
    )
    started = time.monotonic()

    try:
        snapshot = await provider.place_call(request)
        if args.wait == "none":
            return EXIT_OK

        snapshot = await watch(
            provider,
            snapshot.call_id,
            until=args.wait,
            poll_secs=args.poll,
            max_secs=args.max_wait,
            hang_up_machines=args.hang_up_machines,
        )
    finally:
        await provider.close()

    return _report(snapshot, time.monotonic() - started)


async def command_hang_up(config: TelephonyConfig, args: argparse.Namespace) -> int:
    """End a call that is still ringing or in progress."""
    provider = make_provider(config)
    try:
        await provider.hang_up(args.hang_up)
    finally:
        await provider.close()
    return EXIT_OK


def _report(snapshot: CallSnapshot, elapsed: float) -> int:
    """Log the outcome and turn it into an exit code."""
    logger.info(f"CALL | {snapshot.describe()}")

    if snapshot.status.reached_person:
        # `duration_secs` is the carrier's billed time, measured from the moment
        # the call was answered; `elapsed` includes the time it spent ringing.
        billed = f", {snapshot.duration_secs:.0f}s answered" if snapshot.duration_secs else ""
        if snapshot.machine_answered:
            logger.warning(
                f"CALL | an answering machine picked up ({elapsed:.1f}s total{billed})"
            )
            return EXIT_NOT_REACHED
        logger.info(f"CALL | reached the other end ({elapsed:.1f}s total{billed})")
        return EXIT_OK

    if not snapshot.status.is_final:
        # We stopped watching before the call finished. Nothing is known to have
        # gone wrong, so this is not a failure.
        logger.info(f"CALL | still {snapshot.status.value} when we stopped watching")
        return EXIT_OK

    logger.warning(f"CALL | not reached: {snapshot.status.explain()} (after {elapsed:.1f}s)")
    if snapshot.error_message:
        logger.warning(f"CALL | the carrier said: {snapshot.error_message}")
    return EXIT_NOT_REACHED


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="call.py",
        description="Place an outbound phone call that connects to the running voice agent.",
    )
    parser.add_argument(
        "to",
        nargs="?",
        help="the number to call, in E.164 form: +923001234567",
    )
    parser.add_argument(
        "--from",
        dest="from_number",
        help="caller ID to present. Defaults to TELEPHONY_FROM_NUMBER.",
    )
    parser.add_argument(
        "--stream-url",
        help=(
            "where the carrier should stream the call's audio. Defaults to "
            "TELEPHONY_PUBLIC_URL + TELEPHONY_STREAM_PATH."
        ),
    )
    parser.add_argument(
        "--answer-timeout",
        type=int,
        help="seconds to let it ring before giving up. Defaults to TELEPHONY_ANSWER_TIMEOUT_SECS.",
    )
    parser.add_argument(
        "--wait",
        choices=("none", "answer", "end"),
        default="end",
        help=(
            "how long to watch the call: 'none' exits once it is placed, 'answer' once somebody "
            "picks up, 'end' when the call finishes (default)."
        ),
    )
    parser.add_argument(
        "--poll", type=float, default=1.0, help="seconds between status checks (default 1.0)"
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=600.0,
        help="stop watching after this many seconds (default 600). Does not end the call.",
    )
    parser.add_argument(
        "--hang-up",
        metavar="CALL_ID",
        help="end a call that is already up, instead of placing one",
    )
    parser.add_argument(
        "--machine-detection",
        choices=("off", "async", "sync"),
        help=(
            "ask the carrier to detect an answering machine (Phase 12). Defaults to "
            "TELEPHONY_MACHINE_DETECTION."
        ),
    )
    parser.add_argument(
        "--hang-up-machines",
        action="store_true",
        help="end the call as soon as the carrier reports a machine answered (needs detection on)",
    )
    parser.add_argument(
        "--no-webhooks",
        action="store_true",
        help="do not ask the carrier to push this call's events to the receiver (Phase 14)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request and the TwiML that would be sent, and dial nothing",
    )
    return parser


def main() -> int:
    """Parse arguments and run."""
    args = _parser().parse_args()

    if not args.hang_up and not args.to:
        _parser().error("give a number to call, or --hang-up CALL_ID")

    try:
        config = TelephonyConfig.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_CANNOT_PLACE

    command = command_hang_up if args.hang_up else command_call
    try:
        return asyncio.run(command(config, args))
    except (ConfigError, TelephonyError) as exc:
        # Everything that means "the call was never made": missing settings, a
        # number the carrier would not dial, credentials it would not accept.
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_CANNOT_PLACE
    except KeyboardInterrupt:
        print("\nStopped watching. The call, if it was placed, is still running.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
