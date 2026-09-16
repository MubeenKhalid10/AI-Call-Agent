#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Is everything this agent depends on actually working? Phase 9.

Run it from the `server/` directory, before a calling session and after any
change to `.env`::

    uv run health.py                 # every component
    uv run health.py --json          # the same, machine-readable
    uv run health.py llm telephony   # only these

**Nothing here places a call, synthesises a word or runs an inference.** Each
check is the cheapest authenticated read the vendor offers, so this costs
nothing and rings nobody. What it proves is that a credential is accepted and a
service is reachable — plus, for the LLM, that the configured model still
exists, which is the failure this project has actually met: Groq rotates its
catalogue, and a retired model id turns into a bot that answers the phone and
says nothing.

What it deliberately does *not* prove is that a call would sound right. That is
what `evals/` is for, and it costs API calls and minutes.

**Exit codes**, so a script can gate a calling session on this::

    0   nothing is failed (some components may be degraded or skipped)
    1   at least one component failed
    2   the configuration could not be read at all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from dotenv import load_dotenv

from src.config import Config, ConfigError
from src.reliability import Status, check_health, configure_logging

load_dotenv(override=True)

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_NO_CONFIG = 2

COMPONENTS = ("application", "database", "scheduler", "knowledge", "stt", "llm", "tts", "tts_fallback", "telephony", "calendar", "crm")


async def run(config: Config, args: argparse.Namespace) -> int:
    """Check everything asked for and report it."""
    report = await check_health(
        config, timeout_secs=args.timeout or config.reliability.health_timeout_secs,
        only=tuple(args.components),
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return EXIT_OK if report.healthy else EXIT_UNHEALTHY

    print()
    for component in report.components:
        print(f"  {component.describe()}")
    print(f"\n{report.summary()}.")

    if report.degraded:
        print("\nWorth fixing before a calling session:")
        for component in report.degraded:
            print(f"  - {component.name}: {component.detail}")
    if not report.healthy:
        print("\nNot ready to place calls: a component failed.", file=sys.stderr)
        return EXIT_UNHEALTHY
    return EXIT_OK


def main() -> int:
    """Parse arguments and run the checks."""
    parser = argparse.ArgumentParser(
        prog="health.py",
        description="Check every dependency this agent needs, without placing a call.",
    )
    parser.add_argument(
        "components",
        nargs="*",
        choices=[*COMPONENTS, []],
        help="which components to check; omit for all",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--timeout", type=float, help="seconds to allow each check (default HEALTH_TIMEOUT_SECS)"
    )
    args = parser.parse_args()

    # Quiet by default: the report is the output, and a health check that prints
    # a page of DEBUG before it is one nobody reads.
    configure_logging(level="WARNING")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_NO_CONFIG

    try:
        return asyncio.run(run(config, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
