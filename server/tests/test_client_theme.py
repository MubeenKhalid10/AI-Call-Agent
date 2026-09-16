"""Checks that the bot's browser client is served in the application's design. Phase 33.

Run it from the `server/` directory::

    uv run python tests/test_client_theme.py

The Live AI Agent page frames the runner's prebuilt playground from the bot's
origin. `src/client_theme.py` serves that page with the application's tokens,
font and theme switch added, ahead of the runner's own `/client` mount. These
checks drive a bare FastAPI app through `TestClient`: the page is patched, the
stylesheet and fonts are served from this origin, a path outside the fonts
directory is refused, the routes win over a mount added afterwards (the
runner's order), a missing prebuilt client installs nothing, and the
application's page hands the theme to the frame. Deterministic; no keys, no
network, no database. Exit status is 0 when every check passes.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.client_theme import PLAYGROUND_THEME_KEY, THEME_MESSAGE_TYPE, install_client_theme, prebuilt_dist, themed_index  # noqa: E402

_failures: list[str] = []
WEB = SERVER / "web"
VENDOR_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Pipecat UI</title>
  <script type="module" crossorigin src="./assets/index-abc.js"></script>
  <link rel="stylesheet" crossorigin href="./assets/index-abc.css">
</head>
<body>
  <div id="root"></div>
</body>
</html>
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def fake_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(VENDOR_INDEX, encoding="utf-8")
    (dist / "assets" / "index-abc.css").write_text(":root{--color-background:#fff}", encoding="utf-8")
    (dist / "assets" / "index-abc.js").write_text("console.log('vendor')", encoding="utf-8")
    return dist


def check_page() -> None:
    print("\n=== the page ===")
    html = themed_index(VENDOR_INDEX)
    check("the vendor's page keeps its bundle", 'src="./assets/index-abc.js"' in html and 'href="./assets/index-abc.css"' in html)
    check("the theme stylesheet is linked after the bundle's own", html.index('href="./theme.css"') > html.index("index-abc.css"))
    check("the theme script is inside the head, before the body", html.index("<script>") < html.index("<body>"))
    check("the script writes the playground's own theme key, so its toggle stays in step", PLAYGROUND_THEME_KEY in html and "localStorage.setItem(KEY" in html)
    check("the script honours ?theme= and the application's message", 'get("theme")' in html and THEME_MESSAGE_TYPE in html and 'addEventListener("message"' in html)
    check("the script toggles the .dark / .light class the bundle's CSS keys on", 'classList.add(t)' in html and 'classList.remove(t === "dark" ? "light" : "dark")' in html)
    check("the script renames the vendor's title and hides its inline mark", '"Pipecat Playground"' in html and 'el.textContent = OURS' in html and 'classList.add("aiva-title")' in html)
    check("a page without a head is still themed", 'href="./theme.css"' in themed_index("<div id=root></div>"))


def check_routes() -> None:
    print("\n=== the routes ===")
    with tempfile.TemporaryDirectory() as tmp:
        dist = fake_dist(Path(tmp))
        app = FastAPI()
        installed = install_client_theme(app, WEB, dist=dist)
        check("installs when the prebuilt client and the stylesheet exist", installed)
        # The runner mounts the vendor's bundle afterwards; the themed routes must still win.
        app.mount("/client", StaticFiles(directory=str(dist), html=True), name="client")
        with TestClient(app) as client:
            for path in ("/client/", "/client/index.html"):
                r = client.get(path)
                check(f"GET {path} is the themed page", r.status_code == 200 and 'href="./theme.css"' in r.text and "no-store" in r.headers.get("cache-control", ""), f"{r.status_code}")
            r = client.get("/client/assets/index-abc.js")
            check("the bundle's own assets still come from the vendor's mount", r.status_code == 200 and "vendor" in r.text)
            r = client.get("/client/theme.css")
            check("theme.css is served as CSS", r.status_code == 200 and r.headers["content-type"].startswith("text/css") and "--color-agent" in r.text)
            r = client.get("/client/fonts/inter-latin.woff2")
            check("the bundled Inter is served from this origin", r.status_code == 200 and r.headers["content-type"] == "font/woff2" and len(r.content) > 10_000)
            for bad in ("/client/fonts/..%2F..%2Fapp.js", "/client/fonts/styles.css", "/client/fonts/nope.woff2"):
                r = client.get(bad)
                check(f"{bad} is refused", r.status_code == 404, str(r.status_code))
    with tempfile.TemporaryDirectory() as tmp:
        app = FastAPI()
        check("a missing prebuilt client installs nothing", install_client_theme(app, WEB, dist=Path(tmp) / "absent") is False and not [r for r in app.routes if getattr(r, "path", "").startswith("/client")])
    dist = prebuilt_dist()
    check("the installed prebuilt client is where the runner looks for it", dist is not None and (dist / "index.html").is_file(), str(dist))


def check_stylesheet() -> None:
    print("\n=== the stylesheet ===")
    css = (WEB / "client-theme.css").read_text(encoding="utf-8")
    app_css = (WEB / "styles.css").read_text(encoding="utf-8")
    light = css.split("html.dark")[0]
    dark = css.split("html.dark", 1)[1]
    check("light and dark blocks both exist", "html.dark" in css and ":root" in css)
    for token in ("--color-background", "--color-foreground", "--color-primary", "--color-border", "--color-agent", "--color-client", "--color-active", "--color-inactive", "--radius-base", "--font-sans"):
        check(f"{token} is set for light", f"{token}:" in light)
    for token in ("--color-background", "--color-foreground", "--color-primary", "--color-border", "--color-agent", "--color-client"):
        check(f"{token} is set for dark", f"{token}:" in dark)
    primary = re.search(r"--primary:\s*(#[0-9a-f]{6})", app_css).group(1)
    check("the client's primary is the application's primary", f"--color-primary: {primary}" in light, primary)
    dark_surface = re.search(r'\[data-theme="dark"\][^}]*--surface:\s*(#[0-9a-f]{6})', app_css).group(1)
    check("the client's dark background is the application's dark surface", f"--color-background: {dark_surface}" in dark, dark_surface)
    check("Inter is declared from the client's own origin", css.count("@font-face") == 2 and 'url("./fonts/inter-latin.woff2")' in css)
    check("the vendor's mark is hidden, the product's brand is the page's", '.aiva-title::before, .aiva-title::after { display: none !important; content: none !important; }' in css and 'img[src*="pipecat-logo"] { display: none; }' in css)


def check_application_side() -> None:
    print("\n=== the application's page ===")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    check("the Live page frames the client with the current theme", "/?theme=${currentTheme()}" in js)
    check("the theme toggle tells the frame", 'postMessage({ type: "aiva:theme"' in js)
    check("the message type is the one the client listens for", f'"{THEME_MESSAGE_TYPE}"' in js)
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py installs the theme before the runner's main()", "install_client_theme(app" in bot and bot.index("install_client_theme(app") < bot.rindex("    main()"))


def main() -> int:
    print("Client theme checks — the page, the routes, the stylesheet, the application's side.")
    check_page()
    check_routes()
    check_stylesheet()
    check_application_side()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
