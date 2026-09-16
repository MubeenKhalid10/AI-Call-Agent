#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Serve the calling dashboard. Phase 10.

Run it from the `server/` directory, alongside whatever else is running::

    uv run dashboard.py                 # http://127.0.0.1:7870
    uv run dashboard.py --port 8080
    uv run dashboard.py --once          # print the numbers as JSON and exit

**A separate process from the bot, on purpose.** `bot.py` answers calls; this
reads the database. They share PostgreSQL and nothing else, which is the same
seam `campaign.py`, `call.py`, `ingest.py` and `health.py` already use. Putting
a reporting page inside the runner would mean a page refresh and a live phone
call in one process, and the phone call is the one that must not be disturbed.

**It only reads.** No route writes, and the store methods it calls are the
reporting aggregates. Pointing this at a system that is dialling real people
cannot change what it does.

**It demands a login (Phase 18).** Users come from `DASHBOARD_USERS`
(`name:role:hash`; `uv run security.py hash-password` makes the hash), the
session is a signed cookie, and a viewer sees the phone numbers masked.
Without any user configured it refuses to start. `DASHBOARD_AUTH_DISABLED=true`
restores Phase 10's login-free page for local use — and then `--host` may
only be loopback, because a page of names and numbers with no login is a
contact list to whoever can reach the port.

**It still binds to loopback unless told otherwise.** `--host 0.0.0.0` prints
what that means: the login protects the data, but the cookie and the
password travel in clear unless something in front terminates TLS
(`SECURITY_REQUIRE_HTTPS=true`; see SECURITY.md).

`--once` exists for the terminal and for scripts: the same JSON the page reads,
without starting a server (and without a login — it reads the database
directly, as `campaign.py` does).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys

import uvicorn
from dotenv import load_dotenv

from src.campaigns import CampaignStore, CampaignStoreError
from src.config import Config, ConfigError
from src.dashboard import API_PATH, collect, create_app
from src.reliability import configure_logging

load_dotenv(override=True)
# Phase 9's logging: credentials scrubbed from every line, `LOG_FORMAT=json`
# honoured. Must run after `load_dotenv`.
configure_logging(component="dashboard")

EXIT_OK = 0
EXIT_FAILED = 1

DEFAULT_PORT = 7870  # Not 7860: the bot's runner owns that one.


async def print_once(config: Config) -> int:
    """Print the dashboard's JSON and exit, without starting a server."""
    store = await CampaignStore.connect(config.database_url or "")
    try:
        snapshot = await collect(store, timezone=config.calendar.timezone)
    finally:
        await store.close()
    print(json.dumps(snapshot.to_dict(), indent=2, default=str))
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
        f"  The login protects the data, but the password and the session cookie travel in\n"
        f"  clear unless TLS is terminated in front (SECURITY_REQUIRE_HTTPS=true; see SECURITY.md).\n",
        file=sys.stderr,
    )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "")


def main() -> int:
    """Parse arguments and serve, or print once."""
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description="Serve a read-only dashboard over the campaign database.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind. Loopback by default; the page has no login.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument(
        "--once",
        action="store_true",
        help="print the dashboard's JSON to stdout and exit, starting no server",
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    if not config.database_url:
        print(
            "\nNo database is configured, so there is nothing to report on.\n"
            "  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to).\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    if args.once:
        try:
            return asyncio.run(print_once(config))
        except CampaignStoreError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return EXIT_FAILED
        except KeyboardInterrupt:
            return 130

    security = config.security
    if security.dashboard_auth_disabled and not _is_loopback(args.host):
        print(
            f"\nDASHBOARD_AUTH_DISABLED=true and --host {args.host}: a page of names and phone numbers\n"
            f"  with no login must not be reachable from the network. Either configure DASHBOARD_USERS\n"
            f"  (`uv run security.py hash-password`) or serve on 127.0.0.1.\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        app = create_app(config)
    except (CampaignStoreError, ConfigError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    _warn_if_public(args.host)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    scheme = "https" if security.require_https else "http"
    print(f"\n  Dashboard   {scheme}://{shown}:{args.port}")
    print(f"  JSON        {scheme}://{shown}:{args.port}{API_PATH}   (session cookie, or Authorization: Bearer <API key>)")
    print(f"  Times in    {config.calendar.timezone}   (CALENDAR_TIMEZONE)")
    print(f"  Security    {security.describe()}")
    print(f"  Monitoring  {scheme}://{shown}:{args.port}/healthz  /readyz  /metrics   ({config.monitoring.describe()})")
    if security.dashboard_auth_disabled:
        print("  Login       OFF (DASHBOARD_AUTH_DISABLED) — loopback only")
    else:
        print(f"  Login       {', '.join(security.users.names())}   (DASHBOARD_USERS)")
    print()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        return 130
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
