"""The bot's web server, reached through the application. Phase 34.

One tunnel gives one public hostname, and a free tunnel's hostname can point
at one local port. The carrier needs the bot's ``POST /`` and ``/ws`` on that
hostname; the Live Agent page needs the bot's ``/client`` and ``/api/offer``;
the operator needs the application. Until now the tunnel went to the bot and
the application stayed on the laptop.

With ``APP_PROXY_BOT=true`` (``app.py --proxy-bot``) the application takes
the hostname and forwards every path it does not own to the bot's runner:
HTTP requests are streamed through (the body, the status, the headers), and
WebSockets are relayed frame for frame, text and binary alike — the carrier's
audio stream included. The application's own routes and mounts are matched
first, so nothing here can shadow them; the catch-alls are registered last.

The forwarded answer is allowed to be framed by this origin and to use the
microphone, because framing the bot's client is the point; the hardening
middleware's defaults (``DENY``, ``microphone=()``) would otherwise be
applied to it, since they are set only where a header is absent.

Nothing on the call path imports this module; it is the application's.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.typing import Subprotocol

from ..reliability.observability import event

#: Headers that describe one hop, never forwarded in either direction.
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
)
#: Handshake headers the WebSocket client library writes itself.
WS_HANDSHAKE = frozenset({"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"})
#: The methods a forwarded path answers. ``GET /`` stays the application's (it opens the page); ``HEAD`` rides with it.
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")
ROOT_METHODS = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")

#: What a forwarded answer carries so the Live Agent page may frame it and it may hear.
FRAMED_CSP = "frame-ancestors 'self'"
FRAMED_PERMISSIONS = "camera=(self), microphone=(self), geolocation=()"


class BotProxy:
    """Forwards HTTP and WebSocket traffic to the bot's runner at ``bot_url``."""

    def __init__(self, bot_url: str) -> None:
        self.bot_url = bot_url.rstrip("/")
        parts = urlsplit(self.bot_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"APP_BOT_URL must be http(s)://host[:port], not {bot_url!r}")
        self.ws_url = ("wss" if parts.scheme == "https" else "ws") + "://" + parts.netloc + parts.path.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    # --- lifecycle ---------------------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.bot_url,
                # The bot answers its HTTP quickly; a slow read is a stuck runner, not a long download.
                timeout=httpx.Timeout(10.0, read=120.0, write=60.0, pool=10.0),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # --- HTTP --------------------------------------------------------------------------

    def _forward_headers(self, headers: Any, client_host: str | None, scheme: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP or lower == "host" or lower == "content-length":
                continue
            out[lower] = value
        # Say who asked, without overwriting what a tunnel or proxy already said.
        out.setdefault("x-forwarded-host", headers.get("host", ""))
        out.setdefault("x-forwarded-proto", scheme)
        if client_host:
            hops = out.get("x-forwarded-for", "")
            out["x-forwarded-for"] = f"{hops}, {client_host}" if hops else client_host
        return out

    async def http(self, request: Request) -> Response:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        body = await request.body()
        headers = self._forward_headers(request.headers, request.client.host if request.client else None, request.url.scheme)
        if body or request.method in ("POST", "PUT", "PATCH"):
            headers["content-length"] = str(len(body))
        client = self.client()
        try:
            upstream = await client.send(client.build_request(request.method, target, headers=headers, content=body), stream=True)
        except httpx.HTTPError as exc:
            logger.warning(event("app.bot_proxy.unreachable", outcome=f"{request.method} {target}: {type(exc).__name__}: {exc}", target=self.bot_url))
            return JSONResponse(
                {"error": f"the bot is not answering at {self.bot_url}; is `uv run bot.py` running?", "code": "bot_unreachable"},
                status_code=502,
            )

        raw: list[tuple[bytes, bytes]] = []
        seen: set[str] = set()
        for name, value in upstream.headers.multi_items():
            lower = name.lower()
            if lower in HOP_BY_HOP:
                continue
            if lower == "location" and value.startswith(self.bot_url):
                # A redirect onto the bot's own address stays on this origin.
                value = value[len(self.bot_url) :] or "/"
            if lower == "x-frame-options":
                value = "SAMEORIGIN"
            seen.add(lower)
            raw.append((lower.encode("latin-1"), value.encode("latin-1")))
        if "content-security-policy" not in seen:
            raw.append((b"content-security-policy", FRAMED_CSP.encode("latin-1")))
        if "x-frame-options" not in seen:
            raw.append((b"x-frame-options", b"SAMEORIGIN"))
        if "permissions-policy" not in seen:
            raw.append((b"permissions-policy", FRAMED_PERMISSIONS.encode("latin-1")))

        # The bytes as the bot sent them (still encoded), so its Content-Length
        # and Content-Encoding stay true.
        response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(upstream.aclose))
        response.raw_headers = raw
        return response

    # --- WebSocket -----------------------------------------------------------------------

    async def websocket(self, websocket: WebSocket) -> None:
        target = self.ws_url + websocket.url.path + (f"?{websocket.url.query}" if websocket.url.query else "")
        offered = [Subprotocol(p) for p in (websocket.scope.get("subprotocols") or [])]
        headers = {
            name: value
            for name, value in self._forward_headers(websocket.headers, websocket.client.host if websocket.client else None, "https" if websocket.url.scheme == "wss" else "http").items()
            if name not in WS_HANDSHAKE
        }
        try:
            upstream = await ws_connect(
                target,
                subprotocols=offered or None,
                additional_headers=headers,
                max_size=None,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
            )
        except (TimeoutError, OSError, InvalidHandshake, InvalidURI, ConnectionClosed) as exc:
            logger.warning(event("app.bot_proxy.unreachable", outcome=f"websocket {websocket.url.path}: {type(exc).__name__}: {exc}", target=self.bot_url))
            await websocket.close(code=1013, reason="the bot is not answering")
            return

        await websocket.accept(subprotocol=upstream.subprotocol)
        logger.debug(event("app.bot_proxy.websocket_open", outcome=websocket.url.path, target=self.bot_url))

        async def downstream() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        async def upstream_side() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is not None:
                    await upstream.send(data)
                    continue
                text = message.get("text")
                if text is not None:
                    await upstream.send(text)

        tasks = [asyncio.create_task(downstream(), name="bot-proxy-down"), asyncio.create_task(upstream_side(), name="bot-proxy-up")]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (ConnectionClosed, WebSocketDisconnect)):
                    logger.warning(event("app.bot_proxy.websocket_error", outcome=f"{type(exc).__name__}: {exc}", target=self.bot_url))
        finally:
            await upstream.close()
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 — the browser (or the carrier) already hung up
                pass
            logger.debug(event("app.bot_proxy.websocket_closed", outcome=websocket.url.path, target=self.bot_url))


def install_bot_proxy(app: FastAPI, bot_url: str) -> BotProxy:
    """Register the catch-alls. Call it after every route and mount the application owns."""
    proxy = BotProxy(bot_url)
    app.add_api_route("/", proxy.http, methods=list(ROOT_METHODS), include_in_schema=False)
    app.add_api_route("/{path:path}", proxy.http, methods=list(METHODS), include_in_schema=False)
    app.add_api_websocket_route("/{path:path}", proxy.websocket)
    return proxy


__all__ = ["BotProxy", "FRAMED_CSP", "FRAMED_PERMISSIONS", "install_bot_proxy"]
