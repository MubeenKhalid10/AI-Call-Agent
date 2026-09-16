"""The bot's browser client in the application's own design. Phase 33.

The dev runner mounts Pipecat's prebuilt playground at ``/client`` — a compiled
React bundle with its own (grey, Geist-flavoured) look, which the application's
Live AI Agent page embeds in a frame. The bundle cannot be configured from
outside, but it is themed with CSS custom properties (``--color-*``, a ``.dark``
class on ``<html>``) and its font stack already lists Inter, which it never
ships. So the application gives it three things, all served from the bot's own
origin so nothing about the runner or the frame policy changes:

* ``/client/`` and ``/client/index.html`` — the vendor's page with one
  stylesheet and one script added: ``theme.css`` remaps the playground's tokens
  to the application's palette (light and dark), and the script applies the
  theme the frame asks for (``?theme=dark``, or a ``postMessage`` when the
  person toggles it in the application) and keeps the playground's own toggle
  in step by writing its ``localStorage`` key.
* ``/client/theme.css`` — ``web/client-theme.css``.
* ``/client/fonts/<file>`` — the Inter files the application already bundles.

Routes registered here come before the runner's ``/client`` mount (the runner
adds it inside ``main()``), so they win for exactly these paths and the bundle's
assets keep coming from the package. If the prebuilt package is missing, the
runner has nothing to theme and this installs nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger

#: The playground's own theme key (``voice-ui-kit``'s ThemeProvider default).
PLAYGROUND_THEME_KEY = "voice-ui-kit-theme"
#: What a frame posts to switch the theme: ``{"type": "aiva:theme", "theme": "dark"}``.
THEME_MESSAGE_TYPE = "aiva:theme"

_FONT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.woff2$")

_SCRIPT = """<script>(function () {
  var KEY = %(key)r, MSG = %(msg)r;
  function apply(t) {
    if (t !== "dark" && t !== "light") return;
    try { localStorage.setItem(KEY, t); } catch (e) {}
    var h = document.documentElement;
    h.classList.remove(t === "dark" ? "light" : "dark");
    h.classList.add(t);
    h.style.colorScheme = t;
  }
  var wanted = new URLSearchParams(location.search).get("theme");
  if (wanted) apply(wanted);
  window.addEventListener("message", function (e) { var d = e.data; if (d && d.type === MSG) apply(d.theme); });
  document.title = "Voice client";
  var VENDOR = "Pipecat Playground", OURS = "Voice client";
  function rename(root) {
    var els = root.querySelectorAll("*");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.childElementCount !== 0 || el.textContent !== VENDOR) continue;
      el.textContent = OURS;
      el.classList.add("aiva-title");  // theme.css drops the vendor's mark, a ::before on the title
    }
  }
  window.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("root");
    if (!root) return;
    new MutationObserver(function () { rename(root); }).observe(root, { childList: true, subtree: true });
  });
})();</script>"""


def themed_index(html: str) -> str:
    """The vendor's ``index.html`` with the theme stylesheet and the theme script added."""
    script = _SCRIPT % {"key": PLAYGROUND_THEME_KEY, "msg": THEME_MESSAGE_TYPE}
    link = '<link rel="stylesheet" href="./theme.css">'
    out = html
    # The stylesheet after the bundle's own, so its unlayered rules win.
    if "</head>" in out:
        out = out.replace("</head>", f"  {link}\n  {script}\n</head>", 1)
    else:  # a page without a head: still themed, still scripted
        out = f"{link}\n{script}\n{out}"
    return out


def prebuilt_dist() -> Path | None:
    """Where the runner's prebuilt client lives, or None when the package is absent."""
    try:
        from pipecat_ai_prebuilt import frontend
    except Exception:  # noqa: BLE001 - the package may be missing or broken; then there is nothing to theme
        return None
    dist = getattr(frontend, "dist_dir", None)
    return Path(dist) if dist else None


def install_client_theme(app: FastAPI, web_dir: Path, dist: Path | None = None) -> bool:
    """Register the themed ``/client/`` page, ``theme.css`` and the fonts on ``app``.

    Returns True when installed; False when the prebuilt client or the
    application's stylesheet cannot be found (the runner then serves the
    vendor's page unchanged).
    """
    dist = dist if dist is not None else prebuilt_dist()
    index = (dist / "index.html") if dist else None
    css = web_dir / "client-theme.css"
    fonts = web_dir / "fonts"
    if index is None or not index.is_file():
        logger.warning("Client theme: the prebuilt client was not found; the vendor's page is served as is")
        return False
    if not css.is_file():
        logger.warning(f"Client theme: {css} is missing; the vendor's page is served as is")
        return False

    @app.get("/client/", include_in_schema=False)
    @app.get("/client/index.html", include_in_schema=False)
    async def themed_client() -> HTMLResponse:
        html = index.read_text(encoding="utf-8")
        return HTMLResponse(themed_index(html), headers={"Cache-Control": "no-store"})

    @app.get("/client/theme.css", include_in_schema=False)
    async def theme_css() -> FileResponse:
        return FileResponse(css, media_type="text/css; charset=utf-8", headers={"Cache-Control": "no-cache"})

    @app.get("/client/fonts/{name}", include_in_schema=False)
    async def theme_font(name: str) -> FileResponse:
        if not _FONT_NAME.match(name):
            raise HTTPException(status_code=404)
        path = fonts / name
        if not path.is_file() or path.parent != fonts:
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="font/woff2", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    logger.info("Client theme OK | /client/ in the application's design (theme.css + Inter), light and dark")
    return True


__all__ = ["PLAYGROUND_THEME_KEY", "THEME_MESSAGE_TYPE", "install_client_theme", "prebuilt_dist", "themed_index"]
