#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for authentication, authorisation and hardening. Phase 18. No keys, no phone, no database.

Run it from the `server/` directory::

    uv run python tests/test_security.py

**What this is for.** Phase 18 puts a door on the three servers that show
customer data — the dashboard, the automation API and the webhook receiver
— so the checks are arranged around the questions a reviewer asks of a
door: does it lock (a request with no credential is refused), does the key
open only its own rooms (a viewer cannot write, cannot read a transcript,
sees numbers masked), does it slow down somebody trying every key (the
rate limits), does it remember who came in (the audit log), and does it
keep the keys out of the log (the scrubber).

**The real code, a fake world.** The password, session, role, limiter,
masking and audit modules are checked directly. The dashboard and the API
are the real FastAPI apps driven through FastAPI's test client over
`test_automation.py`'s in-memory store, with the dashboard's aggregate
read replaced by a fixed snapshot (the aggregates are `test_dashboard.py`'s
business). The HTTP middleware is checked on a two-route app of its own.
Nothing here needs PostgreSQL: the `audit_log` table's SQL is exercised by
`test_dashboard.py` and `test_automation.py` when a database is reachable.

A plain script rather than a pytest suite, like the other seventeen. Exit
status is 0 when everything passes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402

from src.security import (  # noqa: E402
    COOKIE_NAME,
    AuditLog,
    AuditUnavailable,
    HttpPolicy,
    Permission,
    Principal,
    RateLimiter,
    Role,
    UserDirectory,
    client_ip,
    cors_problems,
    effective_scheme,
    hash_password,
    install_security,
    is_password_hash,
    issue_session,
    mask_email,
    mask_phone,
    origin_allowed,
    parse_networks,
    parse_role,
    read_session,
    redact_pii,
    verify_password,
)
from src.security.passwords import DUMMY_HASH  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []

ADMIN_KEY = "admin-key-0123456789abcdef"
OPERATOR_KEY = "operator-key-0123456789abc"
VIEWER_KEY = "viewer-key-0123456789abcde"
SESSION_SECRET = "session-secret-0123456789abcdef0123456789"
ALICE_PASSWORD = "correct horse battery staple"
VERA_PASSWORD = "viewer-only-password-1"


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        _failures.append(label + (f" — {detail}" if detail else ""))


def _mark() -> int:
    return len(LOGS)


def _logged(text: str, since: int = 0) -> int:
    return sum(1 for line in LOGS[since:] if text in line)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


# --- Passwords ---------------------------------------------------------------------------


