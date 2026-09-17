"""The unified application as a Vercel Function.

    Vercel → Import the repository → Root Directory: server
           → Environment variables (see README, "Deploying the application to Vercel")
           → Deploy
    https://<project>.vercel.app/app/

Vercel loads this file through ``[tool.vercel] entrypoint`` in ``pyproject.toml``
and installs that file's base dependencies only: the ``voice`` extra (Pipecat,
the speech services, fastembed) stays out, and so does everything long-lived.

Vercel hosts the dashboard and the APIs. The campaign engine, the bot and the
outbox delivery run outside Vercel (``uv run app.py`` / ``campaign.py run`` /
``bot.py`` on a machine sharing the same ``DATABASE_URL``); ``APP_BOT_URL`` is
the bot's public address, which the Live Agent page frames.
"""

import asyncio
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.app.server import create_unified_app
from src.config import Config, ConfigError

# Local development: load .env.
# On Vercel, environment variables come from Vercel.
load_dotenv(override=True)

# Serverless defaults; a variable set in the Vercel project wins. No process
# stays up in a function (the engine), a server-sent-events response would hold
# one open (the stream), and the knowledge base's embedder is not installed here.
os.environ.setdefault("WORKER_EMBEDDED", "false")
os.environ.setdefault("APP_STREAM_ENABLED", "false")
os.environ.setdefault("KB_ENABLED", "false")
# The embedder's weights: the function's filesystem is read-only except /tmp,
# so fastembed's cache and the Hugging Face Hub download it goes through both
# live there (the hub's xet transfer writes its own cache too; plain HTTP is
# enough for one model).
if os.getenv("VERCEL") == "1":
    os.environ.setdefault("EMBEDDING_CACHE_DIR", "/tmp/fastembed")
    os.environ.setdefault("HF_HOME", "/tmp/huggingface")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("TMPDIR", "/tmp")

config: Config | None = None
_shared_store: Any = None
_shared_lock: asyncio.Lock | None = None


async def _one_store_per_instance() -> Any:
    """One connection pool for the dashboard, the API and the app routes together.

    Left to themselves the three sub-applications open a pool each (1+1+1
    held, up to 14), and Supabase's session pooler allows 15 clients in all:
    two warm function instances exhausted it. One pool of at most three per
    instance keeps a handful of instances inside that limit.
    """
    global _shared_store, _shared_lock
    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    async with _shared_lock:
        if _shared_store is None:
            from src.campaigns.store import CampaignStore

            assert config is not None
            _shared_store = await CampaignStore.connect(config.database_url or "", min_size=1, max_size=3)
        return _shared_store


def _build() -> FastAPI:
    """The application, or — when the Vercel variables are wrong — an app that says so.

    A function whose module fails to import answers every request with Vercel's
    opaque FUNCTION_INVOCATION_FAILED, and the reason is only in the logs.
    A configuration problem is answered as a 503 that names it instead.
    """
    global config
    try:
        config = Config.from_env()
        if not config.database_url:
            raise ConfigError(
                "DATABASE_URL is not set (and KB_DATABASE_URL, its fallback, is not either); "
                "the application has nothing to serve. Point it at the hosted PostgreSQL."
            )
        # Only on Vercel (it sets VERCEL=1): locally, `uvicorn api.index:app`
        # against server/.env and its localhost PostgreSQL is the normal run.
        host = (config.database_url.split("@")[-1].split("/")[0].split(":")[0] or "").lower()
        if os.getenv("VERCEL") == "1" and host in ("localhost", "127.0.0.1", "::1"):
            raise ConfigError(
                f"DATABASE_URL points at {host}, which on Vercel is the function itself, not a "
                "database. Set it (or KB_DATABASE_URL) to the hosted PostgreSQL's URL: Supabase's "
                "session pooler (aws-0-<region>.pooler.supabase.com:5432, user postgres.<ref>) or "
                "Neon's pooled URL, with ?sslmode=require."
            )
        # Vercel hosts the dashboard/API only.
        # The persistent campaign/voice worker and outbox delivery
        # remain outside Vercel.
        return create_unified_app(
            config,
            bot_url=os.getenv("APP_BOT_URL") or "http://127.0.0.1:7860",
            store_factory=_one_store_per_instance,
            engine=False,
            deliver=False,
            stream=os.getenv("APP_STREAM_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"),
        )
    except ConfigError as exc:
        problem = str(exc)
        broken = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @broken.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )
        async def unconfigured(path: str) -> JSONResponse:
            return JSONResponse(
                {
                    "error": "the application is not configured",
                    "detail": problem,
                    "where": "Vercel → Settings → Environment Variables (then redeploy)",
                },
                status_code=503,
            )

        return broken


app = _build()
