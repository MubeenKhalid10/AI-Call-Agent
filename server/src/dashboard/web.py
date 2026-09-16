"""The dashboard's HTTP surface: a login, the page, the JSON, the call detail. Phase 10; 18; 20.

FastAPI because it is already installed — Pipecat's dev runner uses it — so the
dashboard adds no dependency at all. It is a *separate* application from the
runner on purpose: the runner's job is to answer calls, and a reporting page
that shared its process could take a live call down with it. They share the
database and nothing else, which is the same seam every other CLI in this
project uses.

**Every reporting route is a read.** The only writes are the login and the
logout, and neither touches a campaign row: the store methods the page
calls are the reporting aggregates and keyed lookups. That is what makes it
defensible to point a browser at a system that is dialling real people: the
worst a bug here can do is show the wrong number.

**It demands a login (Phase 18).** Users from `DASHBOARD_USERS`, a signed
session cookie, login attempts rate-limited per address, every login on the
audit log. An API key from `AUTOMATION_*_API_KEYS` in an `Authorization:
Bearer` header is accepted on the JSON routes too. A **viewer** sees phone
numbers masked and no transcript; an **operator** or **admin** sees the
people and the words, and every transcript they open is an audit row.

**Phase 20: filters, search, and one call.** `/api/dashboard` takes a
campaign and a date range; `/api/calls` lists calls under the same filters
with a search box and paging; `/api/calls/{id}` and `/calls/{id}` are one
call in full. The snapshot cache is keyed by the filter, so twenty viewers
of one campaign cost one read every five seconds, and a search is a bounded
`LIMIT` query that the cache never holds.

**One connection pool for the process, opened at startup.** A pool per request
would be a connection storm on a page that refreshes every fifteen seconds, and
the store is built to be held.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from loguru import logger

from ..campaigns.store import CampaignStore, CampaignStoreError
from ..config import Config, ConfigError
from ..monitoring.collect import GaugeRefresher
from ..monitoring.http import Readiness, install_ops_routes, store_ready
from ..reliability.guardrails import resolve_zone
from ..reliability.observability import event
from ..security import (
    COOKIE_NAME,
    AuditLog,
    AuditUnavailable,
    HttpPolicy,
    Permission,
    Principal,
    RateLimiter,
    Role,
    User,
    UserDirectory,
    client_ip,
    generate_secret,
    install_security,
    issue_session,
    origin_allowed,
    parse_networks,
    parse_role,
    read_session,
    redact_pii,
)
from .page import render_call_page, render_login, render_page
from .stats import MAX_CALLS_PAGE, ReportFilter, call_detail, collect, list_calls, search_people

#: Where the page fetches its data. One constant, used by the route and by the
#: page, so they cannot disagree.
API_PATH = "/api/dashboard"
CALLS_API_PATH = "/api/calls"
CAMPAIGNS_API_PATH = "/api/campaigns"
SEARCH_API_PATH = "/api/search"
CALL_PAGE_PATH = "/calls"
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"


def user_from_row(row: Any) -> User:
    """A directory `User` from a `dashboard_users` row. Phase 27."""
    return User(
        name=row.name, role=parse_role(row.role), password_hash=row.password_hash, email=row.email, status=getattr(row, "status", "active")
    )


async def load_registered_user(directory: UserDirectory, store: Any, name_or_email: str) -> User | None:
    """Bring a sign-up from the `dashboard_users` table into the directory, as the table has it now. Phase 27.

    Called by the login before it authenticates. A `DASHBOARD_USERS` entry is
    returned as is. A registered user is re-read every time, so an approval
    (or a rejection) decided on another process — or before this one started
    — is what the login sees. A missing table, or an unreachable database, is
    logged and treated as "no such user": the login page then says "wrong
    name or password", which is also what it says for a stranger.
    """
    known = directory.get(name_or_email)
    if known is not None and known.email is None:
        return known
    try:
        row = await store.get_dashboard_user(name_or_email)
    except CampaignStoreError as exc:
        logger.warning(event("dashboard.registered_user_lookup_failed", error=str(exc).splitlines()[0] if str(exc) else type(exc).__name__))
        return known
    if row is None:
        if known is not None:
            directory.remove(known.name)
        return None
    user = user_from_row(row)
    if not directory.update(user):
        directory.add(user)
    return directory.get(name_or_email)
ME_PATH = "/api/me"

#: How often the page re-fetches. Long enough not to hammer the database from a
#: tab somebody left open, short enough that a call placed now shows up before
#: it has ended.
REFRESH_SECS = 15

#: How long a snapshot is served again without re-reading. Phase 11.
#:
#: The dashboard's queries are full-table aggregates — measured at 249 ms over
#: 60,000 attempts — and every open tab was running all of them every 15
#: seconds. Five viewers meant five times that load on the same database the
#: dialer is using. A cache shorter than the refresh interval means a single
#: viewer still sees fresh numbers every time, while N viewers cost the same as
#: one. Deliberately well under `REFRESH_SECS`, so nobody is ever shown a
#: figure older than they expect.
CACHE_TTL_SECS = 5.0
#: How many distinct filter views the cache holds. Phase 20: a campaign and a
#: date range is one entry; the oldest goes when the bound is reached.
CACHE_MAX_VIEWS = 32

#: The most a login form field may carry. A user name is short and a
#: password is bounded by `passwords.MAX_PASSWORD_LENGTH`; anything larger is
#: not a login.
MAX_FIELD_CHARS = 1024
#: The longest search the routes accept.
MAX_SEARCH_CHARS = 100
#: The widest date range a view may ask for. A year of calls is a report,
#: not a dashboard; the CLI and the API page through history.
MAX_RANGE_DAYS = 400

StoreFactory = Callable[[], Awaitable[Any]]


class _SnapshotCache:
    """Serves one snapshot per filter view to every viewer for a few seconds. Phase 11; 20.

    Two problems, one lock per view:

    * **N viewers cost the same as one.** Each open tab refreshes every 15
      seconds, and each refresh was a full pass of aggregate queries over the
      same database the dialer is using.
    * **A slow read is not run twice.** The lock means that when a request
      arrives while another is already reading, the second waits for the first
      rather than starting its own — the "cache stampede" that turns a slow
      query into several slow queries at exactly the moment it can least afford
      them.

    In-process and deliberately so, like Phase 9's pacing limiter: one dashboard
    process is the shape of this stage, and a shared cache would need
    infrastructure the project has none of. Phase 20 keys the cache by the
    filter (a campaign and a date range) and bounds the number of views held,
    so a browser cycling through campaigns cannot grow it without limit.
    """

    def __init__(self, config: Config, ttl_secs: float = CACHE_TTL_SECS, max_views: int = CACHE_MAX_VIEWS) -> None:
        """Create the cache."""
        self._config = config
        self._ttl = ttl_secs
        self._max = max_views
        self._views: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
        self._locks: dict[tuple[Any, ...], asyncio.Lock] = {}

    async def get(self, store: Any, filters: ReportFilter | None = None) -> dict[str, Any]:
        """The current snapshot for a view, reading it again only when the copy is stale."""
        filters = filters or ReportFilter()
        key = filters.key()
        fresh = self._fresh(key)
        if fresh is not None:
            return fresh
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Checked again inside the lock: whoever was ahead in the queue has
            # just refreshed it, and reading a second time would be the
            # stampede this exists to prevent.
            fresh = self._fresh(key)
            if fresh is not None:
                return fresh
            started = time.monotonic()
            if filters.active:
                snapshot = await collect(store, timezone=self._config.calendar.timezone, filters=filters)
            else:
                snapshot = await collect(store, timezone=self._config.calendar.timezone)
            payload = snapshot.to_dict()
            payload["read_ms"] = int((time.monotonic() - started) * 1000)
            payload["cache_ttl_secs"] = self._ttl
            self._views[key] = (time.monotonic(), payload)
            self._views.move_to_end(key)
            while len(self._views) > self._max:
                oldest, _ = self._views.popitem(last=False)
                self._locks.pop(oldest, None)
            return payload

    def _fresh(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        held = self._views.get(key)
        if held is None:
            return None
        read_at, payload = held
        if (time.monotonic() - read_at) < self._ttl:
            return payload
        return None


class FilterError(ValueError):
    """A filter the request asked for cannot be read."""


def parse_date(raw: str | None, zone: Any, *, end: bool = False) -> datetime | None:
    """A `YYYY-MM-DD` day (in the campaign zone) or an ISO 8601 moment, or None.

    A bare day is the start of that day; with `end`, the start of the *next*
    day, so a `to` of a day includes the whole day. `Z` is accepted; a naive
    moment is read in the campaign zone.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if len(text) > 40:
        raise FilterError("a date is at most 40 characters")
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=zone)
            return day + timedelta(days=1) if end else day
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FilterError(f"{text!r} is not a date (YYYY-MM-DD) or an ISO 8601 moment") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def create_app(
    config: Config,
    *,
    store_factory: StoreFactory | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Build the dashboard application.

    Args:
        config: Supplies the database URL, the campaign timezone, the users,
            the session secret and the hardening settings.
        store_factory: Where the store comes from. `CampaignStore.connect`
            by default; the checks hand in a fake.
        clock: Where "now" comes from for sessions; the checks inject one.

    Raises:
        CampaignStoreError: There is no `DATABASE_URL`. Raised at build time
            rather than on the first request, so the CLI can say so and exit
            instead of serving a page that cannot work.
        ConfigError: A login is required (the default) and `DASHBOARD_USERS`
            names nobody, so nobody could ever sign in.
    """
    if not config.database_url and store_factory is None:
        raise CampaignStoreError(
            "No database is configured, so there is nothing to report on.\n"
            "  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to)."
        )
    security = config.security
    if security.dashboard_auth_required and not security.users:
        raise ConfigError(
            "The dashboard needs a login and DASHBOARD_USERS names nobody, so nobody could sign in.\n"
            "  Make a hash with `uv run security.py hash-password`, then set\n"
            "  DASHBOARD_USERS=alice:admin:<hash>  (roles: admin, operator, viewer).\n"
            "  For a local, loopback-only dashboard with no login, set DASHBOARD_AUTH_DISABLED=true."
        )

    now = clock or time.time
    secret = security.session_secret or generate_secret()
    networks = parse_networks(security.trusted_proxies)
    login_limiter = RateLimiter(security.login_rate_limit, 60.0)
    anon_limiter = RateLimiter(security.anon_rate_limit, 60.0)
    state: dict[str, Any] = {"store": None}
    cache = _SnapshotCache(config)
    timezone = config.calendar.timezone
    zone = resolve_zone(timezone)

    def store_for_audit() -> Any:
        store = state["store"]
        if store is None:
            raise CampaignStoreError("the dashboard is still starting")
        return store

    audit = AuditLog(store_for_audit, enabled=security.audit_enabled, strict=security.audit_strict)
    monitoring = config.monitoring
    refresher = (
        GaugeRefresher(
            store_for_audit,
            interval_secs=monitoring.refresh_secs,
            stale_secs=config.worker.stale_secs,
            max_attempts=config.campaign_max_attempts,
            window_secs=monitoring.throughput_window_secs,
        )
        if monitoring.enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Hold one connection pool for the life of the process."""
        # Phase 11: `collect` issues eight reads at once, so a pool of four
        # would queue half of them and give back much of what the concurrency
        # bought. Eight is the width of one page load; the cache keeps the
        # number of page loads down.
        if store_factory is not None:
            store = await store_factory()
        else:
            store = await CampaignStore.connect(config.database_url or "", min_size=1, max_size=8)
        state["store"] = store
        logger.info(event("dashboard.ready", outcome=f"reading {timezone} times; {security.describe()}"))
        if security.dashboard_auth_required and not security.session_secret:
            logger.warning(
                "dashboard.session_secret_generated | sessions will not survive a restart and a second "
                "dashboard process will not share them; set DASHBOARD_SESSION_SECRET (`uv run security.py make-secret`)"
            )
        # Phase 22: the fleet gauges, refreshed from the rows on a timer, so
        # this process's /metrics answers for the deployment.
        if refresher is not None:
            refresher.start()
        try:
            yield
        finally:
            if refresher is not None:
                await refresher.stop()
            state["store"] = None
            await store.close()

    app = FastAPI(
        title="Ai-Voice-Agent dashboard",
        description="Read-only reporting over the campaign database, behind a login.",
        version="20",
        lifespan=lifespan,
        # No interactive docs: they are a write-shaped affordance on a read-only
        # tool, and the JSON endpoint is the whole API.
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
            max_body_bytes=min(security.max_body_bytes, 64 * 1024),
        ),
        kind="dashboard",
    )
    # Phase 22: /healthz, /readyz, /metrics; a request id on every request
    # and a count per route template. Unauthenticated by design — a probe
    # cannot log in — and carrying nothing about a person; MONITORING_TOKEN
    # puts a bearer in front of the numbers.
    if monitoring.enabled:

        async def readiness() -> Readiness:
            return Readiness([await store_ready(store_or_503)])

        install_ops_routes(
            app, "dashboard", readiness=readiness, token=monitoring.token, version="22"
        )

    # --- Who is asking -----------------------------------------------------------------

    def ip_of(request: Request) -> str:
        return client_ip(request, networks)

    def bearer_of(request: Request) -> str | None:
        auth = request.headers.get("authorization", "").strip()
        if auth:
            scheme, _, token = auth.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
        key = request.headers.get("x-api-key", "").strip()
        return key or None

    def principal_of(request: Request) -> Principal | None:
        """The session's user, an API key's role, or nobody."""
        if security.dashboard_auth_disabled:
            return Principal(name="anonymous", role=Role.OPERATOR, via="anonymous")
        session, _reason = read_session(secret, request.cookies.get(COOKIE_NAME), now=now())
        if session is not None:
            return session.principal()
        match = config.automation.role_for_key(bearer_of(request))
        if match is not None:
            role, label = match
            return Principal(name=label, role=role, via="api_key")
        return None

    def unauthenticated(request: Request) -> JSONResponse:
        decision = anon_limiter.check(ip_of(request))
        if not decision.allowed:
            return JSONResponse(
                {"error": "too many unauthenticated requests", "retry_after_secs": int(decision.retry_after_header)},
                status_code=429,
                headers={"Retry-After": decision.retry_after_header},
            )
        return JSONResponse({"error": "sign in first", "login": LOGIN_PATH}, status_code=401)

    def safe_next(raw: str | None) -> str:
        """A path on this site to return to after login; never another host."""
        if not raw:
            return "/"
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or not raw.startswith("/") or raw.startswith("//"):
            return "/"
        return raw[:200]

    def set_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=int(security.session_ttl_secs),
            httponly=True,
            samesite="strict",
            secure=security.require_https,
            path="/",
        )

    def clear_cookie(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=security.require_https)

    def store_or_503() -> Any:
        store = state["store"]
        if store is None:
            raise CampaignStoreError("the dashboard is still starting")
        return store

    def shaped(principal: Principal, payload: dict[str, Any]) -> JSONResponse:
        """The answer, masked for a principal who may not see the people."""
        if not principal.can(Permission.READ_PII):
            payload = redact_pii(payload)
            payload["masked"] = True
        return JSONResponse(payload)

    async def filters_of(store: Any, campaign: str | None, since: str | None, until: str | None) -> ReportFilter:
        """The view a request asked for, validated. Phase 20."""
        campaign_id: int | None = None
        campaign_name: str | None = None
        text = (campaign or "").strip()
        if text:
            if len(text) > 200:
                raise FilterError("a campaign reference is at most 200 characters")
            found = await store.get_campaign(int(text)) if text.isdigit() else None
            if found is None:
                found = await store.find_campaign_by_name(text)
            if found is None:
                raise FilterError(f"no campaign called {text!r}")
            campaign_id, campaign_name = found.id, found.name
        start = parse_date(since, zone)
        end = parse_date(until, zone, end=True)
        if start and end and end <= start:
            raise FilterError("`to` must be after `from`")
        if start and end and (end - start) > timedelta(days=MAX_RANGE_DAYS):
            raise FilterError(f"a range is at most {MAX_RANGE_DAYS} days; page through history with the CLI or the API")
        return ReportFilter(campaign_id=campaign_id, campaign_name=campaign_name, since=start, until=end)

    def bad_filter(exc: FilterError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(AuditUnavailable)
    async def _audit_unavailable(request: Request, exc: AuditUnavailable) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(CampaignStoreError)
    async def _store_error(request: Request, exc: CampaignStoreError) -> JSONResponse:
        logger.warning(event("dashboard.unavailable", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
        return JSONResponse({"error": (str(exc).splitlines() or [type(exc).__name__])[0]}, status_code=503)

    # --- Login and logout ----------------------------------------------------------------

    @app.get(LOGIN_PATH, response_class=HTMLResponse)
    async def login_form(request: Request, next: str | None = None) -> Response:
        """The login page. Already signed in: straight to the dashboard."""
        if security.dashboard_auth_disabled or principal_of(request) is not None:
            return RedirectResponse(safe_next(next), status_code=303)
        return HTMLResponse(render_login(error=None, next_path=safe_next(next)))

    @app.post(LOGIN_PATH, response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        """Check a name and password; issue the session cookie.

        Wrong is wrong, whatever was wrong: the page says "name or password"
        so a guess does not learn which names exist, and the directory takes
        the same time either way. Attempts are limited per address, and every
        one — success or failure — is on the audit log.
        """
        if security.dashboard_auth_disabled:
            return RedirectResponse("/", status_code=303)
        ip = ip_of(request)
        if not origin_allowed(request, security.cors_origins):
            await audit.record("auth.login_refused", ip=ip, outcome="cross-site form post")
            return HTMLResponse(render_login(error="That request did not come from this site.", next_path="/"), status_code=403)
        decision = login_limiter.check(ip)
        if not decision.allowed:
            await audit.record("auth.login_rate_limited", ip=ip)
            return HTMLResponse(
                render_login(error=f"Too many attempts. Try again in {decision.retry_after_header} seconds.", next_path="/"),
                status_code=429,
                headers={"Retry-After": decision.retry_after_header},
            )
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001 - not a form is the whole finding
            return HTMLResponse(render_login(error="Expected a login form.", next_path="/"), status_code=400)
        username = str(form.get("username", ""))[:MAX_FIELD_CHARS].strip()
        password = str(form.get("password", ""))[:MAX_FIELD_CHARS]
        next_path = safe_next(str(form.get("next", "/")))
        # Phase 27: a name the environment does not know may be a sign-up.
        if username and state["store"] is not None:
            await load_registered_user(security.users, state["store"], username)
        user = security.users.authenticate(username, password)
        if user is None:
            # The name is recorded only when it exists: an attacker's guesses
            # would otherwise fill the audit log with whatever they typed.
            known = security.users.get(username) is not None
            await audit.record("auth.login_failed", ip=ip, outcome="unknown user" if not known else "wrong password", user=username if known else None)
            logger.warning(event("dashboard.login_failed", ip=ip, outcome="unknown user" if not known else "wrong password"))
            return HTMLResponse(render_login(error="Wrong name or password.", next_path=next_path, username=username[:64]), status_code=401)
        if not user.active:
            # Phase 27: an operator / admin request an admin has not approved.
            # Said only once the password matched, so a guess learns nothing.
            await audit.record("auth.login_pending", principal=user.principal(), ip=ip, outcome=f"{user.role.value} request awaiting approval")
            logger.info(event("dashboard.login_pending", outcome=f"{user.name} ({user.role.value})", ip=ip))
            return HTMLResponse(
                render_login(error="Your account is awaiting an administrator's approval.", next_path=next_path, username=username[:64]),
                status_code=403,
                headers={"X-Aiva-Login": "pending"},
            )
        login_limiter.reset(ip)
        token = issue_session(secret, user.name, user.role, ttl_secs=security.session_ttl_secs, now=now())
        await audit.record("auth.login", principal=user.principal(), ip=ip)
        logger.info(event("dashboard.login", outcome=f"{user.name} ({user.role.value})", ip=ip))
        response = RedirectResponse(next_path, status_code=303)
        set_cookie(response, token)
        return response

    @app.post(LOGOUT_PATH)
    async def logout(request: Request) -> Response:
        """End the session: the cookie is cleared, and the token it held expires on its own."""
        principal = principal_of(request)
        if not origin_allowed(request, security.cors_origins):
            return JSONResponse({"error": "that request did not come from this site"}, status_code=403)
        if principal is not None and principal.via == "session":
            await audit.record("auth.logout", principal=principal, ip=ip_of(request))
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        clear_cookie(response)
        return response

    # --- The page and its data -------------------------------------------------------------

    def page_for(request: Request, principal: Principal, *, path: str) -> HTMLResponse:
        return HTMLResponse(
            render_page(
                api_path=API_PATH,
                refresh_secs=REFRESH_SECS,
                user=principal.name,
                role=principal.role.value,
                logout_path=LOGOUT_PATH if principal.via == "session" else None,
                masked=not principal.can(Permission.READ_PII),
                calls_api_path=CALLS_API_PATH,
                campaigns_api_path=CAMPAIGNS_API_PATH,
                search_api_path=SEARCH_API_PATH,
                call_page_path=CALL_PAGE_PATH,
            )
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        """The dashboard page. Static — every number arrives from `API_PATH`."""
        principal = principal_of(request)
        if principal is None:
            return RedirectResponse(f"{LOGIN_PATH}?next={quote('/')}", status_code=303)
        return page_for(request, principal, path="/")

    @app.get(API_PATH)
    async def dashboard(
        request: Request,
        campaign: str | None = Query(default=None, max_length=200),
        from_: str | None = Query(default=None, alias="from", max_length=40),
        to: str | None = Query(default=None, max_length=40),
    ) -> JSONResponse:
        """Every number the page shows, as JSON, under the view's filters.

        The reusable half: anything that wants these totals — a status script, a
        future CRM sync, a second page — reads this rather than the database.
        A session cookie or an API key is needed; a viewer gets the phone
        numbers masked.

        A database that has gone away is reported as a 503 with a message,
        because the page renders that into the timestamp and keeps its last
        numbers on screen. A dashboard that blanks itself when the database
        blinks is one nobody trusts.
        """
        principal = principal_of(request)
        if principal is None:
            return unauthenticated(request)
        store = store_or_503()
        try:
            filters = await filters_of(store, campaign, from_, to)
        except FilterError as exc:
            return bad_filter(exc)
        payload = dict(await cache.get(store, filters))
        return shaped(principal, payload)

    @app.get(CAMPAIGNS_API_PATH)
    async def campaigns(request: Request) -> JSONResponse:
        """The campaigns, for the filter. Id, name, status; nothing personal."""
        principal = principal_of(request)
        if principal is None:
            return unauthenticated(request)
        rows = await store_or_503().list_campaigns(limit=200)
        return JSONResponse({"campaigns": [{"id": c.id, "name": c.name, "status": c.status.value} for c in rows]})

    @app.get(CALLS_API_PATH)
    async def calls(
        request: Request,
        campaign: str | None = Query(default=None, max_length=200),
        from_: str | None = Query(default=None, alias="from", max_length=40),
        to: str | None = Query(default=None, max_length=40),
        q: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
        status: str | None = Query(default=None, max_length=40),
        prospect_id: int | None = Query(default=None, ge=1),
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=25, ge=1, le=MAX_CALLS_PAGE),
    ) -> JSONResponse:
        """The calls under the filters, newest first, searchable and paged. Phase 20.

        `q` matches the person's name, company and email and the carrier's
        call id for everybody; the digits of a phone number only for a
        principal with `read_pii` — a search is a way to learn whether a
        number is on file, which is the fact a viewer may not have.
        """
        principal = principal_of(request)
        if principal is None:
            return unauthenticated(request)
        store = store_or_503()
        try:
            filters = await filters_of(store, campaign, from_, to)
        except FilterError as exc:
            return bad_filter(exc)
        wanted = status.strip().upper() if status and status.strip() else None
        if wanted is not None and (len(wanted) > 40 or not wanted.replace("_", "").isalpha()):
            return JSONResponse({"error": "status must be an attempt status such as COMPLETED"}, status_code=422)
        payload = await list_calls(
            store,
            timezone=timezone,
            filters=filters,
            search=q,
            search_phone=principal.can(Permission.READ_PII),
            status=wanted,
            before_id=before_id,
            prospect_id=prospect_id,
            limit=limit,
        )
        return shaped(principal, payload)

    @app.get(SEARCH_API_PATH)
    async def search(request: Request, q: str = Query(min_length=1, max_length=MAX_SEARCH_CHARS)) -> JSONResponse:
        """People and calls matching a search. Phase 20."""
        principal = principal_of(request)
        if principal is None:
            return unauthenticated(request)
        store = store_or_503()
        pii = principal.can(Permission.READ_PII)
        people = await search_people(store, q, limit=10, search_phone=pii)
        found = await list_calls(store, timezone=timezone, search=q, search_phone=pii, limit=10)
        payload: dict[str, Any] = {"query": q, "prospects": people, "calls": found["calls"], "phone_searched": pii and any(ch.isdigit() for ch in q)}
        if q.strip().isdigit():
            attempt = await store.get_attempt(int(q.strip()))
            payload["attempt"] = {"attempt_id": attempt.id, "status": attempt.status.value} if attempt else None
        return shaped(principal, payload)

    @app.get(f"{CALLS_API_PATH}/{{attempt_id}}")
    async def call(request: Request, attempt_id: int = Path(ge=1)) -> JSONResponse:
        """One call in full. The transcript, the conversation record and the report need `read_pii`."""
        principal = principal_of(request)
        if principal is None:
            return unauthenticated(request)
        store = store_or_503()
        pii = principal.can(Permission.READ_PII)
        detail = await call_detail(store, attempt_id, timezone=timezone, include_transcript=pii)
        if detail is None:
            return JSONResponse({"error": f"no call attempt {attempt_id}"}, status_code=404)
        if pii and detail.get("transcript_included"):
            await audit.record("pii.transcript_read", principal=principal, ip=ip_of(request), target=("call", attempt_id), via_page="dashboard")
        detail["viewer"] = {"role": principal.role.value, "can_read_pii": pii}
        return shaped(principal, detail)

    @app.get(f"{CALL_PAGE_PATH}/{{attempt_id}}", response_class=HTMLResponse)
    async def call_page(request: Request, attempt_id: int = Path(ge=1)) -> Response:
        """The call detail page. Static — everything arrives from the JSON route."""
        principal = principal_of(request)
        if principal is None:
            return RedirectResponse(f"{LOGIN_PATH}?next={quote(f'{CALL_PAGE_PATH}/{attempt_id}')}", status_code=303)
        return HTMLResponse(
            render_call_page(
                attempt_id=attempt_id,
                api_path=f"{CALLS_API_PATH}/{attempt_id}",
                user=principal.name,
                role=principal.role.value,
                logout_path=LOGOUT_PATH if principal.via == "session" else None,
                masked=not principal.can(Permission.READ_PII),
            )
        )

    @app.get(ME_PATH)
    async def me(request: Request) -> JSONResponse:
        """Who the caller is, for a script that wants to know which key it holds."""
        principal = principal_of(request)
        if principal is None:
            return unauthenticated(request)
        return JSONResponse(
            {
                "name": principal.name,
                "role": principal.role.value,
                "via": principal.via,
                "permissions": sorted(p.value for p in principal.permissions),
            }
        )

    @app.get("/api/ping")
    async def ping(request: Request) -> JSONResponse:
        """Is the dashboard up and can it reach the database?

        Deliberately not the vendor health check: that one costs seven network
        round trips and belongs to `uv run health.py`. This answers the one
        question a process monitor asks, and says nothing else.
        """
        decision = anon_limiter.check(ip_of(request))
        if not decision.allowed:
            return JSONResponse({"ok": False, "detail": "too many requests"}, status_code=429, headers={"Retry-After": decision.retry_after_header})
        store = state["store"]
        if store is None:
            return JSONResponse({"ok": False, "detail": "starting"})
        try:
            await store.count_prospects()
        except CampaignStoreError as exc:
            return JSONResponse({"ok": False, "detail": (str(exc).splitlines() or [type(exc).__name__])[0]})
        return JSONResponse({"ok": True, "detail": "database reachable"})

    return app


__all__ = [
    "API_PATH",
    "CALLS_API_PATH",
    "CALL_PAGE_PATH",
    "CAMPAIGNS_API_PATH",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "ME_PATH",
    "REFRESH_SECS",
    "SEARCH_API_PATH",
    "FilterError",
    "create_app",
    "load_registered_user",
    "parse_date",
    "user_from_row",
]