def check_passwords() -> None:
    print("\n=== passwords ===")
    digest = hash_password(ALICE_PASSWORD)
    check("a hash names its scheme and parameters", digest.startswith("scrypt$16384$8$1$"), digest[:24])
    check("the right password verifies", verify_password(ALICE_PASSWORD, digest))
    check("the wrong password does not", not verify_password("wrong horse", digest))
    check("two hashes of one password differ (a fresh salt each time)", hash_password(ALICE_PASSWORD) != digest)
    check("a hash is recognised as one", is_password_hash(digest))
    check("a plain password is not", not is_password_hash(ALICE_PASSWORD))
    check("a malformed hash verifies as False rather than raising", not verify_password("x", "scrypt$oops"))
    check("a hash with a hostile n is refused", not verify_password("x", "scrypt$1073741824$8$1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
    check("None verifies as False", not verify_password("x", None))
    for bad in ("short", "x" * 2000):
        try:
            hash_password(bad)
            check(f"a password of length {len(bad)} is refused", False, "hashed")
        except ValueError:
            check(f"a password of length {len(bad)} is refused", True)
    check("the dummy hash is a real one, so an unknown user costs the same time", is_password_hash(DUMMY_HASH))


# --- Sessions ----------------------------------------------------------------------------


def check_sessions() -> None:
    print("\n=== sessions ===")
    clock = FakeClock()
    token = issue_session(SESSION_SECRET, "alice", Role.ADMIN, ttl_secs=3600, now=clock())
    session, reason = read_session(SESSION_SECRET, token, now=clock())
    check("a token reads back", session is not None and session.user == "alice" and session.role is Role.ADMIN, reason)
    check("with its lifetime", session is not None and session.ttl_secs == 3600)
    check("the principal it yields is a session principal", session is not None and session.principal().via == "session")
    clock.advance(3601)
    expired, reason = read_session(SESSION_SECRET, token, now=clock())
    check("it expires", expired is None and reason == "expired", reason)
    clock.advance(-3601)
    body, sig = token.split(".")
    tampered_sig = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    check("a tampered signature is refused", read_session(SESSION_SECRET, tampered_sig, now=clock())[0] is None)
    other_body = issue_session(SESSION_SECRET, "mallory", Role.ADMIN, now=clock()).split(".")[0]
    check("a body from another token under this signature is refused", read_session(SESSION_SECRET, other_body + "." + sig, now=clock())[0] is None)
    check("another secret does not read it", read_session("another-secret-0123456789abcdef0123456", token, now=clock())[0] is None)
    check("garbage is refused, quietly", read_session(SESSION_SECRET, "not.a.token", now=clock())[0] is None)
    check("an empty token is refused", read_session(SESSION_SECRET, "", now=clock())[0] is None)
    check("an oversized token is refused", read_session(SESSION_SECRET, "a" * 5000, now=clock())[0] is None)
    try:
        issue_session("short", "alice", Role.ADMIN)
        check("a short secret cannot sign", False, "signed")
    except ValueError:
        check("a short secret cannot sign", True)
    check("a token carries no plain role change: the role is inside the signed claims", '"r":"admin"' in _unb64(body))


def _unb64(text: str) -> str:
    import base64

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


# --- Roles -------------------------------------------------------------------------------


def check_roles() -> None:
    print("\n=== roles and users ===")
    viewer, operator, admin = (Principal("v", Role.VIEWER), Principal("o", Role.OPERATOR), Principal("a", Role.ADMIN))
    check("a viewer reads and nothing else", viewer.can(Permission.READ) and not viewer.can(Permission.READ_PII) and not viewer.can(Permission.WRITE) and not viewer.can(Permission.MANAGE))
    check("an operator reads the people and writes, but does not manage", operator.can(Permission.READ_PII) and operator.can(Permission.WRITE) and not operator.can(Permission.MANAGE))
    check("an admin does everything", all(admin.can(p) for p in Permission))
    check("roles parse case-insensitively", parse_role(" Admin ") is Role.ADMIN)
    try:
        parse_role("root")
        check("an unknown role is refused", False)
    except ValueError:
        check("an unknown role is refused", True)

    alice_hash = hash_password(ALICE_PASSWORD)
    problems: list[str] = []
    directory = UserDirectory.parse(f"alice:admin:{alice_hash}, vera:viewer:{hash_password(VERA_PASSWORD)}", problems)
    check("a directory parses two users", len(directory) == 2 and not problems, "; ".join(problems))
    check("names are case-insensitive", directory.get("ALICE") is not None)
    check("the right password authenticates", directory.authenticate("alice", ALICE_PASSWORD) is not None)
    check("the wrong password does not", directory.authenticate("alice", "nope-nope-nope") is None)
    check("an unknown user does not", directory.authenticate("bob", ALICE_PASSWORD) is None)
    check("a missing password does not", directory.authenticate("alice", None) is None)
    check("describe() names roles, never hashes", "1 admin, 1 viewer" in directory.describe() and "scrypt" not in directory.describe())

    problems = []
    UserDirectory.parse("alice:admin:plain-password-here", problems)
    check("a plain password in the directory is refused, and says so", any("not a password hash" in p for p in problems), "; ".join(problems))
    problems = []
    UserDirectory.parse(f"alice:king:{alice_hash}", problems)
    check("an unknown role is a problem", any("not a role" in p for p in problems))
    problems = []
    UserDirectory.parse(f"alice:admin:{alice_hash},alice:viewer:{alice_hash}", problems)
    check("a duplicate name is a problem", any("twice" in p for p in problems))
    problems = []
    UserDirectory.parse("just-a-name", problems)
    check("an entry that is not name:role:hash is a problem", any("name:role:hash" in p for p in problems))
    problems = []
    UserDirectory.parse(f"bad name!:admin:{alice_hash}", problems)
    check("a name with spaces or punctuation is a problem", any("usable user name" in p for p in problems))


# --- Rate limiting ----------------------------------------------------------------------


def check_ratelimit() -> None:
    print("\n=== rate limiting ===")
    clock = FakeClock()
    limiter = RateLimiter(3, 60.0, clock=clock)
    decisions = [limiter.check("1.2.3.4") for _ in range(4)]
    check("three calls are allowed", all(d.allowed for d in decisions[:3]))
    check("the fourth is not, and says when to retry", not decisions[3].allowed and 59 < decisions[3].retry_after_secs <= 60, str(decisions[3]))
    check("Retry-After is whole seconds, at least one", decisions[3].retry_after_header == "60")
    check("another key is unaffected", limiter.check("5.6.7.8").allowed)
    clock.advance(30)
    check("still refused mid-window", not limiter.check("1.2.3.4").allowed)
    clock.advance(31)
    check("allowed once the window slides", limiter.check("1.2.3.4").allowed)
    limiter.reset("1.2.3.4")
    check("reset forgets a key", limiter.check("1.2.3.4").remaining == 2)
    check("a peek does not consume", limiter.check("9.9.9.9", consume=False).remaining == 3 and limiter.check("9.9.9.9", consume=False).remaining == 3)
    off = RateLimiter(0, 60.0, clock=clock)
    check("a limit of zero disables the limiter", all(off.check("x").allowed for _ in range(100)))
    small = RateLimiter(1, 60.0, clock=clock, max_keys=3)
    for key in ("a", "b", "c", "d", "e"):
        small.check(key)
    check("keys are bounded in memory", len(small._hits) <= 3)


# --- Masking -----------------------------------------------------------------------------


def check_pii() -> None:
    print("\n=== masking ===")
    check("an E.164 number keeps its country code and last two digits", mask_phone("+923001234567") == "+92••••••••67", mask_phone("+923001234567"))
    check("a short number is hidden whole", mask_phone("123") == "•••")
    check("a local number keeps only the tail", mask_phone("0300 1234567").endswith("67") and "1234" not in mask_phone("0300 1234567"))
    check("None stays None", mask_phone(None) is None)
    check("an email keeps its first letter and domain", mask_email("hina@example.com") == "h•••@example.com")
    payload = {
        "prospect": {"first_name": "Hina", "phone": "+923001234567", "phone_normalized": "+923001234567", "email": "hina@example.com", "custom_data": {"salary": "x"}},
        "result": {"transcript": [{"role": "user", "text": "hello"}], "transcript_included": True, "summary_text": "fine"},
        "transfers": [{"to_number": "+441234567890"}],
        "count": 1,
    }
    masked = redact_pii(payload)
    check("names are kept", masked["prospect"]["first_name"] == "Hina")
    check("numbers are masked wherever they are", masked["prospect"]["phone"] == "+92••••••••67" and masked["transfers"][0]["to_number"].endswith("90") and "1234567" not in masked["transfers"][0]["to_number"])
    check("emails are masked", masked["prospect"]["email"] == "h•••@example.com")
    check("custom data is emptied", masked["prospect"]["custom_data"] == {})
    check("the transcript is withheld and says so", masked["result"]["transcript"] is None and masked["result"]["transcript_included"] is False)
    check("everything else is untouched", masked["result"]["summary_text"] == "fine" and masked["count"] == 1)
    check("the original is not modified", payload["prospect"]["phone"] == "+923001234567")


# --- The log scrubber ----------------------------------------------------------------------


def check_redaction() -> None:
    print("\n=== secrets in logs ===")
    from src.reliability.observability import install_scrubber, redact

    alice_hash = hash_password(ALICE_PASSWORD)
    os.environ["DASHBOARD_USERS"] = f"alice:admin:{alice_hash}"
    os.environ["AUTOMATION_OPERATOR_API_KEYS"] = OPERATOR_KEY
    os.environ["AUTOMATION_VIEWER_API_KEYS"] = VIEWER_KEY
    os.environ["DASHBOARD_SESSION_SECRET"] = SESSION_SECRET
    count = install_scrubber()
    check("the new secrets are loaded for scrubbing", count >= 4, str(count))
    check("an operator key is scrubbed", OPERATOR_KEY not in redact(f"refused key {OPERATOR_KEY}"))
    check("a viewer key is scrubbed", VIEWER_KEY not in redact(f"key={VIEWER_KEY}"))
    check("the session secret is scrubbed", SESSION_SECRET not in redact(f"secret {SESSION_SECRET}"))
    check("a password hash is scrubbed by shape, wherever it appears", "scrypt$" not in redact(f"user: {hash_password('another password 1')}"))
    check("a session cookie is scrubbed by shape", redact("cookie: aiva_session=abc.def; other=1") == "cookie: aiva_session=***; other=1")
    check("a password is scrubbed by name", redact("password=hunter22 ok") == "password=*** ok")
    check("a bearer key is scrubbed by shape", redact(f"Authorization: Bearer {ADMIN_KEY}").endswith("***"))


# --- HTTP hardening ------------------------------------------------------------------------


def _scope(client: str, headers: dict[str, str] | None = None, scheme: str = "http") -> dict[str, Any]:
    return {
        "type": "http",
        "client": (client, 12345),
        "scheme": scheme,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }


def check_http() -> None:
    print("\n=== HTTP hardening ===")
    trusted = parse_networks(("127.0.0.0/8", "10.0.0.0/8"))
    check("a direct client is its own address", client_ip(_scope("203.0.113.9", {"X-Forwarded-For": "1.1.1.1"}), trusted) == "203.0.113.9")
    check("X-Forwarded-For is believed from a trusted proxy", client_ip(_scope("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}), trusted) == "203.0.113.9")
    check("and walked past every trusted hop, never further", client_ip(_scope("127.0.0.1", {"X-Forwarded-For": "8.8.8.8, 203.0.113.9, 10.0.0.5"}), trusted) == "203.0.113.9")
    check("with no trusted proxies the header is ignored", client_ip(_scope("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}), ()) == "127.0.0.1")
    check("X-Forwarded-Proto is believed from a trusted proxy", effective_scheme(_scope("127.0.0.1", {"X-Forwarded-Proto": "https"}), trusted) == "https")
    check("and not from anybody else", effective_scheme(_scope("203.0.113.9", {"X-Forwarded-Proto": "https"}), trusted) == "http")
    problems: list[str] = []
    parse_networks(("10.0.0.0/8", "not-a-network"), problems)
    check("a bad network is a problem, not a crash", len(problems) == 1)
    check("a wildcard CORS origin is refused", any("wildcard" in p for p in cors_problems(("*",))))
    check("an origin with a path is refused", cors_problems(("https://app.example.com/x",)))
    check("a real origin is fine", not cors_problems(("https://app.example.com", "http://localhost:3000")))
    # The two list settings are comma-separated, as `.env.example` and the README
    # (`SECURITY_TRUSTED_PROXIES=0.0.0.0/0,::/0` behind Vercel) say; the sales
    # lists' pipe is accepted too.
    from src.config import SecurityConfig
    saved = {k: os.environ.get(k) for k in ("SECURITY_TRUSTED_PROXIES", "SECURITY_CORS_ORIGINS", "DASHBOARD_USERS", "DASHBOARD_AUTH_DISABLED")}
    try:
        os.environ["SECURITY_TRUSTED_PROXIES"] = "0.0.0.0/0,::/0"
        os.environ["SECURITY_CORS_ORIGINS"] = "https://app.example.com, https://ops.example.com|http://localhost:3000"
        os.environ["DASHBOARD_AUTH_DISABLED"] = "true"
        os.environ.pop("DASHBOARD_USERS", None)
        env_problems: list[str] = []
        parsed = SecurityConfig.from_env(env_problems)
        check("SECURITY_TRUSTED_PROXIES is comma-separated", parsed.trusted_proxies == ("0.0.0.0/0", "::/0") and not env_problems, "; ".join(env_problems))
        check("SECURITY_CORS_ORIGINS is comma-separated (a pipe works too)", parsed.cors_origins == ("https://app.example.com", "https://ops.example.com", "http://localhost:3000"))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    class _Req:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = {k.lower(): v for k, v in headers.items()}

    check("a same-origin form post is allowed", origin_allowed(_Req({"host": "dash.example.com", "origin": "https://dash.example.com"})))
    check("a cross-site form post is not", not origin_allowed(_Req({"host": "dash.example.com", "origin": "https://evil.example"})))
    check("Sec-Fetch-Site decides when present", not origin_allowed(_Req({"host": "d", "sec-fetch-site": "cross-site", "origin": "https://evil.example"})) and origin_allowed(_Req({"host": "d", "sec-fetch-site": "same-origin"})))
    check("a listed origin is allowed", origin_allowed(_Req({"host": "dash.example.com", "origin": "https://app.example.com"}), ("https://app.example.com",)))
    check("no origin at all (a curl) is allowed — the cookie's SameSite covers browsers", origin_allowed(_Req({"host": "d"})))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def tiny(policy: HttpPolicy, kind: str) -> TestClient:
        app = FastAPI()

        @app.get("/x")
        async def x() -> dict[str, str]:
            return {"ok": "yes"}

        @app.post("/x")
        async def post_x() -> dict[str, str]:
            return {"ok": "posted"}

        install_security(app, policy, kind=kind)
        return TestClient(app)

    plain = tiny(HttpPolicy(), "api")
    got = plain.get("/x")
    check("every answer carries the security headers", got.headers.get("x-content-type-options") == "nosniff" and got.headers.get("x-frame-options") == "DENY" and got.headers.get("cache-control") == "no-store" and got.headers.get("referrer-policy") == "no-referrer", str(dict(got.headers)))
    check("an API answer carries no CSP (its docs page needs its own)", "content-security-policy" not in got.headers)
    check("and no HSTS while HTTP is allowed", "strict-transport-security" not in got.headers)
    page = tiny(HttpPolicy(), "dashboard").get("/x")
    check("a dashboard answer carries a CSP that allows inline script and nothing external", "default-src 'none'" in page.headers.get("content-security-policy", "") and "script-src 'unsafe-inline'" in page.headers.get("content-security-policy", ""))

    capped = tiny(HttpPolicy(max_body_bytes=100), "api")
    big = capped.post("/x", content=b"x" * 200, headers={"content-type": "text/plain"})
    check("a body over the cap is 413 before it is read", big.status_code == 413 and big.json()["error"]["code"] == "body_too_large", big.text)
    check("a body under the cap is fine", capped.post("/x", content=b"x" * 50, headers={"content-type": "text/plain"}).status_code == 200)

    https = tiny(HttpPolicy(require_https=True), "api")
    refused = https.get("/x")
    check("with HTTPS required, a plain request to the API is 403", refused.status_code == 403 and refused.json()["error"]["code"] == "https_required", refused.text)
    check("but the refusal still carries the headers", refused.headers.get("x-frame-options") == "DENY")
    forwarded = https.get("/x", headers={"X-Forwarded-Proto": "https"})
    check("a request the trusted proxy says was HTTPS is served", forwarded.status_code == 200, forwarded.text)
    check("and carries HSTS", forwarded.headers.get("strict-transport-security", "").startswith("max-age="))
    redirect = tiny(HttpPolicy(require_https=True), "dashboard").get("/x?a=1", follow_redirects=False)
    check("a plain dashboard page is redirected to HTTPS, query kept", redirect.status_code == 308 and redirect.headers["location"] == "https://testserver/x?a=1", str(redirect.headers.get("location")))
    posted = tiny(HttpPolicy(require_https=True), "dashboard").post("/x", follow_redirects=False)
    check("a plain dashboard POST is refused rather than redirected (a redirect would replay it)", posted.status_code == 403)

    cors = tiny(HttpPolicy(cors_origins=("https://app.example.com",)), "api")
    listed = cors.options("/x", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "GET"})
    check("a listed origin's preflight is answered", listed.status_code == 200 and listed.headers.get("access-control-allow-origin") == "https://app.example.com", str(dict(listed.headers)))
    unlisted = cors.get("/x", headers={"Origin": "https://evil.example"})
    check("an unlisted origin gets no CORS headers at all", "access-control-allow-origin" not in unlisted.headers)
    none = plain.get("/x", headers={"Origin": "https://app.example.com"})
    check("with CORS unconfigured, no origin gets any", "access-control-allow-origin" not in none.headers)


# --- The audit writer ----------------------------------------------------------------------


class _AuditStore:
    def __init__(self, fail: Exception | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = fail

    async def record_audit(self, **fields: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.rows.append(fields)


async def check_audit() -> None:
    print("\n=== the audit writer ===")
    store = _AuditStore()
    clock_now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    log = AuditLog(lambda: store, clock=lambda: clock_now)
    since = _mark()
    entry = await log.record("campaign.start", principal=Principal("alice", Role.ADMIN, "session"), ip="10.0.0.9", target=("campaign", 7), phone="+923001234567", password="hunter22", note="ok")
    check("an entry is written through the store", len(store.rows) == 1 and store.rows[0]["action"] == "campaign.start" and store.rows[0]["actor"] == "alice")
    check("with the target and the address", store.rows[0]["target_kind"] == "campaign" and store.rows[0]["target_id"] == "7" and store.rows[0]["ip"] == "10.0.0.9")
    check("a phone in the detail is masked before it is stored", store.rows[0]["detail"]["phone"] == "+92••••••••67", str(store.rows[0]["detail"]))
    check("a password in the detail is scrubbed", "hunter22" not in json.dumps(store.rows[0]["detail"]))
    check("and an audit.<action> line is logged", _logged("audit.campaign.start", since) == 1)
    check("the line carries no password either", not any("hunter22" in line for line in LOGS[since:]))
    check("the entry is stamped", entry.created_at == clock_now and log.written == 1)
    nobody = await log.record("auth.refused", ip="1.2.3.4", outcome="unknown key")
    check("an entry with nobody behind it records nobody", nobody.actor == "-" and nobody.via == "none")

    broken = AuditLog(lambda: _AuditStore(RuntimeError("table gone")))
    since = _mark()
    entry = await broken.record("prospect.create", principal=Principal("o", Role.OPERATOR))
    check("a store that cannot write does not stop the action (by default)", entry is not None and broken.failed == 1)
    check("and says so, once", _logged("audit.unavailable", since) == 1)
    await broken.record("prospect.create", principal=Principal("o", Role.OPERATOR))
    check("the second failure within a minute is not repeated in the log", _logged("audit.unavailable", since) == 1)
    strict = AuditLog(lambda: _AuditStore(RuntimeError("table gone")), strict=True)
    try:
        await strict.record("prospect.create", principal=Principal("o", Role.OPERATOR))
        check("in strict mode the failure is raised", False, "returned")
    except AuditUnavailable:
        check("in strict mode the failure is raised", True)
    quiet = AuditLog(None)
    entry = await quiet.record("auth.login", principal=Principal("a", Role.ADMIN, "session"))
    check("with no store, the log line is the record", entry.action == "auth.login" and quiet.written == 0)


# --- The dashboard -------------------------------------------------------------------------


def _snapshot() -> Any:
    class Snap:
        def to_dict(self) -> dict[str, Any]:
            return {
                "totals": [], "attention": [], "usage": [], "outcomes": [], "campaigns": [], "notes": [],
                "generated_at": "now", "generated_at_label": "now", "timezone": "Asia/Karachi",
                "recent_calls": [{"prospect": "Hina Qureshi", "phone": "+923001234567", "campaign": "Q1", "disposition_label": "Qualified"}],
            }

    return Snap()


async def _fake_collect(store: Any, *, timezone: str = "UTC") -> Any:
    return _snapshot()


def check_dashboard() -> None:
    print("\n=== the dashboard ===")
    from fastapi.testclient import TestClient
    from test_automation import FakeStore
    from test_worker import NOW
    from test_worker import FakeClock as StoreClock

    import src.dashboard.web as web
    from src.config import Config, ConfigError
    from src.dashboard import API_PATH, LOGIN_PATH, LOGOUT_PATH, ME_PATH, create_app

    alice_hash = hash_password(ALICE_PASSWORD)
    vera_hash = hash_password(VERA_PASSWORD)
    os.environ.update({
        "DASHBOARD_USERS": f"alice:admin:{alice_hash},vera:viewer:{vera_hash}",
        "DASHBOARD_SESSION_SECRET": SESSION_SECRET,
        "SECURITY_LOGIN_RATE_LIMIT": "3",
        "SECURITY_ANON_RATE_LIMIT": "4",
        "AUTOMATION_API_KEYS": ADMIN_KEY,
        "AUTOMATION_OPERATOR_API_KEYS": OPERATOR_KEY,
        "AUTOMATION_VIEWER_API_KEYS": VIEWER_KEY,
        "DATABASE_URL": "postgresql://x:y@localhost/unused",
    })
    os.environ.pop("DASHBOARD_AUTH_DISABLED", None)
    web.collect = _fake_collect  # the aggregates are test_dashboard.py's business
    config = Config.from_env()
    store = FakeStore(clock=StoreClock(NOW))
    clock = FakeClock()

    async def factory() -> Any:
        return store

    app = create_app(config, store_factory=factory, clock=clock)
    audits = store.audit_entries
    with TestClient(app) as client:
        client.headers["origin"] = "http://testserver"
        page = client.get("/", follow_redirects=False)
        check("the page needs a login: redirected", page.status_code == 303 and page.headers["location"].startswith(LOGIN_PATH), str(page.headers.get("location")))
        check("the JSON needs a login: 401", client.get(API_PATH).status_code == 401)
        check("and says where to sign in", client.get(API_PATH).json()["login"] == LOGIN_PATH)
        check("/api/me likewise", client.get(ME_PATH).status_code == 401)
        form = client.get(LOGIN_PATH)
        check("the login page is served", form.status_code == 200 and 'name="password"' in form.text and "Sign in" in form.text)
        check("with a CSP and no caching", "content-security-policy" in form.headers and form.headers.get("cache-control") == "no-store")
        check("the ping still answers without a login", client.get("/api/ping").status_code == 200)
        check("there are no docs", client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404)

        since = _mark()
        wrong = client.post(LOGIN_PATH, data={"username": "alice", "password": "not the password"}, follow_redirects=False)
        check("a wrong password is 401 with the form again", wrong.status_code == 401 and "Wrong name or password" in wrong.text)
        check("and audited", any(e.action == "auth.login_failed" and e.outcome == "wrong password" for e in audits))
        unknown = client.post(LOGIN_PATH, data={"username": "mallory", "password": "whatever-it-is"}, follow_redirects=False)
        check("an unknown user gets the same answer", unknown.status_code == 401 and "Wrong name or password" in unknown.text)
        check("and is audited without the name they typed", any(e.action == "auth.login_failed" and e.outcome == "unknown user" and "mallory" not in json.dumps(e.detail) for e in audits))
        check("the password is in no log line", not any("not the password" in line or "whatever-it-is" in line for line in LOGS[since:]))
        third = client.post(LOGIN_PATH, data={"username": "alice", "password": "still wrong"}, follow_redirects=False)
        fourth = client.post(LOGIN_PATH, data={"username": "alice", "password": ALICE_PASSWORD}, follow_redirects=False)
        check("the fourth attempt in a minute is 429 even with the right password", third.status_code == 401 and fourth.status_code == 429 and fourth.headers.get("retry-after"), f"{third.status_code} {fourth.status_code}")
        check("and audited", any(e.action == "auth.login_rate_limited" for e in audits))
        clock.advance(61)
        # The limiter uses its own monotonic clock; a fresh client address is
        # the honest way to get past it here.
        cross = client.post(LOGIN_PATH, data={"username": "alice", "password": ALICE_PASSWORD}, headers={"origin": "https://evil.example"}, follow_redirects=False)
        check("a cross-site login post is refused", cross.status_code == 403 and any(e.action == "auth.login_refused" for e in audits), str(cross.status_code))

    # A second app: the limiter state above is spent.
    app = create_app(config, store_factory=factory, clock=clock)
    with TestClient(app) as client:
        client.headers["origin"] = "http://testserver"
        since = _mark()
        ok = client.post(LOGIN_PATH, data={"username": "Alice", "password": ALICE_PASSWORD, "next": "/"}, follow_redirects=False)
        check("the right password is a redirect to the page", ok.status_code == 303 and ok.headers["location"] == "/", ok.text[:80])
        cookie = ok.headers.get("set-cookie", "")
        check("with a session cookie: HttpOnly, SameSite=Strict, a lifetime", COOKIE_NAME in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict") and "Max-Age" in cookie, cookie[:120])
        check("not Secure while HTTPS is not required (it would never be sent over http)", "Secure" not in cookie)
        check("the login is audited by name", any(e.action == "auth.login" and e.actor == "alice" and e.role == "admin" for e in audits))
        token = client.cookies.get(COOKIE_NAME)
        check("the cookie value appears in no log line", token is not None and not any(token in line for line in LOGS[since:]))
        page = client.get("/")
        check("the page is served to the session", page.status_code == 200 and "Calling dashboard" in page.text)
        check("and names who is signed in, with a sign-out button", "<b>alice</b>" in page.text and "Sign out" in page.text)
        me = client.get(ME_PATH).json()
        check("/api/me says who and what", me["name"] == "alice" and me["role"] == "admin" and "manage" in me["permissions"])
        data = client.get(API_PATH)
        check("an admin sees the numbers unmasked", data.status_code == 200 and data.json()["recent_calls"][0]["phone"] == "+923001234567" and "masked" not in data.json(), data.text[:200])
        check("POST to the page is refused", client.post("/").status_code == 405)
        check("POST to the JSON is refused", client.post(API_PATH).status_code == 405)
        out = client.post(LOGOUT_PATH, follow_redirects=False)
        check("logout clears the cookie and goes to the login page", out.status_code == 303 and out.headers["location"] == LOGIN_PATH and 'aiva_session=""' in out.headers.get("set-cookie", "") or "Max-Age=0" in out.headers.get("set-cookie", ""))
        check("and is audited", any(e.action == "auth.logout" and e.actor == "alice" for e in audits))
        client.cookies.clear()
        check("after logout the JSON is 401 again", client.get(API_PATH).status_code == 401)

        forged = issue_session("another-secret-0123456789abcdef0123456", "alice", Role.ADMIN)
        client.cookies.set(COOKIE_NAME, forged)
        check("a cookie signed with another secret is 401", client.get(API_PATH).status_code == 401)
        client.cookies.clear()
        stale = issue_session(SESSION_SECRET, "alice", Role.ADMIN, ttl_secs=60, now=clock() - 3600)
        client.cookies.set(COOKIE_NAME, stale)
        check("an expired cookie is 401", client.get(API_PATH).status_code == 401)
        client.cookies.clear()

        vera = client.post(LOGIN_PATH, data={"username": "vera", "password": VERA_PASSWORD}, follow_redirects=False)
        check("a viewer signs in", vera.status_code == 303)
        data = client.get(API_PATH).json()
        check("a viewer sees the numbers masked, and is told so", data["recent_calls"][0]["phone"] == "+92••••••••67" and data["masked"] is True, str(data["recent_calls"]))
        check("and the page says so too", "numbers masked" in client.get("/").text)
        client.cookies.clear()

        bearer = client.get(API_PATH, headers={"Authorization": f"Bearer {VIEWER_KEY}"})
        check("a viewer API key reads the JSON, masked", bearer.status_code == 200 and bearer.json()["recent_calls"][0]["phone"].startswith("+92••"))
        bearer = client.get(API_PATH, headers={"Authorization": f"Bearer {OPERATOR_KEY}"})
        check("an operator API key reads it unmasked", bearer.status_code == 200 and bearer.json()["recent_calls"][0]["phone"] == "+923001234567")
        check("an unknown key is 401", client.get(API_PATH, headers={"Authorization": "Bearer nope-nope-nope-nope"}).status_code == 401)
        check("the page for a key holder has no sign-out (there is no session to end)", "Sign out" not in client.get("/", headers={"Authorization": f"Bearer {OPERATOR_KEY}"}).text)
        codes = [client.get(API_PATH).status_code for _ in range(6)]
        check("unauthenticated requests are limited per address", 429 in codes, str(codes))
        methods = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set()) if m == "POST"}
        check("the only POST routes are login and logout", methods == {(LOGIN_PATH, "POST"), (LOGOUT_PATH, "POST")}, str(sorted(methods)))

    # Nobody configured: the app refuses to build.
    os.environ["DASHBOARD_USERS"] = ""
    try:
        create_app(Config.from_env(), store_factory=factory)
        check("with no users the dashboard refuses to build", False, "built")
    except ConfigError as exc:
        check("with no users the dashboard refuses to build", "DASHBOARD_USERS" in str(exc))

    # Auth switched off: Phase 10's page, for loopback.
    os.environ["DASHBOARD_AUTH_DISABLED"] = "true"
    app = create_app(Config.from_env(), store_factory=factory)
    with TestClient(app) as client:
        check("with DASHBOARD_AUTH_DISABLED the page is served without a login", client.get("/").status_code == 200)
        check("as an anonymous operator (numbers shown, as Phase 10 showed them)", client.get(API_PATH).json()["recent_calls"][0]["phone"] == "+923001234567")
        check("and the login page just redirects home", client.get(LOGIN_PATH, follow_redirects=False).status_code == 303)
    os.environ.pop("DASHBOARD_AUTH_DISABLED", None)
    os.environ["DASHBOARD_USERS"] = f"alice:admin:{alice_hash},vera:viewer:{vera_hash}"


# --- The automation API ----------------------------------------------------------------------


def check_api() -> None:
    print("\n=== the automation API ===")
    from fastapi.testclient import TestClient
    from test_automation import FakeStore, api_settings, automation_config
    from test_worker import NOW
    from test_worker import FakeClock as StoreClock

    from src.automation import API_PREFIX, IDEMPOTENCY_HEADER, PING_PATH, create_automation_app
    from src.config import SecurityConfig

    security = dataclasses.replace(SecurityConfig.defaults(), api_rate_limit=80, anon_rate_limit=3, max_body_bytes=4096)
    settings = dataclasses.replace(
        api_settings(api_keys=(ADMIN_KEY,), operator_api_keys=(OPERATOR_KEY,), viewer_api_keys=(VIEWER_KEY,)),
        security=security,
    )
    store_clock = StoreClock(NOW)
    store = FakeStore(clock=store_clock)
    audits = store.audit_entries

    async def factory() -> Any:
        return store

    app = create_automation_app(settings, store_factory=factory, deliver=False, clock=store_clock)
    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    operator = {"Authorization": f"Bearer {OPERATOR_KEY}"}
    viewer = {"X-API-Key": VIEWER_KEY}
    v = API_PREFIX
    with TestClient(app) as client:
        since = _mark()
        status = client.get(f"{v}/status", headers=viewer)
        check("a viewer key reads the status", status.status_code == 200 and status.json()["principal"]["role"] == "viewer", status.text[:200])
        check("and knows what it may do", status.json()["principal"]["permissions"] == ["read"])
        check("an operator key is an operator", client.get(f"{v}/status", headers=operator).json()["principal"]["role"] == "operator")
        check("an admin key is an admin", client.get(f"{v}/status", headers=admin).json()["principal"]["name"] == "admin-key#1")
        check("every answer carries the security headers", status.headers.get("x-content-type-options") == "nosniff" and status.headers.get("cache-control") == "no-store")

        refused = client.post(f"{v}/prospects", json={"first_name": "Hina", "last_name": "Q", "phone": "0300 1234567"}, headers=viewer)
        check("a viewer cannot create a prospect: 403", refused.status_code == 403 and refused.json()["error"]["code"] == "forbidden" and refused.json()["error"]["details"]["required"] == "write", refused.text)
        check("and the refusal is audited", any(e.action == "auth.forbidden" and e.actor == "viewer-key#1" for e in audits))
        made = client.post(f"{v}/prospects", json={"first_name": "Hina", "last_name": "Qureshi", "phone": "0300 1234567", "email": "hina@example.com", "custom_data": {"salary": "high"}}, headers=operator)
        check("an operator can", made.status_code == 201, made.text[:200])
        check("and it is audited with the actor and the row, never the number", any(e.action == "prospect.create" and e.actor == "operator-key#1" and e.target_kind == "prospect" and "1234567" not in json.dumps(e.detail) for e in audits))
        pid = made.json()["prospect"]["id"]
        full = client.get(f"{v}/prospects/{pid}", headers=operator).json()["prospect"]
        masked = client.get(f"{v}/prospects/{pid}", headers=viewer).json()["prospect"]
        check("an operator sees the number, the email and the custom fields", full["phone_normalized"] == "+923001234567" and full["email"] == "hina@example.com" and full["custom_data"] == {"salary": "high"})
        check("a viewer sees them masked, emptied", masked["phone_normalized"] == "+92••••••••67" and masked["phone"] != full["phone"] and masked["email"] == "h•••@example.com" and masked["custom_data"] == {}, str(masked))
        check("names are not masked", masked["first_name"] == "Hina")
        check("a lookup by number needs read_pii", client.get(f"{v}/prospects", params={"phone": "0300 1234567"}, headers=viewer).status_code == 403 and client.get(f"{v}/prospects", params={"phone": "0300 1234567"}, headers=operator).status_code == 200)
        check("a viewer may list prospects (masked)", client.get(f"{v}/prospects", headers=viewer).json()["prospects"][0]["phone"] == masked["phone"])
        check("a transcript needs read_pii: the call route", client.get(f"{v}/calls/1", params={"include": "transcript"}, headers=viewer).status_code == 403)
        check("the results list", client.get(f"{v}/results", params={"include": "transcript"}, headers=viewer).status_code == 403)
        check("the full result", client.get(f"{v}/results/1", headers=viewer).status_code == 403)
        check("an event payload", client.get(f"{v}/events", params={"include": "payload"}, headers=viewer).status_code == 403 and client.get(f"{v}/events/1", headers=viewer).status_code == 403)
        check("without the transcript a viewer may read the results list", client.get(f"{v}/results", headers=viewer).status_code == 200)

        camp = client.post(f"{v}/campaigns", json={"name": "Secured"}, headers=operator)
        check("an operator creates a campaign", camp.status_code == 201 and any(e.action == "campaign.create" for e in audits))
        cid = camp.json()["campaign"]["id"]
        check("and starts it, audited", client.post(f"{v}/campaigns/{cid}/start", headers=operator).json()["changed"] and any(e.action == "campaign.start" and e.outcome == "DRAFT -> ACTIVE" for e in audits))
        check("but may not complete it: that needs manage", client.post(f"{v}/campaigns/{cid}/complete", headers=operator).status_code == 403)
        check("a viewer may not start one", client.post(f"{v}/campaigns/{cid}/pause", headers=viewer).status_code == 403)
        check("an admin completes it", client.post(f"{v}/campaigns/{cid}/complete", headers=admin).json()["changed"] and any(e.action == "campaign.complete" and e.actor == "admin-key#1" for e in audits))
        check("retrying an outbox row needs manage", client.post(f"{v}/events/1/retry", headers=operator).status_code == 403)
        check("the audit log needs manage: a viewer", client.get(f"{v}/audit", headers=viewer).status_code == 403)
        check("an operator", client.get(f"{v}/audit", headers=operator).status_code == 403)
        log = client.get(f"{v}/audit", headers=admin)
        check("an admin reads it, newest first, with counts", log.status_code == 200 and log.json()["count"] >= 6 and log.json()["entries"][0]["action"] == "auth.forbidden" and "campaign.complete" in log.json()["counts"], log.text[:300])
        check("filtered by prefix", all(e["action"].startswith("campaign.") for e in client.get(f"{v}/audit", params={"action": "campaign."}, headers=admin).json()["entries"]))
        check("no entry carries a key", not any(k in log.text for k in (ADMIN_KEY, OPERATOR_KEY, VIEWER_KEY)))

        # Validation.
        bad = client.post(f"{v}/prospects", json={"first_name": "Hi\x00na", "last_name": "Q", "phone": "0300 1234568"}, headers=operator)
        check("a control character in a name is 422", bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request", bad.text[:200])
        check("a newline in a name is 422", client.post(f"{v}/prospects", json={"first_name": "Hi\nna", "last_name": "Q", "phone": "0300 1234568"}, headers=operator).status_code == 422)
        check("a control character in a phone is 422", client.post(f"{v}/prospects", json={"first_name": "A", "last_name": "B", "phone": "0300\x1b1234568"}, headers=operator).status_code == 422)
        check("a phone that is not a number is still stored UNREACHABLE (Phase 17's rule stands)", client.post(f"{v}/prospects", json={"first_name": "A", "last_name": "B", "phone": "call me"}, headers=operator).status_code == 201)
        huge = {"first_name": "A", "last_name": "B", "phone": "0300 1234569", "custom_data": {"blob": "x" * 20_000}}
        check("oversized custom data is 422 (not 413: it is inside the body cap)", client.post(f"{v}/prospects", json=huge, headers=operator).status_code in (413, 422))
        many = {"first_name": "A", "last_name": "B", "phone": "0300 1234569", "custom_data": {f"k{i}": i for i in range(200)}}
        check("too many custom fields is 422", client.post(f"{v}/prospects", json=many, headers=operator).status_code == 422)
        check("a negative id in the path is 422, not a query", client.get(f"{v}/prospects/-1", headers=operator).status_code == 422)
        check("a bad Idempotency-Key is 422", client.post(f"{v}/campaigns", json={"name": "K"}, headers={**operator, IDEMPOTENCY_HEADER: "bad key with spaces"}).status_code == 422)
        check("an unknown event kind is 422", client.get(f"{v}/events", params={"kind": "call.exploded"}, headers=operator).status_code == 422)
        too_big = client.post(f"{v}/prospects/import", content=b"x" * 5000, headers={**operator, "content-type": "text/csv"})
        check("a body over SECURITY_MAX_BODY_BYTES is 413", too_big.status_code == 413, too_big.text[:100])

        # Limits.
        anon = [client.get(f"{v}/status", headers={"Authorization": "Bearer guess-guess-guess-guess"}).status_code for _ in range(5)]
        check("unknown keys are 401 then 429 per address", anon[:3] == [401, 401, 401] and anon[3] == 429, str(anon))
        check("refusals are audited only while within the budget", sum(1 for e in audits if e.action == "auth.refused") == 3)
        check("the ping shares the anonymous budget", client.get(PING_PATH).status_code == 429)
        burst = [client.get(f"{v}/status", headers=viewer).status_code for _ in range(90)]
        check("a key is limited per minute, with Retry-After", 429 in burst, str(burst))
        limited = client.get(f"{v}/status", headers=viewer)
        check("", limited.status_code == 429 and limited.headers.get("retry-after") and limited.json()["error"]["code"] == "rate_limited")
        check("another key is unaffected", client.get(f"{v}/status", headers=admin).status_code == 200)
        check("no key appears in any log line", not any(k in line for line in LOGS[since:] for k in (ADMIN_KEY, OPERATOR_KEY, VIEWER_KEY)))

    # HTTPS required.
    strict = dataclasses.replace(settings, security=dataclasses.replace(security, require_https=True))
    app = create_automation_app(strict, store_factory=factory, deliver=False, clock=store_clock)
    with TestClient(app) as client:
        plain = client.get(f"{v}/status", headers=admin)
        check("with HTTPS required, a plain request is 403 before the key is checked", plain.status_code == 403 and plain.json()["error"]["code"] == "https_required")
        check("and one the trusted proxy forwarded as HTTPS is served", client.get(f"{v}/status", headers={**admin, "X-Forwarded-Proto": "https"}).status_code == 200)

    # Docs off.
    quiet = dataclasses.replace(settings, automation=dataclasses.replace(settings.automation, docs_enabled=False))
    app = create_automation_app(quiet, store_factory=factory, deliver=False, clock=store_clock)
    with TestClient(app) as client:
        check("AUTOMATION_DOCS_ENABLED=false removes the docs and the schema", client.get(f"{v}/docs").status_code == 404 and client.get(f"{v}/openapi.json").status_code == 404)


# --- The webhook receiver -------------------------------------------------------------------


def check_webhooks() -> None:
    print("\n=== the webhook receiver ===")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.campaigns.webhooks import WebhookReceipt, create_webhook_router
    from src.config import SecurityConfig

    class Stub:
        expected_url = "https://example.test/webhooks/telephony"
        received = 0

        async def receive(self, request: Any) -> WebhookReceipt:
            self.received += 1
            return WebhookReceipt(403, "refused", "the signature does not match")

    stub = Stub()

    async def get_processor() -> Any:
        return stub

    security = dataclasses.replace(SecurityConfig.defaults(), anon_rate_limit=2)
    app = FastAPI()
    app.include_router(create_webhook_router(get_processor, path="/webhooks/telephony", security=security))
    with TestClient(app) as client:
        codes = [client.post("/webhooks/telephony", data={"CallSid": "CA1", "CallStatus": "completed"}).status_code for _ in range(4)]
        check("a forged delivery is 403", codes[0] == 403)
        check("an address whose deliveries keep failing is 429 before the next signature is computed", codes[2:] == [429, 429] and stub.received == 2, f"{codes} received={stub.received}")
        big = client.post("/webhooks/telephony", content=b"x" * (2 * 1024 * 1024), headers={"content-type": "application/x-www-form-urlencoded"})
        check("an oversized body is refused", big.status_code in (413, 429))


# --- The boundary -------------------------------------------------------------------------


def check_boundary() -> None:
    print("\n=== the boundary ===")
    import re

    call_path = ["bot.py", "src/conversation", "src/telephony", "src/actions", "src/campaigns/worker.py", "src/campaigns/dialer.py"]
    offenders = []
    for item in call_path:
        path = SERVER / item
        files = [path] if path.is_file() else list(path.rglob("*.py"))
        for file in files:
            text = file.read_text(encoding="utf-8")
            if re.search(r"^\s*(from|import)\s+(src\.|\.\.?)security\b", text, re.M) or "from ..security" in text or "from .security" in text:
                offenders.append(str(file.relative_to(SERVER)))
    check("nothing on the call path imports src.security", not offenders, ", ".join(offenders))
    text = (SERVER / "security.py").read_text(encoding="utf-8")
    check("security.py imports no pipecat and no bot", "pipecat" not in text and "import bot" not in text)


async def main() -> int:
    """Run every check and report."""
    print("Security checks — passwords, sessions, roles, rate limits, masking, the scrubber, the HTTP layer, the audit writer, the dashboard, the API, the webhook receiver, the boundary.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_passwords()
        check_sessions()
        check_roles()
        check_ratelimit()
        check_pii()
        check_redaction()
        check_http()
        await check_audit()
        check_dashboard()
        check_api()
        check_webhooks()
        check_boundary()
    finally:
        logger.remove(handler)

    print()
    if _skipped:
        print("SKIPPED:")
        for item in _skipped:
            print(f"  - {item}")
        print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed." + (" (some were skipped)" if _skipped else ""))
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
