"""The HTTP hardening every server here shares. Phase 18.

One ASGI middleware, installed on the dashboard, the automation API and the
standalone webhook receiver, doing four things a FastAPI app does not do by
itself:

1. **Security headers** on every response — `nosniff`, `DENY` framing,
   no referrer, a `Cache-Control: no-store` so a shared browser does not
   keep a page of phone numbers, a Content-Security-Policy for the
   dashboard's HTML, and HSTS once HTTPS is required.
2. **HTTPS enforcement**, when `SECURITY_REQUIRE_HTTPS=true`. The scheme is
   read from `X-Forwarded-Proto` *only* when the connection comes from a
   trusted proxy (`SECURITY_TRUSTED_PROXIES`); otherwise a forged header
   would be the whole check. A plain-HTTP request is redirected (a
   dashboard page) or refused with 403 (an API call — a redirect would
   make a client replay a POST over the wire it just used).
3. **A body size cap** (`SECURITY_MAX_BODY_BYTES`), read from
   `Content-Length` before a byte of body is buffered, and enforced again
   while the body streams in for a request that did not say its length.
4. **CORS**, explicitly and only when `SECURITY_CORS_ORIGINS` names an
   origin. No configuration means no `Access-Control-*` headers at all,
   which is the browser's own default: nothing cross-origin. A wildcard
   is refused at configuration time — with credentials it is meaningless
   and without them it publishes the API to every page on the internet.

`client_ip` is the one place the caller's address is decided, and the
same trusted-proxy rule applies: `X-Forwarded-For` is believed one hop
past the last trusted proxy, never further.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Loopback is trusted by default: a tunnel agent (ngrok) and a reverse
#: proxy on the same machine both arrive from it.
DEFAULT_TRUSTED_PROXIES = ("127.0.0.0/8", "::1/128")
DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024
HSTS_MAX_AGE_SECS = 31_536_000

#: The dashboard renders from inline script and style (`page.py`), so those
#: are allowed; everything else — images, fonts, frames, other origins — is
#: not, and `connect-src 'self'` keeps its fetch on its own origin.
DASHBOARD_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)


@dataclass(frozen=True)
class HttpPolicy:
    """What the middleware enforces. Built from `SecurityConfig`; the checks build one by hand."""

    require_https: bool = False
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES
    cors_origins: tuple[str, ...] = ()
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    hsts_max_age_secs: int = HSTS_MAX_AGE_SECS
    #: A Content-Security-Policy of the app's own, sent on every response
    #: whatever the kind. The unified application (Phase 24) serves its
    #: scripts from files and frames the bot's client, which the dashboard's
    #: inline-only policy forbids. None keeps the per-kind default.
    csp: str | None = None

    @property
    def networks(self) -> tuple[IPNetwork, ...]:
        return parse_networks(self.trusted_proxies)


def parse_networks(items: Iterable[str], problems: list[str] | None = None) -> tuple[IPNetwork, ...]:
    """CIDR strings (or bare addresses) as networks; a bad one is a problem, not a crash."""
    found: list[IPNetwork] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            found.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            if problems is not None:
                problems.append(f"SECURITY_TRUSTED_PROXIES: {text!r} is not an address or a CIDR network.")
    return tuple(found)


def is_trusted(address: str | None, networks: Iterable[IPNetwork]) -> bool:
    """Whether an address sits in one of the trusted networks."""
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address in ("testclient", "localhost")
    return any(ip in network for network in networks)


def is_loopback(address: str | None) -> bool:
    """Whether an address is the machine itself (or FastAPI's test client)."""
    if not address:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return address in ("testclient", "localhost")


def client_ip(scope_or_request: Any, networks: Iterable[IPNetwork] = ()) -> str:
    """The caller's address, believing `X-Forwarded-For` only past a trusted proxy.

    Walks the header from the right: every trusted hop is skipped, and the
    first untrusted address is the client. A header set by an untrusted
    client is therefore ignored, because the connection itself is not
    trusted.
    """
    scope = scope_or_request.scope if hasattr(scope_or_request, "scope") else scope_or_request
    client = scope.get("client")
    direct = str(client[0]) if client else ""
    trusted = tuple(networks)
    if not trusted or not is_trusted(direct, trusted):
        return direct or "unknown"
    forwarded = Headers(scope=scope).get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not is_trusted(hop, trusted):
            return hop
    return hops[0] if hops else direct or "unknown"


def effective_scheme(scope_or_request: Any, networks: Iterable[IPNetwork] = ()) -> str:
    """`https` or `http`, believing `X-Forwarded-Proto` only from a trusted proxy."""
    scope = scope_or_request.scope if hasattr(scope_or_request, "scope") else scope_or_request
    client = scope.get("client")
    direct = str(client[0]) if client else ""
    trusted = tuple(networks)
    if trusted and is_trusted(direct, trusted):
        headers = Headers(scope=scope)
        forwarded = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded in ("http", "https"):
            return forwarded
        if forwarded == "" and headers.get("forwarded"):
            for part in headers.get("forwarded", "").replace(",", ";").split(";"):
                name, _, value = part.strip().partition("=")
                if name.lower() == "proto" and value.strip('" ').lower() in ("http", "https"):
                    return value.strip('" ').lower()
    return str(scope.get("scheme", "http")).lower()


def origin_allowed(request: Any, extra_origins: Iterable[str] = ()) -> bool:
    """Whether a state-changing browser request came from this site (or a listed origin).

    The dashboard's login and logout are the only forms this project
    serves; a cross-site POST to either is refused here. `Sec-Fetch-Site`
    is decisive when present; otherwise `Origin`, then `Referer`, must name
    the request's own host. A request with none of the three (a curl) is
    allowed — it is not a browser, and the cookie's `SameSite=Strict`
    already keeps a browser from sending it cross-site.
    """
    headers = request.headers
    site = headers.get("sec-fetch-site", "").lower()
    if site in ("same-origin", "none"):
        return True
    if site in ("cross-site", "same-site"):
        origin = headers.get("origin", "")
        return _origin_matches(origin, headers.get("host", ""), extra_origins)
    origin = headers.get("origin", "") or headers.get("referer", "")
    if not origin:
        return True
    return _origin_matches(origin, headers.get("host", ""), extra_origins)


def _origin_matches(origin: str, host: str, extra: Iterable[str]) -> bool:
    parts = urlsplit(origin)
    if parts.netloc and parts.netloc.lower() == host.lower():
        return True
    wanted = f"{parts.scheme}://{parts.netloc}".lower()
    return wanted in {o.strip().lower().rstrip("/") for o in extra}


class SecurityMiddleware:
    """Headers, HTTPS, body cap. Pure ASGI, so a streaming body is not buffered."""

    def __init__(self, app: ASGIApp, *, policy: HttpPolicy, kind: str = "api") -> None:
        """Wrap an app.

        Args:
            policy: What to enforce.
            kind: `dashboard` (HTML: CSP, redirect to HTTPS), `api` (JSON
                refusals) or `webhook` (plain-text refusals).
        """
        self.app = app
        self.policy = policy
        self.kind = kind
        self._networks = policy.networks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        scheme = effective_scheme(scope, self._networks)

        if self.policy.require_https and scheme != "https":
            await self._refuse_http(scope, send, headers)
            return

        declared = headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length < 0 or length > self.policy.max_body_bytes:
                await self._respond(send, 413, self._body(
                    "body_too_large", f"The request body may be at most {self.policy.max_body_bytes} bytes."
                ), scheme)
                return

        limit = self.policy.max_body_bytes
        received = 0
        exceeded = False

        async def guarded_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit and not exceeded:
                    exceeded = True
                    # Truncate: the handler sees an ended body and will fail
                    # to parse it, rather than buffering the rest.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                self._decorate(response_headers, scheme)
                if exceeded:
                    message["status"] = 413
            await send(message)

        await self.app(scope, guarded_receive, guarded_send)

    def _decorate(self, headers: MutableHeaders, scheme: str) -> None:
        headers.setdefault("x-content-type-options", "nosniff")
        headers.setdefault("x-frame-options", "DENY")
        headers.setdefault("referrer-policy", "no-referrer")
        headers.setdefault("permissions-policy", "camera=(), microphone=(), geolocation=()")
        headers.setdefault("cache-control", "no-store")
        if self.policy.csp:
            headers.setdefault("content-security-policy", self.policy.csp)
        elif self.kind == "dashboard":
            headers.setdefault("content-security-policy", DASHBOARD_CSP)
        if self.policy.require_https and scheme == "https":
            headers.setdefault(
                "strict-transport-security", f"max-age={self.policy.hsts_max_age_secs}; includeSubDomains"
            )

    async def _refuse_http(self, scope: Scope, send: Send, headers: Headers) -> None:
        method = str(scope.get("method", "GET")).upper()
        if self.kind == "dashboard" and method in ("GET", "HEAD"):
            host = headers.get("host", "")
            path = scope.get("path", "/")
            query = scope.get("query_string", b"").decode("latin-1")
            target = f"https://{host}{path}" + (f"?{query}" if query else "")
            await send({
                "type": "http.response.start",
                "status": 308,
                "headers": [(b"location", target.encode("latin-1")), (b"content-length", b"0")],
            })
            await send({"type": "http.response.body", "body": b""})
            return
        await self._respond(send, 403, self._body(
            "https_required",
            "This server accepts HTTPS only (SECURITY_REQUIRE_HTTPS). Put TLS in front of it and, "
            "behind a proxy, list the proxy in SECURITY_TRUSTED_PROXIES so X-Forwarded-Proto is believed.",
        ), "http")

    def _body(self, code: str, message: str) -> tuple[bytes, bytes]:
        if self.kind == "webhook":
            return message.encode("utf-8"), b"text/plain; charset=utf-8"
        import json

        return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8"), b"application/json"

    async def _respond(self, send: Send, status: int, body: tuple[bytes, bytes], scheme: str) -> None:
        content, content_type = body
        raw: list[tuple[bytes, bytes]] = [
            (b"content-type", content_type),
            (b"content-length", str(len(content)).encode("ascii")),
        ]
        headers = MutableHeaders(raw=raw)
        self._decorate(headers, scheme)
        await send({"type": "http.response.start", "status": status, "headers": headers.raw})
        await send({"type": "http.response.body", "body": content})


def install_security(app: Any, policy: HttpPolicy, *, kind: str = "api") -> None:
    """Add CORS (when configured) and the hardening middleware to a FastAPI app.

    Call it before the app serves its first request. CORS is added first
    so that the hardening layer is outermost and every answer — a CORS
    refusal included — carries the security headers.
    """
    if policy.cors_origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(policy.cors_origins),
            allow_credentials=kind == "dashboard",
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "Idempotency-Key"],
            expose_headers=["Idempotency-Key", "Idempotent-Replayed", "Retry-After"],
            max_age=600,
        )
    app.add_middleware(SecurityMiddleware, policy=policy, kind=kind)


def cors_problems(origins: Iterable[str]) -> list[str]:
    """Why a CORS origin list is not acceptable; empty when it is."""
    problems: list[str] = []
    for origin in origins:
        text = origin.strip()
        if not text:
            continue
        if text == "*" or text.startswith("*"):
            problems.append(
                "SECURITY_CORS_ORIGINS must list origins explicitly (https://app.example.com); "
                "a wildcard would publish the API to every page on the internet."
            )
            continue
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/"):
            problems.append(f"SECURITY_CORS_ORIGINS: {text!r} is not an origin (scheme://host[:port]).")
    return problems


__all__ = [
    "DASHBOARD_CSP",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TRUSTED_PROXIES",
    "HttpPolicy",
    "SecurityMiddleware",
    "client_ip",
    "cors_problems",
    "effective_scheme",
    "install_security",
    "is_loopback",
    "is_trusted",
    "origin_allowed",
    "parse_networks",
]
