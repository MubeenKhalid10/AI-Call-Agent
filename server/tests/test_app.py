"""Phase 24 checks: the unified application, over the existing dashboard and API.

What is checked, driven through FastAPI's test client over the in-memory
store the automation checks use:

* the page and its files are served, with the application's own
  Content-Security-Policy (scripts from files, the bot's client framed);
* one login (the dashboard's) drives every part: the session route, the
  dashboard's JSON, the automation API — which now accepts the session
  cookie — and the knowledge routes; an API key still works as before;
* a cookie-authenticated write must carry the fetch header (the CSRF
  belt); a viewer's session cannot write; a wrong role is refused where
  it was refused before;
* the campaign-configuration route writes the agent profile and pacing
  the brief reads;
* the whole flow the phase asks for, through the application's own
  routes: import a CSV dry-run (nothing written), import it for real into a
  campaign, start the campaign, and the real scheduler — ticked over the
  same store — places the call with the contact's ids on the handshake;
* the knowledge base: list, upload, search, delete, and off;
* Phase 27, the Register page's route and the sign-up approval: a viewer
  sign-up is active at once; an operator or admin sign-up is a pending
  request that cannot sign in until an admin approves it (as the role it
  asked for) or rejects it; the role in the payload never grants anything by
  itself; operators, viewers and strangers cannot decide requests; a
  duplicate name or email is a 409 naming the field; bad input is a 422 with
  a `fields` map; a configured name cannot be taken; the page is closed by
  configuration; a decision made on another process is seen at login; the
  existing users, login and role permissions are unchanged;
* the boundary: nothing on the call path imports the application package.

Run from `server/`:

    uv run python tests/test_app.py

Exit status is non-zero when any check fails.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")

# Module level: the stand-in bot's handlers are annotated under `from __future__ import annotations`.
from fastapi import Request, WebSocket  # noqa: E402
from loguru import logger  # noqa: E402

_failures: list[str] = []
LOGS: list[str] = []

ADMIN_KEY = "app-admin-key-0123456789abcdef"
OPERATOR_PASSWORD = "operator-password-1234"
VIEWER_PASSWORD = "viewer-password-12345"
SESSION_SECRET = "app-session-secret-0123456789abcdef0123456789"
BOT_URL = "http://bot.test:7860"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


# --- Doubles for the knowledge base ------------------------------------------------------------


@dataclasses.dataclass
class FakeDocument:
    source: str
    title: str
    chunk_count: int
    byte_size: int
    embed_model: str
    content_hash: str
    ingested_at: str


class FakeKnowledge:
    """The knowledge store's surface: documents and a search over stored passages."""

    def __init__(self) -> None:
        self.documents: dict[str, FakeDocument] = {}
        self.chunks: dict[str, list[str]] = {}
        self.closed = False

    async def list_documents(self) -> list[FakeDocument]:
        return list(self.documents.values())

    async def counts(self) -> tuple[int, int]:
        return len(self.documents), sum(len(c) for c in self.chunks.values())

    async def add_document(self, *, source: str, title: str, content_hash: str, byte_size: int, chunks: list[str], vectors: list[list[float]]) -> int:
        self.documents[source] = FakeDocument(source, title, len(chunks), byte_size, "fake", content_hash, datetime.now(UTC).isoformat())
        self.chunks[source] = list(chunks)
        return len(chunks)

    async def delete_document(self, source: str) -> bool:
        return self.documents.pop(source, None) is not None and self.chunks.pop(source, None) is not None

    async def search(self, vector: list[float], *, limit: int = 4, min_score: float = 0.0) -> list[Any]:
        from src.knowledge_store import Match

        wanted = vector[0]
        found = []
        for source, pieces in self.chunks.items():
            for piece in pieces:
                if wanted in piece.lower():
                    fields = {"content": piece, "source": source, "title": self.documents[source].title, "ordinal": 0, "score": 0.9}
                    found.append(Match(**{k: v for k, v in fields.items() if k in Match.__dataclass_fields__}))
        return found[:limit]

    async def close(self) -> None:
        self.closed = True


class FakeEmbedder:
    model_name = "fake"
    dimensions = 1

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    async def embed_query_async(self, text: str) -> list[Any]:
        return [text.split()[0].lower()]


# --- Building the application ----------------------------------------------------------------


def build(*, kb_enabled: bool = True, auth_disabled: bool = False, registration: str | None = None, store: Any = None, login_rate: str = "100", bot_url: str = BOT_URL, proxy_bot: bool = False, stream: bool = True):
    from test_automation import FakeStore
    from test_security import _fake_collect
    from test_worker import NOW, FakeClock

    from src.app import create_unified_app
    from src.config import Config
    from src.dashboard import web
    from src.security import hash_password

    os.environ.update(
        {
            "DASHBOARD_USERS": f"operator:operator:{hash_password(OPERATOR_PASSWORD)},viewer:viewer:{hash_password(VIEWER_PASSWORD)}",
            "DASHBOARD_SESSION_SECRET": SESSION_SECRET,
            "AUTOMATION_API_KEYS": ADMIN_KEY,
            "DATABASE_URL": "postgresql://x:y@localhost/unused",
            "KB_DATABASE_URL": "postgresql://x:y@localhost/unused",
            "KB_ENABLED": "true" if kb_enabled else "false",
            "HEALTH_TIMEOUT_SECS": "2",
            "SECURITY_API_RATE_LIMIT": "500",
            "SECURITY_LOGIN_RATE_LIMIT": login_rate,
            "DEFAULT_PHONE_REGION": "PK",
            "SALES_AGENT_NAME": "Alex",
            "SALES_COMPANY_NAME": "Meridian",
        }
    )
    if auth_disabled:
        os.environ["DASHBOARD_AUTH_DISABLED"] = "true"
    else:
        os.environ.pop("DASHBOARD_AUTH_DISABLED", None)
    if registration is None:
        os.environ.pop("DASHBOARD_REGISTRATION_ENABLED", None)
    else:
        os.environ["DASHBOARD_REGISTRATION_ENABLED"] = registration
    web.collect = _fake_collect
    config = Config.from_env()
    store = store or FakeStore(clock=FakeClock(NOW))
    knowledge = FakeKnowledge()

    async def factory() -> Any:
        return store

    async def kb_factory() -> Any:
        return knowledge

    app = create_unified_app(config, store_factory=factory, bot_url=bot_url, deliver=False, knowledge_factory=kb_factory, embedder_factory=FakeEmbedder, proxy_bot=proxy_bot, stream=stream)
    return app, store, knowledge, config


