"""The operational routes every server here answers, and a server for the one that had none. Phase 22.

Three routes, the same on the bot's runner, the dashboard, the automation
API, the webhook receiver and the scheduler:

* `GET /healthz` — **liveness.** Answers 200 the moment the process can run
  a request handler, with its role, pid, uptime and version. It proves the
  process is alive and nothing else: a deployment restarts on this, so it
  must never fail for a dependency's sake.
* `GET /readyz` — **readiness.** 200 when the process could do its job now
  — the database answers, the process is not shutting down — and 503 when
  it could not, with one line per check. A load balancer or a rollout
  waits on this. Details are scrubbed of credentials before they leave.
* `GET /metrics` — the process's registry in the Prometheus text format;
  `GET /metrics.json` the same as data with exact percentiles. Behind
  `MONITORING_TOKEN` (a bearer) when one is set, because the numbers name
  campaign ids and error rates; open on a loopback deployment.

Two pieces of middleware, for the servers that take requests from outside:

* `RequestIdMiddleware` — gives every request an id (`X-Aiva-Request-Id`,
  honoured when the client sent a well-formed one, echoed back either way)
  and binds it on the request's log lines as `request`.
* `RequestMetricsMiddleware` — counts and times requests by **route
  template** (`/api/v1/calls/{attempt_id}`), never by raw path, so a phone
  number in a URL can never become a metric label.

And `serve_ops` — a small uvicorn server for `campaign.py run`, which has no
web server of its own. It binds the socket itself first, so a second worker
on the same machine finding the port taken logs one line and keeps
dialling; a scheduler must never fail to start because a scrape endpoint
could not.

Nothing here imports the campaign tables or a pipeline. A readiness probe
is a callable the entry point hands in; the module knows how to run and
render one, not what it checks.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..reliability.observability import event, redact
from .instruments import HTTP_LATENCY, HTTP_REQUESTS, process_started, process_stopping
from .metrics import REGISTRY, MetricsRegistry
from .tracing import REQUEST_HEADER, clean_id, new_request_id

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
METRICS_PATH = "/metrics"
METRICS_JSON_PATH = "/metrics.json"
OPS_PATHS = (HEALTH_PATH, READY_PATH, METRICS_PATH, METRICS_JSON_PATH)

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The default port for the scheduler's own server. Not 7860 (the bot),
#: 7870 (the dashboard), 7880 (the webhooks) or 7890 (the automation API).
DEFAULT_OPS_PORT = 7895


@dataclass(frozen=True)
class ReadyCheck:
    """One readiness question and its answer."""

    name: str
    ok: bool
    detail: str = ""
    latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": redact(self.detail), "latency_ms": self.latency_ms}


@dataclass
class Readiness:
    """Every check's answer, and the verdict."""

    checks: list[ReadyCheck] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "checks": [check.to_dict() for check in self.checks]}


ReadinessProbe = Callable[[], Awaitable[Readiness]]
InfoSource = Callable[[], dict[str, Any]]


async def run_check(
    name: str, probe: Callable[[], Awaitable[str | None]], *, timeout_secs: float = 3.0
) -> ReadyCheck:
    """Run one probe under a timeout, turning any failure into a not-ok answer with a scrubbed reason.

    The probe returns a short description on success (`"database answers"`)
    or raises. A check must never raise past here: a readiness route that
    throws is a 500 a load balancer reads as "unknown", not as "not ready".
    """
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_secs):
            detail = await probe()
    except TimeoutError:
        return ReadyCheck(name, False, f"no answer within {timeout_secs:g}s", _ms(started))
    except Exception as exc:  # noqa: BLE001 - the answer is "not ready", whatever the reason
        return ReadyCheck(name, False, redact(f"{exc.__class__.__name__}: {(str(exc).splitlines() or [type(exc).__name__])[0] if str(exc) else ''}".strip()), _ms(started))
    return ReadyCheck(name, True, redact(detail or "ok"), _ms(started))


async def store_ready(store_getter: Callable[[], Any], *, timeout_secs: float = 3.0) -> ReadyCheck:
    """The database check every server shares: the store's cheapest round trip.

    `store_getter` returns the process's store or raises when there is none
    yet (a server still starting). A store with a `ping()` is asked that;
    one without — a check's double — is asked to count prospects, which is
    what `/api/ping` has always done.
    """

    async def probe() -> str:
        store = store_getter()
        ping = getattr(store, "ping", None)
        if ping is not None:
            await ping()
            return "database answers"
        await store.count_prospects()
        return "database answers"

    return await run_check("database", probe, timeout_secs=timeout_secs)


