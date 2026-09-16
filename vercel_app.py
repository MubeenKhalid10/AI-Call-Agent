"""The unified application as a Vercel Function. Phase 35.

    Vercel → Import the repository (Root Directory: the repository root)
           → Environment variables (the list below)
           → Deploy
    https://<project>.vercel.app/app/

Vercel finds this file through ``[tool.vercel] entrypoint`` in the root
``pyproject.toml``, installs that file's dependencies (pinned to the same
versions as ``server/uv.lock``) and runs the ``app`` below as one function
with Fluid compute. FastAPI lifespan events run, so the application's pools
open per instance exactly as under ``uv run app.py``.

**What runs here and what does not.** This is the *application*: the page,
the dashboard and the automation API over PostgreSQL, for as many people as
open it. Nothing long-lived runs in a function, so:

- the campaign engine is off (``WORKER_EMBEDDED=false``): campaigns are
  created, started and watched here, but the calls are placed by a process
  that stays up — ``uv run app.py`` or ``uv run campaign.py run`` on a
  machine, pointed at the **same** ``DATABASE_URL``;
- the bot is not here either: ``APP_BOT_URL`` must be the bot's public
  address (the ngrok tunnel, or wherever ``bot.py`` is hosted), and the
  Live Agent page frames its ``/client`` from there. ``APP_PROXY_BOT`` is
  never on in this file (a function cannot relay the carrier's WebSocket);
- the n8n outbox deliverer is off (``deliver=False``); run ``automation.py``
  beside the engine if n8n deliveries are wanted;
- the live event stream (``/api/app/stream``) is off (``APP_STREAM_ENABLED``
  defaults to ``false`` here): a server-sent-events response would hold a
  function open for its whole run. The page notices and polls every 15 s
  instead, which is what it does whenever the stream is unavailable;
- the knowledge base defaults to off (``KB_ENABLED=false``) because its
  embedder (fastembed + an ONNX model) is not installed here. Set it on and
  add ``fastembed`` to the root ``pyproject.toml`` only if the bundle stays
  under Vercel's limit.

**Environment variables to set in the Vercel project** (Settings →
Environment Variables), the same names as ``server/.env``:

    DATABASE_URL                a hosted PostgreSQL the laptop can reach too
                                (Supabase: the *session pooler* URL — the
                                direct host is IPv6-only, and Vercel is IPv4;
                                Neon: the pooled URL). Add ``?sslmode=require``.
                                Run ``uv run campaign.py init`` against it once.
    DASHBOARD_USERS             who can sign in (``uv run security.py add-user``)
    DASHBOARD_SESSION_SECRET    REQUIRED here: without it every instance would
                                sign sessions with its own secret and people
                                would be logged out at random
    AUTOMATION_API_KEYS         the API keys (n8n, the checks)
    APP_BOT_URL                 https://<the bot's public address>
    SECURITY_REQUIRE_HTTPS      true  (Vercel terminates TLS and says so in
    SECURITY_TRUSTED_PROXIES    0.0.0.0/0,::/0   X-Forwarded-Proto, from its own
                                network, so trust every hop here)
    SALES_*, TELEPHONY_*, LLM_*, STT_*, TTS_*, CALENDAR_*, HUBSPOT_*, …
                                as in server/.env — the configuration page and
                                the campaign wizard read them; the bot (elsewhere)
                                needs its own copy

Vercel's own limits that show up in the page: a request or response body is
at most 4.5 MB (a larger CSV import or document upload is refused by the
platform with 413, before this code runs); one invocation runs for at most
``maxDuration`` (``vercel.json``).

Locally, the same file runs the same way::

    uv sync                                  # at the repository root: the slim environment
    uv run uvicorn vercel_app:app --port 7902

which reads ``server/.env`` (Vercel does not: it sets ``VERCEL=1`` and the
project's variables).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

# Serverless defaults. A variable set in the Vercel project (or, on a laptop,
# in the shell) wins over these; `server/.env` does not, so a local run of
# this file behaves as the deployment does.
os.environ.setdefault("WORKER_EMBEDDED", "false")
os.environ.setdefault("APP_STREAM_ENABLED", "false")
os.environ.setdefault("KB_ENABLED", "false")

if not os.getenv("VERCEL"):
    # A laptop run of this file: the bot's .env for everything else, without overriding the shell.
    from dotenv import load_dotenv

    load_dotenv(SERVER / ".env", override=False)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _build():  # noqa: ANN202 — a FastAPI either way
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from src.app import create_unified_app
    from src.config import Config, ConfigError

    try:
        config = Config.from_env()
        if not config.database_url:
            raise ConfigError("DATABASE_URL is not set; the application has nothing to serve.")
        return create_unified_app(
            config,
            bot_url=os.getenv("APP_BOT_URL") or "http://127.0.0.1:7860",
            web_dir=SERVER / "web",
            deliver=False,
            engine=False,
            proxy_bot=False,
            stream=_truthy(os.getenv("APP_STREAM_ENABLED")),
        )
    except ConfigError as exc:
        # Say what is missing on every path, instead of a function that fails to import.
        problem = str(exc)
        broken = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @broken.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
        async def unconfigured(path: str) -> JSONResponse:
            return JSONResponse(
                {"error": "the application is not configured", "detail": problem, "where": "Vercel → Settings → Environment Variables"},
                status_code=503,
            )

        return broken


app = _build()
