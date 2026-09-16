#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Receive the carrier's call events in a process of their own. Phase 14.

Run it from the `server/` directory, when `.env` says
`TELEPHONY_WEBHOOK_RECEIVER=standalone`::

    uv run webhooks.py                  # http://127.0.0.1:7880/webhooks/telephony
    uv run webhooks.py --port 8080
    uv run webhooks.py --host 0.0.0.0   # reachable from the network

**When to use this rather than the bot's own route.** By default the bot
mounts the webhook route on its runner's web server, because a single tunnel
gives you one public address and `/ws` already has to be on it. That handler
does an HMAC and two short database writes and never touches a pipeline, so
it is not in the audio path in any sense that matters — but a deployment that
would rather keep every non-audio request out of the bot's process can: point
the public address's `TELEPHONY_WEBHOOK_PATH` at this instead, set the
receiver to `standalone`, and the bot mounts nothing. Same processor, same
ledger, same idempotency; only the lifecycle differs.

**It writes.** Unlike the dashboard, this process changes call state — that
is its purpose — which is why every delivery is verified with the carrier's
signature before anything reads it. A forged or unsigned event is refused
with 403 and touches nothing. The signing secret is the carrier's own
(`TWILIO_AUTH_TOKEN`, or `SIGNALWIRE_SIGNING_KEY`), never a shared password
in a URL.

**It binds to loopback unless told otherwise.** In production a proxy in
front of the public address forwards the path here; in development the bot's
own route is the easier path and this is not needed at all.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn
from dotenv import load_dotenv

from src.campaigns import CampaignStoreError, create_webhook_app
from src.config import Config, ConfigError
from src.reliability import configure_logging

load_dotenv(override=True)
configure_logging(component="webhooks")

EXIT_OK = 0
EXIT_FAILED = 1

DEFAULT_PORT = 7880  # Not 7860 (the bot) and not 7870 (the dashboard).


def main() -> int:
    """Parse arguments and serve."""
    parser = argparse.ArgumentParser(
        prog="webhooks.py",
        description="Receive carrier call-status webhooks in a process of their own.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind. Loopback by default; put a proxy in front for the carrier.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    telephony = config.telephony
    if telephony.webhook_receiver != "standalone":
        print(
            "\nTELEPHONY_WEBHOOK_RECEIVER is 'bot', so the bot serves the webhook route itself\n"
            "  and this process would receive nothing. Set TELEPHONY_WEBHOOK_RECEIVER=standalone\n"
            "  to serve it from here instead.\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        app = create_webhook_app(config)
    except (ConfigError, CampaignStoreError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"\n  Receiving   http://{shown}:{args.port}{telephony.webhook_path}")
    print(f"  Carrier     will POST to {telephony.webhook_url()}")
    print(f"  Ping        http://{shown}:{args.port}/api/ping")
    print(f"  Monitoring  http://{shown}:{args.port}/healthz  /readyz  /metrics\n")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        return 130
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