def always_ready(detail: str = "nothing to wait for") -> ReadinessProbe:
    """A probe for a process with no dependency to wait on."""

    async def probe() -> Readiness:
        return Readiness([ReadyCheck("process", True, detail)])

    return probe


# --- The routes ---------------------------------------------------------------------


def create_ops_router(
    role: str,
    *,
    readiness: ReadinessProbe | None = None,
    registry: MetricsRegistry = REGISTRY,
    token: str | None = None,
    version: str | None = None,
    started_at: float | None = None,
    info: InfoSource | None = None,
    stopping: Callable[[], bool] | None = None,
) -> APIRouter:
    """The three routes for one process.

    Args:
        role: What the process is: `bot`, `scheduler`, `dashboard`, `api`,
            `webhooks`. On every answer, and the `role` label on the HTTP
            metrics.
        readiness: What `/readyz` asks. `None` means the process is ready
            once it answers.
        registry: The metrics to serve. The process's, by default.
        token: A bearer that `/metrics` and `/metrics.json` demand when set.
        version: A version string for `/healthz`; the phase number, here.
        started_at: When the process started (`time.time()`); now, if omitted.
        info: Extra fields for `/healthz` — sessions active, calls followed,
            the worker's id. Cheap and synchronous, or leave it out.
        stopping: Whether the process is shutting down; `/readyz` says 503
            the moment it is, so a balancer stops sending work before the
            last request is refused.
    """
    started = started_at if started_at is not None else time.time()
    probe = readiness or always_ready()
    process_started(role, started)
    router = APIRouter(tags=["ops"])

    def authorised(request: Request) -> bool:
        if not token:
            return True
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip(), token)

    def refused() -> JSONResponse:
        return JSONResponse(
            {"error": "MONITORING_TOKEN is set; send it as a bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @router.get(HEALTH_PATH, include_in_schema=False)
    async def healthz() -> JSONResponse:
        """Liveness: the process is running. Never asks anything else."""
        payload: dict[str, Any] = {
            "ok": True,
            "role": role,
            "pid": os.getpid(),
            "uptime_secs": round(time.time() - started, 1),
            "version": version,
            "stopping": bool(stopping()) if stopping is not None else False,
        }
        if info is not None:
            try:
                payload.update(info())
            except Exception as exc:  # noqa: BLE001 - liveness must not fail for a detail
                payload["info_error"] = redact(f"{exc.__class__.__name__}: {exc}")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get(READY_PATH, include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Readiness: the process could do its job right now."""
        if stopping is not None and stopping():
            report = Readiness([ReadyCheck("process", False, "shutting down")])
        else:
            try:
                report = await probe()
            except Exception as exc:  # noqa: BLE001 - not ready, and say why
                report = Readiness([ReadyCheck("readiness", False, redact(f"{exc.__class__.__name__}: {exc}"))])
        payload = {"role": role, **report.to_dict()}
        return JSONResponse(payload, status_code=200 if report.ready else 503, headers={"Cache-Control": "no-store"})

    @router.get(METRICS_PATH, include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """The registry, for Prometheus."""
        if not authorised(request):
            return refused()
        return PlainTextResponse(registry.render_prometheus(), media_type=PROMETHEUS_CONTENT_TYPE, headers={"Cache-Control": "no-store"})

    @router.get(METRICS_JSON_PATH, include_in_schema=False)
    async def metrics_json(request: Request) -> Response:
        """The registry as data, with exact percentiles."""
        if not authorised(request):
            return refused()
        return JSONResponse(
            {"role": role, "generated_at": time.time(), "metrics": registry.snapshot()},
            headers={"Cache-Control": "no-store"},
        )

    return router


def install_ops_routes(app: Any, role: str, *, request_metrics: bool = True, request_ids: bool = True, **kwargs: Any) -> None:
    """Mount the three routes on an app, and the two middlewares unless told not to.

    The bot's runner passes `request_metrics=False, request_ids=False`: its
    app carries a websocket the middlewares would sit in front of, and a
    phone call's audio is not a request to count.
    """
    app.include_router(create_ops_router(role, **kwargs))
    if request_ids:
        app.add_middleware(RequestIdMiddleware)
    if request_metrics:
        app.add_middleware(RequestMetricsMiddleware, role=role)


def create_ops_app(role: str, **kwargs: Any) -> FastAPI:
    """A bare app with only the three routes — the scheduler's server."""
    app = FastAPI(title=f"Ai-Voice-Agent {role} ops", docs_url=None, redoc_url=None, openapi_url=None)
    install_ops_routes(app, role, **kwargs)
    return app


# --- Middleware -------------------------------------------------------------------------


class RequestIdMiddleware:
    """A request id on every request: honoured from the client when well-formed, made up otherwise, always echoed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        presented = None
        for name, value in scope.get("headers", ()):
            if name == REQUEST_HEADER.lower().encode("latin-1"):
                presented = clean_id(value.decode("latin-1", "replace"))
                break
        request_id = presented or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_HEADER.lower().encode("latin-1"), request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with logger.contextualize(request=request_id):
            await self.app(scope, receive, send_with_id)


class RequestMetricsMiddleware:
    """Counts and times requests by route template, never by raw path."""

    def __init__(self, app: ASGIApp, role: str) -> None:
        self.app = app
        self.role = role

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": 0}

        async def send_watching(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_watching)
        except Exception:
            status["code"] = status["code"] or 500
            raise
        finally:
            # FastAPI writes the matched `APIRoute` onto the scope, so the
            # template is known here without a second match. No route — a
            # 404, a redirect from the security layer — is `unmatched`.
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = str(scope.get("method", "GET"))
            HTTP_REQUESTS.inc(role=self.role, method=method, route=template, status=str(status["code"] or 0))
            HTTP_LATENCY.observe(time.monotonic() - started, role=self.role, route=template)


# --- The scheduler's server ------------------------------------------------------------


@dataclass
class OpsServer:
    """A running `serve_ops` server: where it listens and how to stop it."""

    host: str
    port: int
    task: asyncio.Task[Any]
    server: Any
    role: str

    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{shown}:{self.port}"

    async def stop(self, timeout_secs: float = 5.0) -> None:
        """Ask uvicorn to exit and wait briefly for it."""
        process_stopping(self.role)
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, timeout=timeout_secs)
        except (TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        except Exception as exc:  # noqa: BLE001 - shutting down; say so and carry on
            logger.warning(event("ops.server_stop_failed", error=str(exc)))


async def serve_ops(app: Any, *, host: str, port: int, role: str) -> OpsServer | None:
    """Serve `app` in the background on this loop, or return None if the port is taken.

    The socket is bound here, not by uvicorn: uvicorn's own bind failure
    ends the process, and a scheduler that cannot open a scrape port must
    still dial. A taken port is a warning with the fix in it.
    """
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # On Windows `SO_REUSEADDR` lets a second process bind a port that
        # is already listening — the opposite of what a "port taken" check
        # needs — so the exclusive flag is used there instead.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        logger.warning(
            event(
                "ops.port_unavailable",
                error=f"{host}:{port}: {exc.strerror or exc}",
                outcome="no /metrics for this process; set MONITORING_PORT to a free port or 0 to disable",
            )
        )
        return None
    bound_port = sock.getsockname()[1]
    config = uvicorn.Config(app, host=host, port=bound_port, log_level="warning", lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name=f"ops-server-{role}")
    ops = OpsServer(host=host, port=bound_port, task=task, server=server, role=role)
    logger.info(event("ops.serving", outcome=f"{ops.url}{HEALTH_PATH} {READY_PATH} {METRICS_PATH}"))
    return ops


def _ms(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))


__all__ = [
    "DEFAULT_OPS_PORT",
    "HEALTH_PATH",
    "METRICS_JSON_PATH",
    "METRICS_PATH",
    "OPS_PATHS",
    "PROMETHEUS_CONTENT_TYPE",
    "READY_PATH",
    "OpsServer",
    "ReadyCheck",
    "Readiness",
    "RequestIdMiddleware",
    "RequestMetricsMiddleware",
    "always_ready",
    "create_ops_app",
    "create_ops_router",
    "install_ops_routes",
    "run_check",
    "serve_ops",
    "store_ready",
]
