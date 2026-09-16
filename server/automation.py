#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Serve the automation API and deliver the outbound events. Phase 17.

Run it from the `server/` directory, beside the bot and the worker::

    uv run automation.py                 # API on http://127.0.0.1:7890/api/v1, deliverer on
    uv run automation.py --port 8090 --host 0.0.0.0
    uv run automation.py --no-deliver    # the API only; run the deliverer elsewhere
    uv run automation.py --once          # one delivery pass, no server (for cron)

**A separate process from the bot, on purpose.** `bot.py` answers calls;
`campaign.py run` places them; this takes requests from an automation
platform and tells it what happened. The three share PostgreSQL and nothing
else. Nothing n8n does can reach a pipeline, a frame or a session: a call it
asks for is a row the scheduler places on its next tick, and an event it is
sent is read from a row the bot wrote at the end of a call.

**It refuses to serve without a key.** Every write here can make a phone
ring, so `AUTOMATION_API_KEYS` must hold at least one key; the startup
message says how to make one. The deliverer needs `AUTOMATION_WEBHOOK_URL`
(or a per-kind URL) and is simply off without one.

**It binds to loopback unless told otherwise.** n8n on the same machine
reaches it there; n8n elsewhere needs `--host 0.0.0.0` behind something
that terminates TLS, or a tunnel, and the key in every request.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import ipaddress
import sys

import uvicorn
from dotenv import load_dotenv

from src.automation import (
    API_PREFIX,
    PING_PATH,
    AiohttpSender,
    ApiSettings,
    EventDeliverer,
    create_automation_app,
)
from src.campaigns import CampaignStore, CampaignStoreError
from src.config import Config, ConfigError
from src.reliability import configure_logging

load_dotenv(override=True)
# Phase 9's logging: credentials scrubbed from every line (the API keys and
# the webhook secret included), `LOG_FORMAT=json` honoured. After `load_dotenv`.
configure_logging(component="api")

EXIT_OK = 0
EXIT_FAILED = 1


async def deliver_once(config: Config) -> int:
    """Run one delivery pass and exit, without starting a server."""
    automation = config.automation
    if not automation.delivery_enabled:
        print(
            "\nNo webhook URL is configured, so there is nowhere to deliver events.\n"
            "  Set AUTOMATION_WEBHOOK_URL (or AUTOMATION_WEBHOOK_URL_<KIND>) in .env.\n",
            file=sys.stderr,
        )
        return EXIT_FAILED
    store = await CampaignStore.connect(config.database_url or "")
    sender = AiohttpSender(timeout_secs=automation.timeout_secs)
    deliverer = EventDeliverer(
        store,
        targets=automation.targets,
        secret=automation.webhook_secret,
        auth_header=automation.webhook_auth_header,
        auth_token=automation.webhook_auth_token,
        sender=sender,
        max_attempts=automation.max_attempts,
        retry_secs=automation.retry_secs,
        max_retry_secs=automation.max_retry_secs,
        batch=automation.batch,
        stale_secs=automation.stale_secs,
        settle_secs=automation.settle_secs,
        since=automation.events_since,
        include_transcript=automation.include_transcript,
    )
    try:
        report = await deliverer.run_once()
    finally:
        await sender.close()
        await store.close()
    print(f"\n{report.describe()}.")
    if report.notes:
        for note in report.notes:
            print(f"  - {note}")
    return EXIT_OK


def _warn_if_public(host: str) -> None:
    """Say plainly what binding to a network interface means here."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in ("localhost", "")
    if loopback:
        return
    print(
        f"\n  WARNING: serving on {host}, which is reachable from the network.\n"
        f"  Every request needs an API key, but the key travels in clear over plain HTTP.\n"
        f"  Put this behind TLS and set SECURITY_REQUIRE_HTTPS=true (see SECURITY.md),\n"
        f"  or use the default 127.0.0.1.\n",
        file=sys.stderr,
    )


def main() -> int:
    """Parse arguments and serve, or deliver once."""
    parser = argparse.ArgumentParser(
        prog="automation.py",
        description="Serve the automation API for n8n, and deliver the outbound events.",
    )
    parser.add_argument("--host", default=None, help="interface to bind (default AUTOMATION_HOST, 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default AUTOMATION_PORT, 7890)")
    parser.add_argument(
        "--no-deliver",
        action="store_true",
        help="serve the API only; do not run the event deliverer in this process",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one delivery pass to the webhook URL and exit; start no server",
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    if not config.database_url:
        print(
            "\nNo database is configured, so there is nothing to serve.\n"
            "  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to).\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    if args.once:
        try:
            return asyncio.run(deliver_once(config))
        except CampaignStoreError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return EXIT_FAILED
        except KeyboardInterrupt:
            return 130

    # A `--host` / `--port` override is folded into the settings, so the
    # startup log line and the banner agree on where the API listens.
    automation = dataclasses.replace(
        config.automation,
        host=args.host or config.automation.host,
        port=args.port or config.automation.port,
    )
    settings = dataclasses.replace(ApiSettings.from_config(config), automation=automation)
    try:
        app = create_automation_app(settings, deliver=not args.no_deliver)
    except (ConfigError, CampaignStoreError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    host = automation.host
    port = automation.port
    _warn_if_public(host)
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"\n  Automation API   http://{shown}:{port}{API_PREFIX}")
    print(f"  OpenAPI docs     http://{shown}:{port}{API_PREFIX}/docs")
    print(f"  Ping             http://{shown}:{port}{PING_PATH}")
    print(f"  Monitoring       http://{shown}:{port}/healthz  /readyz  /metrics   ({config.monitoring.describe()})")
    print(
        f"  Keys             {len(automation.api_keys)} admin, {len(automation.operator_api_keys)} operator, "
        f"{len(automation.viewer_api_keys)} viewer (AUTOMATION_*_API_KEYS)"
    )
    print(f"  Security         {config.security.describe()}")
    print(f"  Audit            GET {API_PREFIX}/audit (admin), or `uv run campaign.py audit`")
    if args.no_deliver:
        print("  Events           not delivered by this process (--no-deliver)")
    elif automation.delivery_enabled:
        targets = automation.targets
        print(f"  Events           {', '.join(sorted(targets))}")
        for kind, url in sorted(targets.items()):
            print(f"                   {kind:<20} -> {url}")
        print(
            f"                   {'signed (X-Aiva-Signature)' if automation.webhook_secret else 'UNSIGNED — set AUTOMATION_WEBHOOK_SECRET'}"
            f"{', header auth on' if automation.webhook_auth_token else ''}"
        )
    else:
        print("  Events           off (set AUTOMATION_WEBHOOK_URL to deliver call.completed and the rest)")
    print(f"  Calls            queued for `uv run campaign.py run`; this process never dials\n")

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        return 130
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