def login(client: Any, user: str, password: str) -> Any:
    return client.post("/dashboard/login", data={"username": user, "password": password, "next": "/app/"}, follow_redirects=False, headers={"sec-fetch-site": "same-origin"})


# --- The checks ----------------------------------------------------------------------------------


def check_serving() -> None:
    print("\n=== the page, its files, its policy ===")
    from fastapi.testclient import TestClient

    app, store, knowledge, config = build()
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        check("/ sends the browser to the application", root.status_code == 302 and root.headers["location"] == "/app/")
        page = client.get("/app/")
        check("/app/ serves the page", page.status_code == 200 and page.headers["content-type"].startswith("text/html") and 'id="app"' in page.text)
        check("every application path serves the same page (the router is client-side)", client.get("/app/campaigns/3").status_code == 200 and 'id="app"' in client.get("/app/calls").text)
        script = client.get("/static/app.js")
        check("the script is served as JavaScript", script.status_code == 200 and "javascript" in script.headers["content-type"] and "/automation/api/v1" in script.text)
        check("and the stylesheet", client.get("/static/styles.css").status_code == 200)
        csp = page.headers.get("content-security-policy", "")
        check("the policy allows the script from a file and frames the bot's client only", "script-src 'self'" in csp and f"frame-src {BOT_URL}" in csp and "'unsafe-inline'" not in csp.split("style-src")[0], csp)
        check("and the other hardening headers are on", page.headers.get("x-frame-options") == "DENY" and page.headers.get("x-content-type-options") == "nosniff")
        check("/healthz names the application", client.get("/healthz").json()["role"] == "app")
        check("/readyz pings the store", client.get("/readyz").status_code == 200)
        check("the dashboard is mounted under /dashboard", client.get("/dashboard/api/ping").json()["ok"] is True)
        check("the automation API under /automation, its lifespan run (its store is open)", client.get("/automation/api/ping").json()["ok"] is True)
        check("the application's session route refuses a stranger and says where to sign in", client.get("/api/app/session").status_code == 401 and client.get("/api/app/session").json()["login"] == "/dashboard/login")
        check("so does the configuration", client.get("/api/app/config").status_code == 401)


def check_one_login() -> None:
    print("\n=== one login drives every part ===")
    from fastapi.testclient import TestClient

    app, store, knowledge, config = build()
    with TestClient(app) as client:
        wrong = login(client, "operator", "nope")
        check("a wrong password does not sign in", "aiva_session" not in client.cookies)
        signed = login(client, "operator", OPERATOR_PASSWORD)
        check("the dashboard's login signs in and sends the browser back to the application", signed.status_code == 303 and signed.headers["location"] == "/app/" and "aiva_session" in client.cookies, str(signed.status_code))
        session = client.get("/api/app/session").json()
        check("the session route knows who it is", session["name"] == "operator" and session["role"] == "operator" and "write" in session["permissions"] and session["via"] == "session", str(session))
        check("the dashboard's JSON answers the same cookie", client.get("/dashboard/api/me").json()["name"] == "operator" and client.get("/dashboard/api/dashboard").status_code == 200)
        status = client.get("/automation/api/v1/status")
        check("the automation API accepts the same cookie for a read", status.status_code == 200 and status.json()["principal"]["name"] == "operator", status.text[:120])
        refused = client.post("/automation/api/v1/prospects", json={"first_name": "No", "last_name": "Header", "phone": "0300 1111111"})
        check("a cookie write without the fetch header is refused (CSRF belt)", refused.status_code == 403 and refused.json()["error"]["code"] == "csrf", refused.text[:120])
        created = client.post("/automation/api/v1/prospects", json={"first_name": "Sara", "last_name": "Ali", "phone": "0300 1234567"}, headers={"X-Requested-With": "fetch"})
        check("with the header the write goes through", created.status_code == 201 and created.json()["prospect"]["phone_normalized"] == "+923001234567", created.text[:120])
        config_view = client.get("/api/app/config").json()
        check("the configuration view names the providers, the carrier, the sales defaults", config_view["providers"]["llm"].startswith("groq") and config_view["sales"]["agent_name"] == "Alex" and config_view["bot_url"] == BOT_URL and "telephony" in config_view)
        check("and no secret", ADMIN_KEY not in client.get("/api/app/config").text and SESSION_SECRET not in client.get("/api/app/config").text and "not-used-by-these-checks" not in client.get("/api/app/config").text)
        health = client.post("/api/app/health", headers={"X-Requested-With": "fetch"})
        check("the health check runs on demand for an operator", health.status_code == 200 and any(c["name"] == "database" for c in health.json()["components"]), health.text[:120])
        out = client.post("/dashboard/logout", follow_redirects=False, headers={"sec-fetch-site": "same-origin"})
        check("signing out ends the session for every part", out.status_code == 303 and client.get("/api/app/session").status_code == 401 and client.get("/automation/api/v1/status").status_code == 401)

        keyed = client.get("/automation/api/v1/status", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        check("an API key still works, without the header, as before", keyed.status_code == 200 and keyed.json()["principal"]["role"] == "admin", keyed.text[:120])
        check("and so does a keyed write", client.post("/automation/api/v1/prospects", json={"first_name": "Key", "last_name": "Holder", "phone": "0300 2222222"}, headers={"Authorization": f"Bearer {ADMIN_KEY}"}).status_code == 201)

    with TestClient(app) as client:
        login(client, "viewer", VIEWER_PASSWORD)
        check("a viewer signs in", client.get("/api/app/session").json()["role"] == "viewer")
        check("a viewer reads", client.get("/automation/api/v1/prospects", headers={"X-Requested-With": "fetch"}).status_code == 200)
        check("but the API refuses a viewer's write", client.post("/automation/api/v1/prospects", json={"first_name": "No", "last_name": "Way", "phone": "0300 3333333"}, headers={"X-Requested-With": "fetch"}).status_code == 403)
        check("and the application refuses a viewer's health check", client.post("/api/app/health", headers={"X-Requested-With": "fetch"}).status_code == 403)
        check("and a viewer sees numbers masked", "+923001234567" not in client.get("/automation/api/v1/prospects").text)

    app, store, knowledge, config = build(auth_disabled=True)
    with TestClient(app) as client:
        check("with the dashboard's login switched off, loopback is an operator everywhere (development)", client.get("/api/app/session").json()["via"] == "anonymous" and client.get("/automation/api/v1/status").status_code == 200)


def check_configuration_route() -> None:
    print("\n=== the campaign configuration route ===")
    from fastapi.testclient import TestClient

    app, store, knowledge, config = build()
    h = {"X-Requested-With": "fetch"}
    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        campaign = client.post("/automation/api/v1/campaigns", json={"name": "Configured", "description": "test"}, headers=h).json()["campaign"]
        saved = client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={"agent_name": "Sam", "company_name": "Northwind", "offer": "fuel monitoring", "value_points": ["saves 12%", " ", "no hardware"], "pacing_secs": 30}, headers=h)
        check("the agent profile and the pacing are written onto the campaign", saved.status_code == 200 and saved.json()["campaign"]["configuration"]["agent_name"] == "Sam" and saved.json()["campaign"]["configuration"]["value_points"] == ["saves 12%", "no hardware"] and saved.json()["campaign"]["configuration"]["pacing_secs"] == 30, saved.text[:200])
        check("the keys are the ones the brief reads", set(saved.json()["changed"]) == {"agent_name", "company_name", "offer", "value_points", "pacing_secs"})
        cleared = client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={"agent_name": "", "pacing_secs": 0}, headers=h).json()["campaign"]["configuration"]
        check("an empty value clears the key so the environment's default applies again", "agent_name" not in cleared and "pacing_secs" not in cleared and cleared["company_name"] == "Northwind", str(cleared))
        check("nothing to change is 422", client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={}, headers=h).status_code == 422)
        check("an unknown key is refused", client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={"voice": "x"}, headers=h).status_code == 422)
        check("an unknown campaign is 404", client.put("/automation/api/v1/campaigns/999/configuration", json={"agent_name": "x"}, headers=h).status_code == 404)
        # Phase 28: a campaign's own company facts.
        facts = client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={"company_description": "Northwind tracks fleets.", "services": ["tracking", " ", "routing"]}, headers=h)
        check("the company facts are written onto the campaign", facts.status_code == 200 and facts.json()["campaign"]["configuration"]["company_description"] == "Northwind tracks fleets." and facts.json()["campaign"]["configuration"]["services"] == ["tracking", "routing"], facts.text[:200])
        cleared_facts = client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={"company_description": "", "services": []}, headers=h).json()["campaign"]["configuration"]
        check("and an empty value clears them", "company_description" not in cleared_facts and "services" not in cleared_facts, str(cleared_facts))

        from src.conversation import CampaignBrief

        brief = CampaignBrief.from_configuration(cleared, defaults=CampaignBrief(agent_name="Alex", company_name="Meridian", offer="default offer"))
        check("the brief the bot builds reads the saved profile over the defaults", brief.company_name == "Northwind" and brief.offer == "fuel monitoring" and brief.agent_name == "Alex" and brief.value_points == ["saves 12%", "no hardware"])
        isolated = CampaignBrief.from_configuration(facts.json()["campaign"]["configuration"], defaults=CampaignBrief(agent_name="Alex", company_name="Meridian", company_description="Meridian builds fleet software.", services=["tracking"]))
        check("a campaign for another company gets its own facts, not the deployment's", isolated.company_description == "Northwind tracks fleets." and isolated.services == ["tracking", "routing"] and "Meridian" not in isolated.render())
        check("the configuration view carries the deployment's facts", "company_description" in client.get("/api/app/config", headers=h).json()["sales"])


