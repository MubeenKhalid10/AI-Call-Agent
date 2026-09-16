"""Serve the unified application: every page, one login, one address. Phase 24.

Run it from the `server/` directory, with the bot up beside it::

    uv run bot.py                          # terminal 1: answers the calls (port 7860)
    uv run app.py                          # terminal 2: http://127.0.0.1:7900/app/
    uv run app.py --with-scheduler         # ... and dial ACTIVE campaigns from a child `campaign.py run`
    uv run app.py --port 8080 --host 0.0.0.0 --bot-url https://bot.example.com

**What it serves.** The browser application (`web/`), the dashboard at
`/dashboard` and the automation API at `/automation` — the two servers that
already existed, mounted unchanged — plus the session, configuration,
health and knowledge-base routes the pages needed. Sign in with a
`DASHBOARD_USERS` account; the same session drives every page.

**What it does not do on its own.** Dial. Starting a campaign here marks it
`ACTIVE`; the calls are placed by the scheduler, which is a process of its
own (`uv run campaign.py run`). `--with-scheduler` starts that process as a
child of this one, so a single command runs the whole loop — and it dials
real numbers for every `ACTIVE` campaign, as `campaign.py run` always has.

**It binds to loopback unless told otherwise.** Behind a TLS proxy set
`SECURITY_REQUIRE_HTTPS=true` (see SECURITY.md); the login and the session
cookie travel in clear otherwise.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import subprocess
import sys
import threading

import uvicorn
from dotenv import load_dotenv

from src.app import APP_PATH, DEFAULT_APP_PORT, create_unified_app
from src.campaigns import CampaignStoreError
from src.config import Config, ConfigError
from src.reliability import configure_logging

load_dotenv(override=True)
configure_logging(component="app")

EXIT_OK = 0
EXIT_FAILED = 1


def _warn_if_public(host: str) -> None:
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


class _Scheduler:
    """`campaign.py run` as a child process, stopped with this one."""

    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        command = [sys.executable, "campaign.py", "run", *self.args]
        self.process = subprocess.Popen(command, cwd=os.path.dirname(os.path.abspath(__file__)), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        print(f"  Scheduler        started as pid {self.process.pid} (`campaign.py run {' '.join(self.args)}`); it dials every ACTIVE campaign")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        # Ctrl+C once: stop placing and let the calls in progress finish.
        try:
            if sys.platform == "win32":
                self.process.terminate()
            else:
                import signal

                self.process.send_signal(signal.SIGINT)
            self.process.wait(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.py", description="Serve the unified application over the existing dashboard and automation API.")
    parser.add_argument("--host", default=os.getenv("APP_HOST") or "127.0.0.1", help="interface to bind (default APP_HOST, 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT") or DEFAULT_APP_PORT), help=f"port (default APP_PORT, {DEFAULT_APP_PORT})")
    parser.add_argument("--bot-url", default=os.getenv("APP_BOT_URL") or "http://127.0.0.1:7860", help="where the bot's browser client is served, for the Live Agent page (default APP_BOT_URL, http://127.0.0.1:7860)")
    parser.add_argument(
        "--proxy-bot",
        action="store_true",
        default=(os.getenv("APP_PROXY_BOT") or "").strip().lower() in ("1", "true", "yes", "on"),
        help="forward every path this application does not own (/client, /api/offer, /ws, POST /) to --bot-url, so one public hostname — one ngrok tunnel to this port — serves the application and the bot (default APP_PROXY_BOT)",
    )
    parser.add_argument("--with-scheduler", action="store_true", help="run `campaign.py run` as a child process instead of the engine inside this one")
    parser.add_argument("--no-engine", action="store_true", help="do not dial from this process at all (WORKER_EMBEDDED=false): `campaign.py run` places the calls")
    parser.add_argument("--no-deliver", action="store_true", help="do not run the n8n outbox deliverer in this process")
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED
    if not config.database_url:
        print("\nNo database is configured, so there is nothing to serve.\n  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to).\n", file=sys.stderr)
        return EXIT_FAILED

    try:
        # Phase 25: the engine dials from inside this process unless told
        # not to — or unless a child `campaign.py run` is asked for instead.
        embedded = config.worker.embedded and not args.no_engine and not args.with_scheduler
        stream = (os.getenv("APP_STREAM_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "off")
        app = create_unified_app(config, bot_url=args.bot_url, deliver=not args.no_deliver, engine=embedded, proxy_bot=args.proxy_bot, stream=stream)
    except (ConfigError, CampaignStoreError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    _warn_if_public(args.host)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"\n  Application      http://{shown}:{args.port}{APP_PATH}/")
    print(f"  Sign in as       {', '.join(config.security.users.names()) if config.security.users else 'nobody configured (DASHBOARD_USERS)'}")
    if args.proxy_bot:
        print(f"  Bot client       http://{shown}:{args.port}/client   (forwarded to {args.bot_url}; one tunnel to this port serves the application and the bot: ngrok http {args.port})")
    else:
        print(f"  Bot client       {args.bot_url}/client   (uv run bot.py; the Live Agent page frames it)")
    print(f"  Dashboard        http://{shown}:{args.port}/dashboard/   API  http://{shown}:{args.port}/automation/api/v1")
    print(f"  Monitoring       http://{shown}:{args.port}/healthz  /readyz  /metrics")
    scheduler = _Scheduler([]) if args.with_scheduler else None
    if scheduler is not None:
        scheduler.start()
    elif embedded:
        carrier = config.telephony
        if carrier.is_configured and carrier.has_credentials:
            print(f"  Engine           inside this process: dials every ACTIVE campaign through {carrier.provider} from {carrier.from_number}; the bot must be up at {carrier.public_url}")
        else:
            print("  Engine           idle: no outbound carrier configured (TELEPHONY_PROVIDER, credentials, TELEPHONY_FROM_NUMBER, TELEPHONY_PUBLIC_URL); nothing is dialled")
    else:
        print("  Engine           off (--no-engine / WORKER_EMBEDDED=false): `uv run campaign.py run` places the calls")
    print()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        return 130
    finally:
        if scheduler is not None:
            threading.Thread(target=scheduler.stop, daemon=True).start()
            scheduler.stop()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
