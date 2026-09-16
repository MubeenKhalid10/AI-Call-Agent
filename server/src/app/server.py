"""The unified application server: the SPA, the two existing apps, and the routes they lacked. Phase 24.

**Composition, not reimplementation.** The dashboard app (`src/dashboard`)
and the automation API (`src/automation`) are built by their own factories,
unchanged, and mounted under `/dashboard` and `/automation`. The browser
application calls them directly — reads from the first, writes to the
second — with one session cookie, which the API learned to accept in this
phase. The routes defined here are only the ones neither app had:

| Route | What |
|---|---|
| `GET /` → `/app/` | the application (a single page; its router is client-side) |
| `GET /static/…` | its script and stylesheet, from `web/` |
| `GET /api/app/session` | who the caller is, or 401 with where to sign in |
| `GET /api/app/config` | the configuration a person may see: providers, carrier, calendar, CRM, automation, security posture, monitoring, the sales defaults — never a key |
| `POST /api/app/health` | `health.py`'s checks, on demand (operators and admins; slow) |
| `GET /api/app/knowledge` | the knowledge base's documents and counts |
| `POST /api/app/knowledge/documents` | upload a `.txt`, `.md` or `.pdf`: extract, chunk, embed, store — `ingest.py add`'s path, over HTTP |
| `DELETE /api/app/knowledge/documents/{source}` | remove one |
| `POST /api/app/knowledge/search` | the retriever's search, for trying the knowledge base out |

**Lifespans.** Starlette does not run a mounted application's lifespan, so
this app's lifespan enters both sub-applications' lifespan contexts by
hand — that is what opens their pools and starts the API's deliverer — and
closes them in reverse.

**One secret.** The dashboard makes a random session secret when none is
configured; here the same secret is handed to every part, or the cookie the
dashboard issued would not be readable by the API.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import mimetypes
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from ..automation import ApiSettings, create_automation_app
from ..automation.auth import extract_key
from ..campaigns import CampaignStatus, CampaignStore, CampaignStoreError
from ..campaigns.progress import campaign_progress
from ..campaigns.store import DuplicateUserError
from ..config import Config
from ..dashboard import LOGIN_PATH, create_app
from ..dashboard.web import user_from_row
from ..documents import SUPPORTED_SUFFIXES, DocumentError, chunk, extract
from ..monitoring.http import Readiness, ReadyCheck, install_ops_routes, store_ready
from ..reliability import check_health
from ..reliability.observability import event, redact
from ..security import (
    COOKIE_NAME,
    MAX_EMAIL_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    NAME_RULE,
    USER_ACTIVE,
    USER_PENDING,
    AuditLog,
    HttpPolicy,
    Permission,
    Principal,
    RateLimiter,
    Role,
    client_ip,
    generate_secret,
    hash_password,
    install_security,
    is_loopback,
    origin_allowed,
    parse_networks,
    parse_role,
    read_session,
    validate_registration,
)
from .engine import FAILED as ENGINE_FAILED
from .engine import IDLE as ENGINE_IDLE
from .engine import OFF as ENGINE_OFF
from .engine import CampaignEngine, ProviderFactory
from .proxy import install_bot_proxy

# Phase 25: how often the event stream re-reads the rows, how often it says
# it is alive when nothing changed, and how long one stream may stay open.
STREAM_POLL_SECS = 2.0
STREAM_HEARTBEAT_SECS = 15.0
STREAM_MAX_SECS = 3600.0

APP_PATH = "/app"
STATIC_PATH = "/static"
DASHBOARD_MOUNT = "/dashboard"
AUTOMATION_MOUNT = "/automation"
DEFAULT_APP_PORT = 7900  # Not 7860 (the bot), 7870 (the dashboard), 7880, 7890, 7895.
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

StoreFactory = Callable[[], Awaitable[Any]]


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class RegisterIn(BaseModel):
    """The Register page's form. Phase 27. The bounds here only cap the body; the rules are `validate_registration`'s."""

    name: str = Field(default="", max_length=256)
    email: str = Field(default="", max_length=MAX_EMAIL_LENGTH + 64)
    password: str = Field(default="", max_length=MAX_PASSWORD_LENGTH + 64)
    confirm_password: str = Field(default="", max_length=MAX_PASSWORD_LENGTH + 64)
    #: The role asked for. Never granted by itself: viewer is active at once;
    #: operator and admin are pending until an admin approves the request.
    role: str = Field(default="viewer", max_length=32)


def app_csp(bot_url: str, *, proxied: bool = False) -> str:
    """The application's own policy: scripts and styles from files, the bot's client in a frame.

    Behind the application (`proxied`, Phase 34) the bot's client is on this
    origin, so the frame source is `'self'`.
    """
    origin = "'self'" if proxied else bot_url.rstrip("/")
    return (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; form-action 'self'; "
        f"frame-src {origin}; frame-ancestors 'none'; base-uri 'none'"
    )


def create_unified_app(
    config: Config,
    *,
    store_factory: StoreFactory | None = None,
    bot_url: str = "http://127.0.0.1:7860",
    web_dir: Path | None = None,
    deliver: bool = True,
    knowledge_factory: Callable[[], Awaitable[Any]] | None = None,
    embedder_factory: Callable[[], Any] | None = None,
    engine: bool | None = None,
    provider_factory: ProviderFactory | None = None,
    worker_id: str | None = None,
    proxy_bot: bool = False,
    stream: bool = True,
) -> FastAPI:
    """Build the application.

    Phase 25: the campaign execution engine — the scheduler as a task inside
    this process — starts with the application and dials every `ACTIVE`
    campaign. `engine=False` (or `WORKER_EMBEDDED=false`) leaves that to
    `campaign.py run`; `provider_factory` is how the checks and the audit
    give it a carrier that never rings a phone.

    Args:
        config: The deployment's configuration. Its session secret is used
            by every part; a missing one is generated once here.
        store_factory: Where the sub-applications and the knowledge routes'
            audit writer get their store. `CampaignStore.connect` by default;
            the checks hand in a fake.
        bot_url: Where the bot's runner serves its browser client, for the
            Live Agent page's frame and the policy that allows it.
        proxy_bot: Phase 34 — forward every path this application does not
            own (`/client`, `/api/offer`, `/ws`, `POST /`) to `bot_url`, so one
            public hostname serves the application and the bot.
        stream: Phase 35 — serve `/api/app/stream` (server-sent events). Off
            where a long-lived response is unwelcome (a serverless function):
            the route answers 204 and the page polls instead.
        web_dir: Where `index.html`, `app.js` and `styles.css` live.
        deliver: Run the outbox deliverer inside the API, as `automation.py` does.
        knowledge_factory / embedder_factory: The checks inject a fake
            knowledge store and embedder; production opens the real ones lazily.

    Raises:
        ConfigError / CampaignStoreError: What the sub-applications raise —
            no API key, no database, no dashboard user.
    """
    security = config.security
    # The route below is itself named `stream`; keep the switch under its own name.
    stream_enabled = stream
    secret = security.session_secret or generate_secret()
    if not security.session_secret:
        logger.warning(
            "app.session_secret_generated | sessions will not survive a restart; set DASHBOARD_SESSION_SECRET (`uv run security.py make-secret`)"
        )
    config = dataclasses.replace(config, security=dataclasses.replace(security, session_secret=secret))
    security = config.security
    networks = parse_networks(security.trusted_proxies)
    web = web_dir or WEB_DIR

    dashboard = create_app(config, store_factory=store_factory)
    api = create_automation_app(ApiSettings.from_config(config), store_factory=store_factory, deliver=deliver)

    state: dict[str, Any] = {"store": None, "knowledge": None, "embedder": None}

    def store_or_503() -> Any:
        store = state["store"]
        if store is None:
            raise CampaignStoreError("the application is still starting")
        return store

    audit = AuditLog(store_or_503, enabled=security.audit_enabled, strict=security.audit_strict)

    # Phase 25: the engine's own store — its own pool, so the worker's
    # transactions never queue behind a page's reads.
    async def engine_store() -> Any:
        if store_factory is not None:
            return await store_factory()
        return await CampaignStore.connect(config.database_url, min_size=1, max_size=2)

    campaign_engine = CampaignEngine(
        config,
        store_factory=engine_store,
        provider_factory=provider_factory,
        enabled=engine if engine is not None else (config.worker.embedded and bool(config.database_url or store_factory)),
        worker_id=worker_id,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The sub-applications' lifespans: Starlette runs only the root's.
        async with dashboard.router.lifespan_context(dashboard), api.router.lifespan_context(api):
            if store_factory is not None:
                state["store"] = await store_factory()
            elif config.database_url:
                state["store"] = await CampaignStore.connect(config.database_url, min_size=1, max_size=2)
            logger.info(event("app.ready", outcome=f"{APP_PATH}/ with {DASHBOARD_MOUNT} and {AUTOMATION_MOUNT}; bot at {bot_url}" + (" (forwarded from this origin)" if proxy_bot else "")))
            await campaign_engine.start()
            try:
                yield
            finally:
                # The engine first: no new call from here on, the calls in
                # progress get their bounded wait, then the rest is closed.
                await campaign_engine.stop()
                knowledge = state.get("knowledge")
                if knowledge is not None:
                    await knowledge.close()
                store = state.get("store")
                proxy = state.get("proxy")
                state.update(store=None, knowledge=None, embedder=None)
                if store is not None and store_factory is None:
                    await store.close()
                if proxy is not None:
                    await proxy.close()

    app = FastAPI(
        title="Ai-Voice-Agent",
        description="The unified application: one login, every page, over the existing dashboard and automation API.",
        version="24",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_security(
        app,
        HttpPolicy(
            require_https=security.require_https,
            trusted_proxies=security.trusted_proxies,
            cors_origins=security.cors_origins,
            max_body_bytes=max(security.max_body_bytes, MAX_UPLOAD_BYTES),
            csp=app_csp(bot_url, proxied=proxy_bot),
        ),
        kind="dashboard",
    )
    if config.monitoring.enabled:

        async def readiness() -> Readiness:
            checks = [await store_ready(store_or_503)] if (config.database_url or store_factory) else []
            # Phase 25: a failed engine is not ready; off and idle are what
            # was asked for (another process dials, or nothing can).
            status = campaign_engine.status()
            checks.append(
                ReadyCheck(
                    "engine",
                    status["state"] != ENGINE_FAILED,
                    f"{status['state']}"
                    + (f": {status['reason']}" if status.get("reason") else "")
                    + (f"; {len(status['in_flight'])} call(s) in progress" if status["in_flight"] else ""),
                )
            )
            return Readiness(checks)

        install_ops_routes(
            app,
            "app",
            readiness=readiness,
            token=config.monitoring.token,
            version="25",
            info=lambda: {"engine": campaign_engine.state, "in_flight": len(campaign_engine.status()["in_flight"])},
            stopping=lambda: campaign_engine.state == "stopping",
        )

    # --- Who is asking -----------------------------------------------------------------

    def principal_of(request: Request) -> Principal | None:
        """The session cookie, an API key, or loopback with the login off — the dashboard's rule."""
        if security.dashboard_auth_disabled and is_loopback(client_ip(request, networks)):
            return Principal(name="anonymous", role=Role.OPERATOR, via="anonymous")
        session, _reason = read_session(secret, request.cookies.get(COOKIE_NAME), now=time.time())
        if session is not None:
            return session.principal()
        match = config.automation.role_for_key(extract_key(request.headers))
        if match is not None:
            role, label = match
            return Principal(name=label, role=role, via="api_key")
        return None

    def require(request: Request, permission: Permission | None = None) -> Principal:
        principal = principal_of(request)
        if principal is None:
            raise _Refused(401, {"error": "sign in first", "login": f"{DASHBOARD_MOUNT}{LOGIN_PATH}"})
        if permission is not None and not principal.can(permission):
            raise _Refused(403, {"error": f"this needs the {permission.value} permission; you hold the {principal.role.value} role"})
        return principal

    @app.exception_handler(_Refused)
    async def _refused(request: Request, exc: _Refused) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    @app.exception_handler(CampaignStoreError)
    async def _store_error(request: Request, exc: CampaignStoreError) -> JSONResponse:
        return JSONResponse({"error": (str(exc).splitlines() or [type(exc).__name__])[0]}, status_code=503)

    # --- The application itself ----------------------------------------------------------

    index = web / "index.html"
    # The Live Agent page frames the bot's client and the frame needs the
    # microphone; the hardening default (`microphone=()`) would refuse it, so
    # the page delegates the microphone to that one origin — this one when the
    # bot is behind the application (Phase 34), the bot's otherwise.
    page_headers = {
        "Cache-Control": "no-store",
        "Permissions-Policy": f"camera=(), microphone=({'self' if proxy_bot else chr(34) + bot_url.rstrip('/') + chr(34)}), geolocation=()",
    }

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(f"{APP_PATH}/", status_code=302)

    @app.get(APP_PATH, include_in_schema=False)
    @app.get(APP_PATH + "/", include_in_schema=False)
    @app.get(APP_PATH + "/{path:path}", include_in_schema=False)
    async def spa(path: str = "") -> Any:
        """The one page; its router reads the hash. Every path serves it."""
        if not index.is_file():
            return JSONResponse({"error": f"the application's files are missing: {index}"}, status_code=500)
        return FileResponse(index, media_type="text/html; charset=utf-8", headers=page_headers)

    if web.is_dir():
        app.mount(STATIC_PATH, StaticFiles(directory=str(web)), name="static")

    @app.get("/api/app/session")
    async def session(request: Request) -> JSONResponse:
        principal = require(request)
        return JSONResponse(
            {
                "name": principal.name,
                "role": principal.role.value,
                "via": principal.via,
                "permissions": sorted(p.value for p in principal.permissions),
                "login": f"{DASHBOARD_MOUNT}{LOGIN_PATH}",
                "logout": f"{DASHBOARD_MOUNT}/logout",
            }
        )

    # --- Registration (Phase 27) -----------------------------------------------------------
    # The Register page: a sign-up becomes a `dashboard_users` row (the hash
    # from `src/security/passwords.py`, the name rule from `DASHBOARD_USERS`)
    # and joins the same directory the dashboard's login reads, so signing in
    # afterwards is Phase 18's login, unchanged. Unauthenticated by nature;
    # same-site only, and limited per address like the login.

    register_limiter = RateLimiter(security.login_rate_limit, 60.0)

    def registration_view() -> dict[str, Any]:
        return {
            "enabled": bool(security.registration_enabled and not security.dashboard_auth_disabled),
            "roles": [r.value for r in Role],
            "approval_roles": [Role.OPERATOR.value, Role.ADMIN.value],
            "login": f"{DASHBOARD_MOUNT}{LOGIN_PATH}",
            "rules": {"name": NAME_RULE, "password_min_length": MIN_PASSWORD_LENGTH, "password_max_length": MAX_PASSWORD_LENGTH},
        }

    @app.get("/api/app/register")
    async def registration(request: Request) -> JSONResponse:
        """Whether the Register page is open, and the rules it should show. Public: the page is shown before a login."""
        return JSONResponse(registration_view())

    @app.post("/api/app/register")
    async def register(request: Request, body: RegisterIn) -> JSONResponse:
        """Create an account from the Register page.

        422 with a `fields` map for anything the form got wrong, 409 naming
        the field for a name or email already taken, 403 when the page is
        closed or the request is not from this site, 429 when an address
        keeps trying. 201 with the new account's name, email, role and
        status. The role the client sends decides nothing by itself: a
        viewer is `active`; an operator or admin request is `pending` and
        cannot sign in until an admin approves it (`/api/app/users`).
        """
        ip = client_ip(request, networks)
        if not origin_allowed(request, security.cors_origins):
            await audit.record("auth.register_refused", ip=ip, outcome="cross-site post")
            raise _Refused(403, {"error": "that request did not come from this site", "code": "csrf"})
        if security.dashboard_auth_disabled:
            raise _Refused(403, {"error": "the login is switched off (DASHBOARD_AUTH_DISABLED), so there is nothing to register for"})
        if not security.registration_enabled:
            raise _Refused(403, {"error": "sign-up is closed on this deployment; ask an administrator for an account"})
        decision = register_limiter.check(ip)
        if not decision.allowed:
            await audit.record("auth.register_rate_limited", ip=ip)
            return JSONResponse(
                {"error": f"too many attempts; try again in {decision.retry_after_header} seconds"},
                status_code=429,
                headers={"Retry-After": decision.retry_after_header},
            )
        name = body.name.strip()
        email = body.email.strip()
        problems = validate_registration(name, email, body.password, body.confirm_password)
        try:
            role = parse_role(body.role or Role.VIEWER.value)
        except ValueError:
            role = Role.VIEWER
            problems["role"] = "Choose viewer, operator or admin."
        if problems:
            raise _Refused(422, {"error": "some of the details are not valid", "fields": problems})
        status = USER_ACTIVE if role is Role.VIEWER else USER_PENDING
        # The environment's users are not in the table, so the directory is
        # asked first: a sign-up may not take a configured name or email.
        if security.users.get(name) is not None:
            raise _Refused(409, {"error": "a user with that name already exists", "field": "name"})
        if security.users.get(email) is not None or security.users.has_email(email):
            raise _Refused(409, {"error": "a user with that email already exists", "field": "email"})
        store = store_or_503()
        try:
            row = await store.add_dashboard_user(
                name=name, email=email, role=role.value, password_hash=hash_password(body.password), status=status
            )
        except DuplicateUserError as exc:
            await audit.record("auth.register_refused", ip=ip, outcome=f"duplicate {exc.field}")
            raise _Refused(409, {"error": str(exc), "field": exc.field}) from exc
        user = user_from_row(row)
        security.users.add(user)
        outcome = "ok" if user.active else f"{user.role.value} request awaiting approval"
        await audit.record("auth.registered", principal=user.principal(), ip=ip, target=("user", row.id), outcome=outcome)
        logger.info(event("app.registered", outcome=f"{user.name} ({user.role.value}, {row.status})", ip=ip))
        return JSONResponse(
            {"name": user.name, "email": row.email, "role": user.role.value, "status": row.status, "login": f"{DASHBOARD_MOUNT}{LOGIN_PATH}"},
            status_code=201,
        )

    # --- Sign-up approval (Phase 27) -------------------------------------------------------
    # An admin (the `manage` permission — the existing role) sees the pending
    # operator / admin requests and approves or rejects each. Approval makes
    # the row active as the role it asked for and nothing else; rejection
    # deletes it, so the name and email are free again. Both on the audit log.

    def approver(request: Request) -> Principal:
        principal = require(request, Permission.MANAGE)
        if not origin_allowed(request, security.cors_origins):
            raise _Refused(403, {"error": "that request did not come from this site", "code": "csrf"})
        return principal

    @app.get("/api/app/users/pending")
    async def pending_users(request: Request) -> JSONResponse:
        """The sign-ups awaiting a decision. Admins only."""
        require(request, Permission.MANAGE)
        rows = await store_or_503().list_dashboard_users(status=USER_PENDING)
        return JSONResponse({"users": [row.public() for row in rows]})

    async def decide(request: Request, user_id: int, *, approve: bool) -> JSONResponse:
        principal = approver(request)
        row = await store_or_503().decide_dashboard_user(user_id, approve=approve, decided_by=principal.name)
        if row is None:
            raise _Refused(404, {"error": "no pending sign-up with that id; it may have been decided already"})
        if approve:
            user = user_from_row(row)
            if not security.users.update(user):
                security.users.add(user)
        else:
            security.users.remove(row.name)
        action = "auth.signup_approved" if approve else "auth.signup_rejected"
        await audit.record(action, principal=principal, ip=client_ip(request, networks), target=("user", row.id), user=row.name, role=row.role)
        logger.info(event("app." + ("signup_approved" if approve else "signup_rejected"), outcome=f"{row.name} ({row.role}) by {principal.name}"))
        return JSONResponse({"user": row.public(), "decision": "approved" if approve else "rejected"})

    @app.post("/api/app/users/{user_id}/approve")
    async def approve_user(request: Request, user_id: int) -> JSONResponse:
        """Approve a pending sign-up as the role it asked for. Admins only."""
        return await decide(request, user_id, approve=True)

    @app.post("/api/app/users/{user_id}/reject")
    async def reject_user(request: Request, user_id: int) -> JSONResponse:
        """Reject a pending sign-up: the row is deleted. Admins only."""
        return await decide(request, user_id, approve=False)

    @app.get("/api/app/config")
    async def configuration(request: Request) -> JSONResponse:
        """The configuration as a person may see it. No value that is a secret; the scrubber runs over the rest."""
        require(request)
        telephony = config.telephony
        sales = config.sales
        body = {
            "providers": {
                "stt": f"{config.stt_provider}:{config.stt_model}",
                "llm": f"{config.llm_provider}:{config.llm_model}",
                "tts": config.tts_provider,
                "embedding": config.embedding_model,
            },
            "telephony": {
                "provider": telephony.provider,
                "from_number": telephony.from_number,
                "public_url": telephony.public_url,
                "configured": telephony.is_configured,
                "has_credentials": telephony.has_credentials,
                "webhooks": telephony.describe_webhooks(),
                "transfer_number_set": bool(telephony.transfer_number),
                "machine_detection": telephony.machine_detection,
            },
            "calling": {
                "hours": config.reliability.calling_hours if hasattr(config.reliability, "calling_hours") else None,
                "describe": config.reliability.describe(),
                "max_attempts": config.campaign_max_attempts,
                "retry_minutes": config.campaign_retry_minutes,
                "default_phone_region": config.default_phone_region,
                "worker": config.worker.describe(),
            },
            "sales": {
                "enabled": sales.enabled,
                "agent_name": sales.agent_name,
                "company_name": sales.company_name,
                "company_description": sales.company_description,
                "services": list(sales.services),
                "offer": sales.offer,
                "value_points": list(sales.value_points),
                "qualification_criteria": list(sales.qualification_criteria),
                "meeting_ask": sales.meeting_ask,
                "notes": list(sales.notes),
            },
            "calendar": config.calendar.describe(),
            "crm": config.crm.describe(),
            "automation": {
                "api_keys": config.automation.key_count if hasattr(config.automation, "key_count") else None,
                "delivery": config.automation.delivery_enabled,
                "targets": sorted(config.automation.targets) if config.automation.delivery_enabled else [],
                "signed": bool(config.automation.webhook_secret),
            },
            "security": config.security.describe(),
            "compliance": config.compliance.describe(),
            "monitoring": config.monitoring.describe(),
            "knowledge_base": config.kb_enabled,
            "bot_url": bot_url,
            "bot_proxied": proxy_bot,
            "database": bool(config.database_url),
        }
        return JSONResponse(_scrub(body))

    @app.post("/api/app/health")
    async def health(request: Request) -> JSONResponse:
        """Every dependency, probed cheaply — `health.py`, on demand."""
        require(request, Permission.WRITE)
        report = await check_health(config, timeout_secs=config.reliability.health_timeout_secs)
        return JSONResponse(report.to_dict())

    # --- The knowledge base ------------------------------------------------------------------

    async def knowledge_store() -> Any:
        if not config.kb_enabled:
            raise _Refused(503, {"error": "the knowledge base is off (KB_ENABLED=false)"})
        if state["knowledge"] is None:
            if knowledge_factory is not None:
                state["knowledge"] = await knowledge_factory()
            else:
                from ..knowledge_store import KnowledgeStore, KnowledgeStoreError

                embedder = embedder_of()
                try:
                    state["knowledge"] = await KnowledgeStore.connect(
                        config.kb_database_url or "", dimensions=embedder.dimensions, embed_model=embedder.model_name
                    )
                except KnowledgeStoreError as exc:
                    raise _Refused(503, {"error": redact((str(exc).splitlines() or [type(exc).__name__])[0])}) from exc
        return state["knowledge"]

    def embedder_of() -> Any:
        if state["embedder"] is None:
            if embedder_factory is not None:
                state["embedder"] = embedder_factory()
            else:
                from ..embeddings import shared_embedder

                state["embedder"] = shared_embedder(config.embedding_model)
        return state["embedder"]

    @app.get("/api/app/knowledge")
    async def knowledge(request: Request) -> JSONResponse:
        require(request)
        store = await knowledge_store()
        documents = await store.list_documents()
        total_documents, total_chunks = await store.counts()
        return JSONResponse(
            {
                "enabled": True,
                "documents": [dataclasses.asdict(d) if dataclasses.is_dataclass(d) else dict(d) for d in documents],
                "counts": {"documents": total_documents, "chunks": total_chunks},
                "supported": sorted(SUPPORTED_SUFFIXES),
                "embedding_model": config.embedding_model,
            }
        )

    @app.post("/api/app/knowledge/documents")
    async def upload_document(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        """`ingest.py add`, over HTTP: extract, chunk, embed, store; replaces a document of the same name."""
        principal = require(request, Permission.WRITE)
        store = await knowledge_store()
        name = Path(file.filename or "").name
        suffix = Path(name).suffix.lower()
        if not name or suffix not in SUPPORTED_SUFFIXES:
            raise _Refused(422, {"error": f"unsupported file; send one of {', '.join(sorted(SUPPORTED_SUFFIXES))}"})
        raw = await file.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise _Refused(413, {"error": f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"})
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / name
            path.write_bytes(raw)
            try:
                document = await asyncio.to_thread(extract, path)
            except DocumentError as exc:
                raise _Refused(422, {"error": (str(exc).splitlines() or [type(exc).__name__])[0]}) from exc
        pieces = chunk(document.text, target_words=config.kb_chunk_words, overlap_words=config.kb_chunk_overlap_words)
        if not pieces:
            raise _Refused(422, {"error": f"{name} produced no text to index"})
        embedder = embedder_of()
        vectors = await asyncio.to_thread(embedder.embed_documents, [piece.content for piece in pieces])
        stored = await store.add_document(
            source=document.source,
            title=document.title,
            content_hash=document.content_hash,
            byte_size=document.byte_size,
            chunks=[piece.content for piece in pieces],
            vectors=vectors,
        )
        await audit.record("knowledge.document_added", principal=principal, ip=client_ip(request, networks), target=("document", document.source), chunks=len(pieces))
        logger.info(event("knowledge.document_added", outcome=document.source, chunks=len(pieces)))
        return JSONResponse({"source": document.source, "title": document.title, "chunks": len(pieces), "bytes": document.byte_size, "stored": stored}, status_code=201)

    @app.delete("/api/app/knowledge/documents/{source:path}")
    async def delete_document(request: Request, source: str) -> JSONResponse:
        principal = require(request, Permission.WRITE)
        store = await knowledge_store()
        removed = await store.delete_document(source)
        if not removed:
            raise _Refused(404, {"error": f"no document called {source!r}"})
        await audit.record("knowledge.document_removed", principal=principal, ip=client_ip(request, networks), target=("document", source))
        return JSONResponse({"removed": source})

    @app.post("/api/app/knowledge/search")
    async def search(request: Request, body: SearchIn) -> JSONResponse:
        """What the agent would be handed for this question."""
        require(request)
        store = await knowledge_store()
        embedder = embedder_of()
        vector = await embedder.embed_query_async(body.query)
        matches = await store.search(vector, limit=body.top_k, min_score=config.kb_min_score)
        return JSONResponse(
            {
                "query": body.query,
                "min_score": config.kb_min_score,
                "matches": [
                    {"source": m.source, "title": getattr(m, "title", None), "score": round(float(m.score), 3), "content": m.content}
                    for m in matches
                ],
            }
        )

    # --- The campaign execution engine (Phase 25) ------------------------------------------

    @app.get("/api/app/engine")
    async def engine_status(request: Request) -> JSONResponse:
        """The scheduler inside this process: its state, its worker, the calls it is following."""
        require(request)
        return JSONResponse(campaign_engine.status())

    async def campaign_or_404(campaign_id: int) -> Any:
        campaign = await store_or_503().get_campaign(campaign_id)
        if campaign is None:
            raise _Refused(404, {"error": f"no campaign {campaign_id}"})
        return campaign

    @app.get("/api/app/campaigns/{campaign_id}/progress")
    async def progress(request: Request, campaign_id: int) -> JSONResponse:
        """Every counter of one campaign, read from the rows now."""
        require(request)
        campaign = await campaign_or_404(campaign_id)
        return JSONResponse(await campaign_progress(store_or_503(), campaign))

    async def watched_campaigns(campaign_id: int | None) -> list[Any]:
        store = store_or_503()
        if campaign_id is not None:
            found = await store.get_campaign(campaign_id)
            return [found] if found is not None else []
        active = await store.list_campaigns(status=CampaignStatus.ACTIVE, limit=50)
        paused = await store.list_campaigns(status=CampaignStatus.PAUSED, limit=50)
        return [*active, *paused]

    @app.get("/api/app/stream")
    async def stream(
        request: Request,
        campaign: int | None = None,
        max_secs: float = STREAM_MAX_SECS,
        poll_secs: float = STREAM_POLL_SECS,
    ) -> StreamingResponse:
        """Server-sent events: the engine and campaign progress, as the rows change.

        One campaign (`?campaign=ID`) or every running and paused one. The
        rows are re-read every `poll_secs`; an event is sent only when
        something changed, a comment keeps the connection alive otherwise,
        and the stream ends after `max_secs` (the page reconnects). Nothing is
        pushed from the worker: the scheduler and the bot write PostgreSQL,
        and this reads it, so the figures are the same whichever process
        placed the call — which is the whole reason there is no WebSocket.

        Events: `hello` (once), `engine` (the engine's status), `campaign`
        (a campaign's progress; its final figures once more when it finishes),
        `error` (the store did not answer; the stream keeps trying).
        """
        require(request)
        if not stream_enabled:
            # 204 ends an EventSource for good (no reconnect); the page then polls.
            return Response(status_code=204)
        max_secs = max(1.0, min(float(max_secs), STREAM_MAX_SECS))
        poll = max(0.2, min(float(poll_secs), 60.0))

        def frame(name: str, data: Any) -> str:
            return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"

        async def events() -> AsyncIterator[str]:
            sent: dict[str, str] = {}
            watched: set[int] = set()
            started = last_sent = time.monotonic()
            yield frame("hello", {"campaign": campaign, "poll_secs": poll, "max_secs": max_secs})
            while time.monotonic() - started < max_secs:
                if await request.is_disconnected():
                    return
                try:
                    status = campaign_engine.status()
                    key = json.dumps({k: v for k, v in status.items() if k != "metrics"}, sort_keys=True, default=str)
                    if sent.get("engine") != key:
                        sent["engine"] = key
                        yield frame("engine", status)
                        last_sent = time.monotonic()
                    store = store_or_503()
                    current = await watched_campaigns(campaign)
                    ids = {c.id for c in current}
                    # A campaign that just finished drops out of the watched
                    # list; its final figures are sent once so the page
                    # shows the end, not the last tick before it.
                    for gone in sorted(watched - ids):
                        finished = await store.get_campaign(gone)
                        if finished is not None:
                            current.append(finished)
                    watched = ids
                    for found in current:
                        report = await campaign_progress(store, found)
                        key = json.dumps({k: v for k, v in report.items() if k != "updated_at"}, sort_keys=True)
                        if sent.get(f"campaign:{found.id}") != key:
                            sent[f"campaign:{found.id}"] = key
                            yield frame("campaign", report)
                            last_sent = time.monotonic()
                except CampaignStoreError as exc:
                    yield frame("error", {"error": (str(exc).splitlines() or [type(exc).__name__])[0]})
                    last_sent = time.monotonic()
                if time.monotonic() - last_sent >= STREAM_HEARTBEAT_SECS:
                    yield ": ping\n\n"
                    last_sent = time.monotonic()
                await asyncio.sleep(poll)
            yield frame("bye", {"reason": "max_secs reached; reconnect"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.state.engine = campaign_engine
    app.mount(DASHBOARD_MOUNT, dashboard, name="dashboard")
    app.mount(AUTOMATION_MOUNT, api, name="automation")
    if proxy_bot:
        # Last, so every route and mount above is matched first.
        state["proxy"] = install_bot_proxy(app, bot_url)
    return app


class _Refused(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("error", "refused"))
        self.status = status
        self.body = body


def _scrub(value: Any) -> Any:
    """Run the log scrubber over every string in a JSON-shaped value."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


mimetypes.add_type("application/javascript", ".js")

__all__ = ["APP_PATH", "DEFAULT_APP_PORT", "STATIC_PATH", "app_csp", "create_unified_app"]