async def check_whole_flow() -> None:
    print("\n=== the flow: CSV -> preview -> import -> campaign -> start -> the scheduler dials with the contact's ids ===")
    from fastapi.testclient import TestClient
    from test_worker import NOW, FakeClock, ScriptedCarrier

    from src.campaigns import CampaignDialer, CampaignService, CampaignWorker
    from src.reliability import CallingWindow, CampaignGuards, PacingLimiter
    from src.telephony import CallStatus

    app, store, knowledge, config = build()
    h = {"X-Requested-With": "fetch"}
    csv = "First Name,Surname,Mobile,Company,Notes\nAyesha,Khan,+92 300 1000001,Ravi Logistics,fleet of 40\nBilal,Raza,0300 1000002,Raza Freight,\nNobody,Here,12,,\n"
    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        before = len(store.prospects)
        preview = client.post("/automation/api/v1/prospects/import?dry_run=true", content=csv, headers={**h, "Content-Type": "text/csv"})
        body = preview.json()
        check("a dry run validates and reports without writing", preview.status_code == 200 and body["dry_run"] is True and body["rejected_count"] == 1 and body["rejected"][0]["line"] == 4 and len(store.prospects) == before, preview.text[:200])
        check("the preview says which columns were mapped", body["mapping"]["columns"]["phone"] == "Mobile" and "Notes" in body["mapping"]["extras"])
        campaign = client.post("/automation/api/v1/campaigns", json={"name": "From the app"}, headers=h).json()["campaign"]
        check("the campaign is created as a draft", campaign["status"] == "DRAFT")
        client.put(f"/automation/api/v1/campaigns/{campaign['id']}/configuration", json={"agent_name": "Sam", "company_name": "Northwind"}, headers=h)
        imported = client.post(f"/automation/api/v1/prospects/import?campaign={campaign['id']}", content=csv, headers={**h, "Content-Type": "text/csv"}).json()
        check("the confirmed import writes the valid rows into the campaign and dials nothing", imported["created"] == 2 and imported["added_to_campaign"] == 2 and len(store.prospects) == before + 2 and not store.attempts, str(imported)[:200])
        started = client.post(f"/automation/api/v1/campaigns/{campaign['id']}/start", headers=h)
        check("starting the campaign marks it running", started.status_code == 200 and started.json()["campaign"]["status"] == "ACTIVE", started.text[:120])
        check("the campaigns list shows it with its counts", any(c["id"] == campaign["id"] and c["counts"]["pending"] == 2 for c in client.get("/automation/api/v1/campaigns").json()["campaigns"]))

    # The real scheduler over the same rows — what `campaign.py run` (or `app.py --with-scheduler`) does.
    clock = FakeClock(NOW)
    service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
    carrier = ScriptedCarrier(clock, script=[CallStatus.ANSWERED, CallStatus.COMPLETED])
    guards = CampaignGuards(window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC", clock=clock), pacing=PacingLimiter(0.0, clock=clock.monotonic), max_concurrent=1)
    dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://bot.example.test", guards=guards)

    async def no_sleep(_s: float) -> None:
        clock.advance(1)

    # auto_complete off so the campaign stays ACTIVE for the state checks below.
    worker = CampaignWorker(service, dialer, guards=guards, poll_secs=1.0, idle_secs=5.0, recovery_interval_secs=3600.0, drain_secs=60.0, report_secs=60.0, auto_complete=False, clock=clock, sleep=no_sleep)
    await worker.start()
    await worker.tick()
    check("the scheduler picks the campaign up and dials its first contact", len(carrier.requests) == 1 and carrier.requests[0].to_number == "+923001000001", str([r.to_number for r in carrier.requests]))
    params = carrier.requests[0].parameters
    attempt = next(iter(store.attempts.values()))
    check("the handshake carries the contact's ids, so the bot personalises the call from the rows", params.get("prospect_id") == str(attempt.prospect_id) and params.get("campaign_id") == str(campaign["id"]) and params.get("call_attempt_id") == str(attempt.id) and params.get("trace_id"), str(params))

    from src.campaigns.briefing import CampaignProspectSource
    from src.conversation import CallIdentifiers, CampaignBrief

    brief = await CampaignProspectSource(service).load(CallIdentifiers(prospect_id=attempt.prospect_id, campaign_id=campaign["id"], call_attempt_id=attempt.id), CampaignBrief(agent_name="Alex", company_name="Meridian"))
    check("the brief the agent gets names the contact, the company, the CSV note and the campaign's agent profile", brief is not None and brief.prospect.first_name == "Ayesha" and brief.prospect.company == "Ravi Logistics" and any("fleet of 40" in n for n in brief.prospect.notes) and brief.campaign.agent_name == "Sam" and brief.campaign.company_name == "Northwind", brief.describe() if brief else "none")
    carrier.end_call(attempt.telephony_call_id)
    await worker.tick()
    check("the call is followed to its end and the row says COMPLETED", store.attempts[attempt.id].status.value == "COMPLETED")
    check("and the same tick dials the next contact, one at a time", len(carrier.requests) == 2 and carrier.requests[1].to_number == "+923001000002", str([r.to_number for r in carrier.requests]))
    await worker.finish()

    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        calls = client.get(f"/automation/api/v1/calls?campaign_id={campaign['id']}").json()
        check("the API lists the campaign's calls", calls["count"] == 2 and sorted(c["status"] for c in calls["calls"]) == ["COMPLETED", "QUEUED"], str([c["status"] for c in calls["calls"]]))
        moves = [client.post(f"/automation/api/v1/campaigns/{campaign['id']}/{action}", headers=h) for action in ("pause", "resume")]
        refused = client.post(f"/automation/api/v1/campaigns/{campaign['id']}/complete", headers=h)
        check("stopping needs the manage permission: an operator is refused (Phase 18's rule, unchanged)", refused.status_code == 403, refused.text[:120])
        moves.append(client.post(f"/automation/api/v1/campaigns/{campaign['id']}/complete", headers={"Authorization": f"Bearer {ADMIN_KEY}"}))
        states = [m.json().get("campaign", {}).get("status") for m in moves]
        check("pause, resume and stop (by an admin) move the campaign through its states", states == ["PAUSED", "ACTIVE", "COMPLETED"], " | ".join(m.text[:120] for m in moves))
        check("a stopped campaign cannot be started again", client.post(f"/automation/api/v1/campaigns/{campaign['id']}/start", headers=h).status_code == 409)


def check_knowledge() -> None:
    print("\n=== the knowledge base ===")
    from fastapi.testclient import TestClient

    app, store, knowledge, config = build()
    h = {"X-Requested-With": "fetch"}
    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        empty = client.get("/api/app/knowledge").json()
        check("an empty knowledge base lists nothing and says what it accepts", empty["counts"] == {"documents": 0, "chunks": 0} and ".pdf" in empty["supported"])
        text = ("Meridian pricing. The fuel monitoring plan costs 12 dollars per vehicle per month. " * 40).encode("utf-8")
        uploaded = client.post("/api/app/knowledge/documents", files={"file": ("pricing.txt", text, "text/plain")}, headers=h)
        check("a text file is extracted, chunked, embedded and stored", uploaded.status_code == 201 and uploaded.json()["chunks"] >= 1 and uploaded.json()["source"] == "pricing.txt", uploaded.text[:160])
        listed = client.get("/api/app/knowledge").json()
        check("and listed with its passages", listed["counts"]["documents"] == 1 and listed["documents"][0]["chunk_count"] == uploaded.json()["chunks"])
        hits = client.post("/api/app/knowledge/search", json={"query": "pricing per vehicle"}, headers=h).json()
        check("a question finds the passage", hits["matches"] and "12 dollars" in hits["matches"][0]["content"], str(hits)[:160])
        check("an unsupported file is refused", client.post("/api/app/knowledge/documents", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=h).status_code == 422)
        check("removing it works once", client.delete("/api/app/knowledge/documents/pricing.txt", headers=h).status_code == 200 and client.delete("/api/app/knowledge/documents/pricing.txt", headers=h).status_code == 404)
        actions = [row.action for row in store.audit_entries]
        check("uploads and removals are audited", "knowledge.document_added" in actions and "knowledge.document_removed" in actions, str(actions[-3:]))
    with TestClient(app) as client:
        login(client, "viewer", VIEWER_PASSWORD)
        check("a viewer may search but not upload", client.post("/api/app/knowledge/search", json={"query": "x"}, headers=h).status_code == 200 and client.post("/api/app/knowledge/documents", files={"file": ("a.txt", b"hello", "text/plain")}, headers=h).status_code == 403)
    app, store, knowledge, config = build(kb_enabled=False)
    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        off = client.get("/api/app/knowledge")
        check("with KB_ENABLED=false the page is told so, plainly", off.status_code == 503 and "KB_ENABLED" in off.json()["error"])


def check_registration() -> None:
    print("\n=== the Register page's route and the sign-up approval (Phase 27) ===")
    from fastapi.testclient import TestClient

    from src.security import ROLE_PERMISSIONS, Role

    FETCH = {"X-Requested-With": "fetch"}
    SIGNUP = {"password": "password-123", "confirm_password": "password-123"}
    app, store, knowledge, config = build()
    with TestClient(app) as client:
        info = client.get("/api/app/register")
        check("the page asks whether sign-up is open, and which roles need approval", info.status_code == 200 and info.json()["enabled"] is True and info.json()["roles"] == ["viewer", "operator", "admin"] and info.json()["approval_roles"] == ["operator", "admin"], info.text[:200])

        # Invalid input.
        bad = client.post("/api/app/register", json={"name": "bad name!", "email": "not-an-email", "role": "king", "password": "short", "confirm_password": "other"}, headers=FETCH)
        fields = bad.json().get("fields", {})
        check("bad input is a 422 that names every field, the role included", bad.status_code == 422 and set(fields) == {"name", "email", "role", "password", "confirm_password"}, bad.text[:240])
        mismatch = client.post("/api/app/register", json={"name": "dana", "email": "dana@example.com", "password": "password-123", "confirm_password": "password-124"}, headers=FETCH)
        check("a confirmation that differs is refused on that field alone", mismatch.status_code == 422 and list(mismatch.json()["fields"]) == ["confirm_password"], mismatch.text[:200])
        check("and nothing was written", not store.dashboard_users)

        # 1. Viewer sign-up → an active viewer.
        made = client.post("/api/app/register", json={"name": "dana", "email": "Dana@Example.com", "role": "viewer", **SIGNUP}, headers=FETCH)
        check("1. a viewer sign-up is a 201, active at once", made.status_code == 201 and made.json()["role"] == "viewer" and made.json()["status"] == "active", made.text[:200])
        no_role = client.post("/api/app/register", json={"name": "dee", "email": "dee@example.com", **SIGNUP}, headers=FETCH)
        check("   a sign-up that names no role is a viewer (the old behaviour)", no_role.status_code == 201 and no_role.json()["role"] == "viewer" and no_role.json()["status"] == "active", no_role.text[:200])
        row = store.dashboard_users[1]
        check("   the row carries a hash, never the password", row.password_hash.startswith("scrypt$") and "password-123" not in row.password_hash)
        check("   the sign-up is on the audit log", any(e.action == "auth.registered" and e.actor == "dana" and e.outcome == "ok" for e in store.audit_entries))

        # 2. Operator sign-up → a pending request.
        op = client.post("/api/app/register", json={"name": "oscar", "email": "oscar@example.com", "role": "operator", **SIGNUP}, headers=FETCH)
        check("2. an operator sign-up is a 201 whose status is pending", op.status_code == 201 and op.json()["role"] == "operator" and op.json()["status"] == "pending", op.text[:200])
        check("   the row is pending, the role recorded as requested", store.dashboard_users[3].status == "pending" and store.dashboard_users[3].role == "operator")
        check("   and the audit log says a request is awaiting approval", any(e.action == "auth.registered" and e.actor == "oscar" and "awaiting approval" in e.outcome for e in store.audit_entries))

        # 3. Admin sign-up → a pending request.
        ad = client.post("/api/app/register", json={"name": "amy", "email": "amy@example.com", "role": "ADMIN", **SIGNUP}, headers=FETCH)
        check("3. an admin sign-up is a 201 whose status is pending", ad.status_code == 201 and ad.json()["role"] == "admin" and ad.json()["status"] == "pending", ad.text[:200])
        check("   the row is pending", store.dashboard_users[4].status == "pending" and store.dashboard_users[4].role == "admin")

        # Duplicates, in any case; a configured name cannot be taken.
        dupe_email = client.post("/api/app/register", json={"name": "dana2", "email": "dana@example.com", **SIGNUP}, headers=FETCH)
        check("the same email again, in another case, is a 409 naming the email", dupe_email.status_code == 409 and dupe_email.json()["field"] == "email", dupe_email.text[:200])
        dupe_name = client.post("/api/app/register", json={"name": "OSCAR", "email": "other@example.com", **SIGNUP}, headers=FETCH)
        check("the same name again — a pending one too — is a 409 naming the name", dupe_name.status_code == 409 and dupe_name.json()["field"] == "name", dupe_name.text[:200])
        taken = client.post("/api/app/register", json={"name": "operator", "email": "operator@example.com", **SIGNUP}, headers=FETCH)
        check("a name configured in DASHBOARD_USERS cannot be taken by a sign-up", taken.status_code == 409 and taken.json()["field"] == "name", taken.text[:200])
        check("four rows, still", len(store.dashboard_users) == 4)

        # 4. and 5. A pending request cannot sign in — so it cannot use anything.
        wrong = login(client, "oscar", "password-124")
        check("4. the pending operator's wrong password is a 401 with no hint", wrong.status_code == 401 and "aiva_session" not in client.cookies and "approval" not in wrong.text)
        pending = login(client, "oscar", "password-123")
        check("   the pending operator's right password is a 403 that says the account awaits approval, and no cookie", pending.status_code == 403 and pending.headers.get("x-aiva-login") == "pending" and "approval" in pending.text and "aiva_session" not in client.cookies, str(pending.status_code))
        check("   so a pending operator cannot read or write anything", client.get("/api/app/session").status_code == 401 and client.post("/automation/api/v1/prospects", json={"first_name": "No", "last_name": "Way", "phone": "0300 3333333"}, headers=FETCH).status_code == 401)
        check("   and the refusal is on the audit log", any(e.action == "auth.login_pending" and e.actor == "oscar" for e in store.audit_entries))
        pending_admin = login(client, "amy", "password-123")
        check("5. the pending admin cannot sign in either", pending_admin.status_code == 403 and pending_admin.headers.get("x-aiva-login") == "pending" and "aiva_session" not in client.cookies)
        check("   so a pending admin cannot use anything an admin can", client.get("/api/app/users/pending").status_code == 401 and client.get("/automation/api/v1/audit").status_code == 401)

        # The viewer from step 1 signs in, as before.
        signed = login(client, "dana", "password-123")
        session = client.get("/api/app/session")
        check("the viewer signs in through the existing login, by name", signed.status_code == 303 and session.status_code == 200 and session.json()["name"] == "dana" and session.json()["role"] == "viewer", session.text[:200])
        check("a viewer's session: reads yes, writes no", client.get("/automation/api/v1/prospects", headers=FETCH).status_code == 200 and client.post("/automation/api/v1/prospects", json={"first_name": "No", "last_name": "Way", "phone": "0300 3333333"}, headers=FETCH).status_code == 403)
        # 9. A viewer cannot approve.
        check("9. a viewer cannot list the requests", client.get("/api/app/users/pending").status_code == 403)
        check("   nor approve one", client.post("/api/app/users/3/approve", headers=FETCH).status_code == 403 and store.dashboard_users[3].status == "pending")
        check("   nor reject one", client.post("/api/app/users/4/reject", headers=FETCH).status_code == 403 and 4 in store.dashboard_users)
        client.post("/dashboard/logout", follow_redirects=False, headers={"sec-fetch-site": "same-origin"})

    # 8. An operator cannot approve.
    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        check("8. the configured operator still signs in", client.get("/api/app/session").json()["role"] == "operator")
        check("   but cannot list the requests", client.get("/api/app/users/pending").status_code == 403)
        check("   nor approve one", client.post("/api/app/users/3/approve", headers=FETCH).status_code == 403 and store.dashboard_users[3].status == "pending")
        check("   nor approve the admin request", client.post("/api/app/users/4/approve", headers=FETCH).status_code == 403 and store.dashboard_users[4].status == "pending")

    # 10. Direct API manipulation: the role in the payload never grants anything.
    with TestClient(app) as client:
        forged = client.post("/api/app/register", json={"name": "mallory", "email": "mallory@example.com", "role": "admin", "status": "active", **SIGNUP}, headers=FETCH)
        check("10. a payload that says role=admin and status=active gets a pending request, nothing more", forged.status_code == 201 and forged.json()["status"] == "pending" and store.dashboard_users[5].status == "pending", forged.text[:200])
        check("    which cannot sign in", login(client, "mallory", "password-123").status_code == 403 and "aiva_session" not in client.cookies)
        check("    and cannot approve itself (no session)", client.post("/api/app/users/5/approve", headers=FETCH).status_code == 401 and store.dashboard_users[5].status == "pending")
        check("    a stranger cannot list or decide requests", client.get("/api/app/users/pending").status_code == 401 and client.post("/api/app/users/3/approve", headers=FETCH).status_code == 401 and client.post("/api/app/users/3/reject", headers=FETCH).status_code == 401)
        keyed = client.post("/api/app/users/3/approve", headers={**FETCH, "Authorization": f"Bearer {ADMIN_KEY}", "Origin": "https://evil.example"})
        check("    a cross-site approval is refused even with an admin credential", keyed.status_code == 403 and store.dashboard_users[3].status == "pending", keyed.text[:120])
        check("    a cross-site sign-up is refused", client.post("/api/app/register", json={"name": "evil", "email": "evil@example.com", **SIGNUP}, headers={**FETCH, "Origin": "https://evil.example"}).status_code == 403)

    # 6. and 7. An admin approves — here the admin API key (the same `manage` permission a session admin holds).
    with TestClient(app) as client:
        ADMIN = {**FETCH, "Authorization": f"Bearer {ADMIN_KEY}"}
        listed = client.get("/api/app/users/pending", headers=ADMIN)
        names = [u["name"] for u in listed.json().get("users", [])]
        check("an admin sees the pending requests, oldest first, without hashes", listed.status_code == 200 and names == ["oscar", "amy", "mallory"] and "scrypt" not in listed.text and all(u["status"] == "pending" for u in listed.json()["users"]), listed.text[:200])
        approved = client.post("/api/app/users/3/approve", headers=ADMIN)
        check("6. the admin approves the operator request: active, as operator", approved.status_code == 200 and approved.json()["decision"] == "approved" and approved.json()["user"]["status"] == "active" and approved.json()["user"]["role"] == "operator" and store.dashboard_users[3].status == "active", approved.text[:200])
        check("   the decision is recorded with who made it", (store.dashboard_users[3].decided_by or "").startswith("admin-key") and any(e.action == "auth.signup_approved" and e.target_id == "3" for e in store.audit_entries), str(store.dashboard_users[3].decided_by))
        check("   approving again is a 404 (nothing pending)", client.post("/api/app/users/3/approve", headers=ADMIN).status_code == 404)
        approved_admin = client.post("/api/app/users/4/approve", headers=ADMIN)
        check("7. the admin approves the admin request: active, as admin", approved_admin.status_code == 200 and approved_admin.json()["user"]["role"] == "admin" and store.dashboard_users[4].status == "active", approved_admin.text[:200])
        rejected = client.post("/api/app/users/5/reject", headers=ADMIN)
        check("   the forged request is rejected: the row is gone, the audit log keeps it", rejected.status_code == 200 and rejected.json()["decision"] == "rejected" and 5 not in store.dashboard_users and any(e.action == "auth.signup_rejected" and e.target_id == "5" for e in store.audit_entries), rejected.text[:200])
        check("   and the name is free again", client.post("/api/app/register", json={"name": "mallory", "email": "mallory@example.com", **SIGNUP}, headers=FETCH).status_code == 201)
        check("   nothing left pending", client.get("/api/app/users/pending", headers=ADMIN).json()["users"] == [])
        check("   a rejected-then-re-registered viewer is active", store.dashboard_users[6].status == "active" and store.dashboard_users[6].role == "viewer")

    # After approval the operator and the admin sign in as what they asked for.
    with TestClient(app) as client:
        signed = login(client, "oscar", "password-123")
        session = client.get("/api/app/session")
        check("the approved operator signs in as an operator", signed.status_code == 303 and session.json()["role"] == "operator" and "write" in session.json()["permissions"], session.text[:200])
        check("and writes", client.post("/automation/api/v1/prospects", json={"first_name": "Now", "last_name": "Allowed", "phone": "0300 5555555"}, headers=FETCH).status_code == 201)
        client.post("/dashboard/logout", follow_redirects=False, headers={"sec-fetch-site": "same-origin"})
    with TestClient(app) as client:
        login(client, "amy@example.com", "password-123")
        session = client.get("/api/app/session")
        check("the approved admin signs in (by email) as an admin", session.status_code == 200 and session.json()["role"] == "admin" and "manage" in session.json()["permissions"], session.text[:200])
        check("and can now decide requests herself (an approved admin holds the same manage permission)", client.get("/api/app/users/pending", headers=FETCH).status_code == 200)
        check("a session admin's approval without the fetch header (a cross-site form) is refused", client.post("/api/app/users/3/approve", headers={"Origin": "https://evil.example"}).status_code == 403)

    # A decision made on another process is what the login sees.
    app2, _s2, _k, config2 = build(store=store)
    with TestClient(app2) as client:
        check("a fresh process does not know the sign-ups at start", config2.security.users.get("oscar") is None)
        login(client, "oscar", "password-123")
        check("but finds the approved operator at login", client.get("/api/app/session").json()["role"] == "operator")
    app3, store3, _k, _c = build()
    with TestClient(app3) as client:
        client.post("/api/app/register", json={"name": "pat", "email": "pat@example.com", "role": "operator", **SIGNUP}, headers=FETCH)
        check("a request pending on this process", login(client, "pat", "password-123").status_code == 403)
        decided: dict[str, Any] = {}
        import threading

        thread = threading.Thread(target=lambda: decided.setdefault("row", asyncio.run(store3.decide_dashboard_user(1, approve=True, decided_by="elsewhere"))))
        thread.start()
        thread.join()
        row = decided.get("row")
        check("approved on another process (the table changed under this one)", row is not None and row.status == "active")
        check("signs in here without a restart: the login re-reads the row", login(client, "pat", "password-123").status_code == 303 and client.get("/api/app/session").json()["role"] == "operator")

    # 11. Existing users, login and roles, unchanged.
    with TestClient(app) as client:
        login(client, "operator", OPERATOR_PASSWORD)
        check("11. the configured operator still signs in", client.get("/api/app/session").json()["role"] == "operator")
        check("    and still writes", client.post("/automation/api/v1/prospects", json={"first_name": "Still", "last_name": "Here", "phone": "0300 4444444"}, headers=FETCH).status_code == 201)
    with TestClient(app) as client:
        login(client, "viewer", VIEWER_PASSWORD)
        check("    the configured viewer still signs in, and still cannot write", client.get("/api/app/session").json()["name"] == "viewer" and client.post("/automation/api/v1/prospects", json={"first_name": "No", "last_name": "Way", "phone": "0300 6666666"}, headers=FETCH).status_code == 403)
        check("    and a stranger still does not", login(client, "nobody", "password-123").status_code == 401)
        check("    the admin key still reads the audit log", client.get("/automation/api/v1/audit", headers={"Authorization": f"Bearer {ADMIN_KEY}"}).status_code == 200)
        check("    the role permissions are what they were", sorted(p.value for p in ROLE_PERMISSIONS[Role.VIEWER]) == ["read"] and sorted(p.value for p in ROLE_PERMISSIONS[Role.OPERATOR]) == ["read", "read_pii", "write"] and sorted(p.value for p in ROLE_PERMISSIONS[Role.ADMIN]) == ["manage", "read", "read_pii", "write"])

    # Sign-up closed by configuration; the login off.
    app4, _s, _k, _c = build(registration="false")
    with TestClient(app4) as client:
        check("DASHBOARD_REGISTRATION_ENABLED=false: the page is told it is closed", client.get("/api/app/register").json()["enabled"] is False)
        check("and the route refuses", client.post("/api/app/register", json={"name": "fay", "email": "fay@example.com", **SIGNUP}, headers=FETCH).status_code == 403)
    app5, _s, _k, _c = build(auth_disabled=True)
    with TestClient(app5) as client:
        check("with the login off there is nothing to register for", client.get("/api/app/register").json()["enabled"] is False and client.post("/api/app/register", json={"name": "fay", "email": "fay@example.com", **SIGNUP}, headers=FETCH).status_code == 403)

    # Sign-ups are limited per address like logins.
    app6, _s, _k, _c = build(login_rate="3")
    with TestClient(app6) as client:
        codes = [client.post("/api/app/register", json={"name": "x", "email": "x", "password": "x", "confirm_password": "y"}, headers=FETCH).status_code for _ in range(4)]
        check("an address that keeps trying is told to wait (429 with Retry-After)", codes == [422, 422, 422, 429], str(codes))

    # The page itself carries the form and the approval card.
    with TestClient(app) as client:
        script = client.get("/static/app.js").text
        check("the page has a Register view with the role choice, and the admin's sign-up requests card", "renderRegister" in script and 'name="role"' in script and 'href="#/register"' in script and "users/pending" in script and "/approve" in script and "/reject" in script)


def check_boundary() -> None:
    print("\n=== the boundary ===")
    offenders = []
    for item in ("bot.py", "src/conversation", "src/telephony", "src/campaigns", "src/actions", "src/reliability", "src/dashboard", "src/automation"):
        path = SERVER / item
        for file in [path] if path.is_file() else path.rglob("*.py"):
            text = file.read_text(encoding="utf-8")
            if re.search(r"^\s*(from|import)\s+(src\.app|\.\.app|\.app)\b", text, re.M):
                offenders.append(str(file.relative_to(SERVER)))
    check("nothing on the call path, in the dashboard or in the API imports the application package", not offenders, ", ".join(offenders))
    app_text = (SERVER / "src" / "app" / "server.py").read_text(encoding="utf-8")
    check("the application composes the existing factories rather than re-declaring their routes", "create_app(" in app_text and "create_automation_app(" in app_text and "@router.post(\"/prospects" not in app_text)
    js = (SERVER / "web" / "app.js").read_text(encoding="utf-8")
    check("the browser talks only to its own origin", "http://" not in js.replace("http://127.0.0.1:7860", "") and "https://" not in js)
    check("every page the phase asks for has a route", all(f"#\\/{name}" in js for name in ("dashboard", "campaigns", "contacts", "calls", "live", "knowledge", "analytics", "settings")) and "campaigns\\/new" in js and "contacts\\/import" in js)
    check("importing never starts a call from the browser", "start" not in js.split("async function pageImport")[1].split("async function")[0].lower().replace("started", "").replace("starting", "").replace("start it", "").replace("you start", ""))


def check_bot_proxy() -> None:
    """Phase 34: with APP_PROXY_BOT the application forwards the bot's paths, so one tunnel serves both."""
    print("\n=== the bot behind the application (APP_PROXY_BOT) ===")
    import socket
    import threading
    import time as _time

    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, RedirectResponse, Response
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    # A stand-in for the bot's runner: the client page, the carrier's webhook, the offer, a websocket that echoes.
    upstream = FastAPI()
    seen: dict[str, Any] = {}

    @upstream.get("/client")
    async def client_redirect() -> RedirectResponse:
        return RedirectResponse("/client/", status_code=307)

    @upstream.get("/client/")
    async def client_page(request: Request) -> HTMLResponse:
        seen["headers"] = dict(request.headers)
        return HTMLResponse("<!doctype html><title>bot</title>", headers={"x-upstream": "bot"})

    @upstream.post("/")
    async def start_call() -> Response:
        return Response(content="<Response/>", media_type="application/xml")

    @upstream.post("/api/offer")
    async def offer(request: Request) -> dict[str, Any]:
        return {"echo": await request.json()}

    @upstream.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        seen["ws_headers"] = dict(websocket.headers)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await websocket.send_bytes(b"got:" + message["bytes"])
            elif message.get("text") is not None:
                await websocket.send_text("got:" + message["text"])

    def free_port() -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    port, closed_port = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(upstream, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.monotonic() + 10
    while not server.started and _time.monotonic() < deadline:
        _time.sleep(0.05)
    check("the stand-in bot is up", server.started)

    h = {"X-Requested-With": "fetch"}
    try:
        app, store, knowledge, config = build(bot_url=f"http://127.0.0.1:{port}/", proxy_bot=True)
        with TestClient(app) as client:
            page = client.get("/client/")
            check("GET /client/ reaches the bot's page through the application", page.status_code == 200 and "<title>bot</title>" in page.text and page.headers.get("x-upstream") == "bot", page.text[:120])
            check("the forwarded page may be framed by this origin and may use the microphone", page.headers.get("x-frame-options") == "SAMEORIGIN" and "frame-ancestors 'self'" in page.headers.get("content-security-policy", "") and "microphone=(self)" in page.headers.get("permissions-policy", ""), str(dict(page.headers)))
            check("the bot is told who asked", seen.get("headers", {}).get("x-forwarded-host") == "testserver" and "x-forwarded-for" in seen.get("headers", {}), str(seen.get("headers")))
            redirect = client.get("/client", follow_redirects=False)
            check("a redirect from the bot stays on this origin", redirect.status_code == 307 and redirect.headers["location"] == "/client/", str(dict(redirect.headers)))
            xml = client.post("/", data={"CallSid": "CA1"})
            check("POST / (the carrier's webhook) goes to the bot while GET / still opens the application", xml.status_code == 200 and "<Response/>" in xml.text and client.get("/", follow_redirects=False).headers.get("location") == "/app/", xml.text[:120])
            offer_reply = client.post("/api/offer", json={"sdp": "v=0", "type": "offer"})
            check("POST /api/offer is forwarded with its body", offer_reply.status_code == 200 and offer_reply.json() == {"echo": {"sdp": "v=0", "type": "offer"}}, offer_reply.text[:120])
            check("the application's own routes are matched first", client.get("/app/").status_code == 200 and client.get("/healthz").json()["role"] == "app" and client.get("/api/app/session").status_code == 401 and client.get("/dashboard/api/ping").json()["ok"] is True)
            with client.websocket_connect("/ws?token=abc") as socket_:
                socket_.send_text("hello")
                check("a WebSocket is relayed frame for frame: text", socket_.receive_text() == "got:hello")
                socket_.send_bytes(b"\x00\x01")
                check("and binary", socket_.receive_bytes() == b"got:\x00\x01")
            check("the relayed handshake says who asked", seen.get("ws_headers", {}).get("x-forwarded-host") == "testserver", str(seen.get("ws_headers")))
            app_page = client.get("/app/")
            csp = app_page.headers.get("content-security-policy", "")
            check("the page's policy frames 'self' and delegates the microphone to the frame", "frame-src 'self'" in csp and "microphone=(self)" in app_page.headers.get("permissions-policy", ""), csp)
            login(client, "operator", OPERATOR_PASSWORD)
            view = client.get("/api/app/config", headers=h).json()
            check("the configuration says the bot is behind the application", view["bot_proxied"] is True and view["bot_url"] == f"http://127.0.0.1:{port}/")

        app, store, knowledge, config = build(bot_url=f"http://127.0.0.1:{closed_port}", proxy_bot=True)
        with TestClient(app) as client:
            down = client.get("/client/")
            check("a bot that is not running is a 502 that says so, not a hang", down.status_code == 502 and down.json()["code"] == "bot_unreachable", down.text[:160])
            refused = False
            try:
                with client.websocket_connect("/ws"):
                    pass
            except WebSocketDisconnect:
                refused = True
            check("and a WebSocket to it is refused", refused)

        app, store, knowledge, config = build()
        with TestClient(app) as client:
            check("without APP_PROXY_BOT nothing is forwarded: the bot's paths are this application's 404", client.get("/client/").status_code == 404 and client.post("/", data={"a": "b"}).status_code == 405)

        # Phase 35: the event stream can be switched off (a serverless function must not hold a response open).
        app, store, knowledge, config = build(stream=False)
        with TestClient(app) as client:
            login(client, "operator", OPERATOR_PASSWORD)
            check("with APP_STREAM_ENABLED=false the event stream answers 204 at once, so the page polls", client.get("/api/app/stream", headers=h).status_code == 204)
            check("and a stranger is still refused first", TestClient(build(stream=False)[0]).get("/api/app/stream").status_code == 401)
            page = client.get("/app/")
            check("and the page delegates the microphone to the bot's own origin", f'microphone=("{BOT_URL}")' in page.headers.get("permissions-policy", ""), page.headers.get("permissions-policy", ""))
            login(client, "operator", OPERATOR_PASSWORD)
            view_after_login = client.get("/api/app/config", headers=h).json()
            check("and the configuration says so", view_after_login["bot_proxied"] is False and view_after_login["bot_url"] == BOT_URL)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def main() -> int:
    print("Unified application checks — the page, one login for every part, the CSRF belt, the roles, the configuration route, the whole flow, the knowledge base, the Register page, the boundary.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_serving()
        check_one_login()
        check_configuration_route()
        await check_whole_flow()
        check_knowledge()
        check_registration()
        check_bot_proxy()
        check_boundary()
    finally:
        logger.remove(handler)
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
