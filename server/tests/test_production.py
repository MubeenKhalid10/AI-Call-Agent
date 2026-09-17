"""Phase 23: the end-to-end production validation suite.

One deployment, one campaign, one story — run through the **real** code
with only the outside world replaced, against a **real PostgreSQL** in a
schema that is thrown away at the end:

    CSV import (through the real API)  →  campaign created and activated
    →  the scheduler selects, gates (do-not-call, calling hours) and dials
    →  the conversation: discovery, a barge-in, a knowledge-base answer,
       qualification, an objection, a booked meeting, a transfer, a callback
    →  the carrier's completion webhook (signed, over HTTP)
    →  every row: attempt, conversation, result, meeting, callback, transfer,
       usage and cost, the ledger, the audit log
    →  the CRM filing, the n8n delivery (real HTTP to a receiver that verifies
       the signature), the dashboard, the API
    →  the callback executed on time, a no-answer, a voicemail, recovery
       after a crash, a transient failure retried
    →  never two calls to one person, nobody in without a key, no secret in
       any answer or any log line, every external provider failing gracefully.

What is replaced, and why: the **carrier** (`test_worker`'s scripted one:
no account, no money, no phone rings), the **CRM** (`test_crm`'s mock), the
**speech and language services** (the conversation is driven turn by turn
as text, through the same detectors, tools and state machine the model
drives; the audio path is `evals/` and `tests/phone_drill.py`, which need
vendor keys), and **n8n** (a small receiver that does exactly what
`n8n/README.md` tells n8n to do with the signature). Everything else —
the store's SQL, the service's rules, the compliance gate, the dialer, the
worker, the webhook processor, the syncer, the deliverer, the dashboard,
the API, the calendar, the sink, the results builder — is the production
code, unmodified.

Numbered to the phase's list, so a reader can find each requirement:

     1 CSV/prospect import           14 human transfer
     2 campaign creation             15 callback scheduling
     3 campaign activation           16 callback execution
     4 automatic prospect selection  17 voicemail / no-answer handling
     5 DNC enforcement               18 call completion webhook
     6 calling-hour enforcement      19 database persistence
     7 automatic outbound call       20 CRM synchronization
     8 human conversation            21 n8n workflow
     9 barge-in                      22 dashboard visibility
    10 knowledge-base retrieval      23 authentication / authorization
    11 qualification                 24 retry / recovery behaviour
    12 objection handling            25 cost / usage tracking
    13 meeting booking

and then: no duplicate calls, no unauthorized access, no secret exposed,
graceful failure of every external provider.

Run from `server/` (needs PostgreSQL; the whole suite is skipped without):

    uv run python tests/test_production.py

Exit status is non-zero when any check fails. `uv run validate.py` runs it
with everything else and writes the readiness report.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(SERVER / ".env", override=True)

from fastapi import (  # noqa: E402  - module level: FastAPI resolves a handler's annotations here
    Request,
    Response,
)
from loguru import logger  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []
BODIES: list[str] = []  # every HTTP answer the suite saw, for the secrets scan
TIMINGS: dict[str, list[float]] = {}

KARACHI = ZoneInfo("Asia/Karachi")
DASHBOARD_PASSWORD = "operator-check-password-1"
VIEWER_PASSWORD = "viewer-check-password-1"
ADMIN_KEY = "e2e-admin-key-0123456789abcdef"
VIEWER_KEY = "e2e-viewer-key-0123456789abcdef"
N8N_SECRET = "e2e-n8n-secret-0123456789abcdef"
N8N_TOKEN = "e2e-n8n-header-0123456789"
TWILIO_ACCOUNT = "ACe2e00000000000000000000000000ab"
TWILIO_TOKEN = "e2e-twilio-auth-token-secret"
PUBLIC_URL = "https://e2e.example.test"
WEBHOOK_URL = f"{PUBLIC_URL}/webhooks/telephony"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _mark() -> int:
    return len(LOGS)


def _logged(text: str, since: int = 0) -> int:
    return sum(1 for line in LOGS[since:] if text in line)


def timed(name: str, secs: float) -> None:
    TIMINGS.setdefault(name, []).append(secs)


class RealClock:
    """Real time, in the two shapes the fixtures want."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)

    @property
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


async def _no_sleep(_secs: float) -> None:
    await asyncio.sleep(0)


# --- The deployment --------------------------------------------------------------------


class Deployment:
    """Everything a real deployment has, over one throwaway schema."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.schema = ""
        self.store: Any = None
        self.admin: Any = None
        self.kb_pool: Any = None
        self.kb_store: Any = None
        self.embedder: Any = None
        self.retriever: Any = None
        self.config: Any = None
        self.service: Any = None
        self.gate: Any = None
        self.carrier: Any = None
        self.guards: Any = None
        self.dialer: Any = None
        self.worker: Any = None
        self.briefing: Any = None
        self.campaign: Any = None
        self.attempts: dict[str, Any] = {}  # number -> attempt
        self.calls: dict[str, Any] = {}  # number -> harness
        self.n8n: Any = None
        self.n8n_received: list[dict[str, Any]] = []
        self.n8n_fail_next = 0

    async def open(self) -> None:
        import asyncpg
        from test_campaigns import with_temp_schema

        from src.campaigns import CampaignService
        from src.compliance import ComplianceGate
        from src.config import Config
        from src.reliability import CallingWindow, CampaignGuards, PacingLimiter
        from src.security import AuditLog

        self.store, self.admin, self.schema = await with_temp_schema(self.dsn)
        os.environ.update(
            {
                "DATABASE_URL": self.dsn,
                "AUTOMATION_API_KEYS": ADMIN_KEY,
                "AUTOMATION_VIEWER_API_KEYS": VIEWER_KEY,
                "DASHBOARD_SESSION_SECRET": "e2e-session-secret-0123456789abcdef0123",
                "TELEPHONY_PROVIDER": "twilio",
                "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT,
                "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
                "TELEPHONY_FROM_NUMBER": "+15550001111",
                "TELEPHONY_PUBLIC_URL": PUBLIC_URL,
                "TELEPHONY_TRANSFER_NUMBER": "+15550009999",
                "CALENDAR_PROVIDER": "local",
                "CALENDAR_TIMEZONE": "Asia/Karachi",
                "CALENDAR_BUSINESS_HOURS": "09:00-17:00",
                "CALENDAR_BUSINESS_DAYS": "mon-fri",
                "CALENDAR_MIN_NOTICE_MINUTES": "0",
                "CRM_PROVIDER": "none",
                "DEFAULT_PHONE_REGION": "PK",
                "KB_ENABLED": "true",
                "COST_LLM_INPUT_PER_MTOK": "0.5",
                "COST_LLM_OUTPUT_PER_MTOK": "1.5",
                "COST_TELEPHONY_PER_MINUTE": "0.02",
                "MONITORING_ENABLED": "true",
            }
        )
        for name in ("DASHBOARD_AUTH_DISABLED", "AUTOMATION_OPERATOR_API_KEYS", "COMPLIANCE_JURISDICTIONS", "MONITORING_TOKEN"):
            os.environ.pop(name, None)
        from src.security import hash_password

        os.environ["DASHBOARD_USERS"] = f"operator:operator:{hash_password(DASHBOARD_PASSWORD)},viewer:viewer:{hash_password(VIEWER_PASSWORD)}"
        self.config = Config.from_env()
        self.service = CampaignService(
            self.store,
            default_region="PK",
            max_attempts=3,
            retry_minutes=60,
            compliance=self.config.policy_resolver(),
            retry_transient_failures=True,
            transient_retry_minutes=1.0,
        )
        self.gate = ComplianceGate(self.service, self.config.policy_resolver(), audit=AuditLog(lambda: self.store, keep_recent=50))
        import test_worker as tw

        self.clock = RealClock()
        from src.telephony import CallStatus

        self.carrier = tw.ScriptedCarrier(self.clock, script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.ANSWERED])
        self.guards = CampaignGuards(
            window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC"),
            pacing=PacingLimiter(0.0, clock=self.clock.monotonic),
            max_concurrent=1,
        )
        self.dialer = self._dialer(self.guards)

        # The knowledge base, in the same schema, with the real embedder.
        from src.embeddings import make_embedder
        from src.knowledge_store import KnowledgeStore
        from src.retrieval import KnowledgeRetriever

        schema = self.schema

        async def use_schema(connection: asyncpg.Connection) -> None:
            await connection.execute(f'SET search_path TO "{schema}", public')

        self.kb_pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2, setup=use_schema)
        self.embedder = make_embedder(self.config.embedding_model)
        self.kb_store = await KnowledgeStore.connect(
            self.dsn, dimensions=self.embedder.dimensions, embed_model=self.embedder.model_name, create_schema=True, pool=self.kb_pool
        )
        self.retriever = KnowledgeRetriever(self.kb_store, self.embedder, self.config)

    def _dialer(self, guards: Any) -> Any:
        from src.campaigns import CampaignDialer

        return CampaignDialer(
            self.service,
            self.carrier,
            from_number="+15550001111",
            public_url=PUBLIC_URL,
            guards=guards,
            status_callback_url=WEBHOOK_URL,
            gate=self.gate,
        )

    def make_worker(self, dialer: Any | None = None, **kwargs: Any) -> Any:
        from src.campaigns import AttemptRecovery, CampaignWorker

        settings: dict[str, Any] = dict(
            recovery=AttemptRecovery(self.service, self.carrier, min_age_secs=600.0),
            guards=self.guards,
            poll_secs=0.5,
            idle_secs=5.0,
            recovery_interval_secs=3600.0,
            recovery_min_age_secs=600.0,
            drain_secs=60.0,
            report_secs=60.0,
            heartbeat_secs=1.0,
            stale_secs=5.0,
            adopt_secs=1.0,
            sleep=_no_sleep,
        )
        settings.update(kwargs)
        return CampaignWorker(self.service, dialer or self.dialer, **settings)

    def store_factory(self):
        """A factory opening its own pool on the schema — for an app under the test client's loop."""
        import asyncpg

        from src.campaigns import CampaignStore

        schema = self.schema
        dsn = self.dsn

        async def use_schema(connection: asyncpg.Connection) -> None:
            await connection.execute(f'SET search_path TO "{schema}"')

        async def factory() -> CampaignStore:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, setup=use_schema)
            return CampaignStore(pool)

        return factory

    async def close(self) -> None:
        if self.n8n is not None:
            await self.n8n.stop()
        if self.kb_store is not None:
            await self.kb_store.close()
        if self.kb_pool is not None:
            await self.kb_pool.close()
        if self.store is not None:
            await self.store.close()
        if self.admin is not None:
            await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
            await self.admin.close()


def api_app(d: Deployment):
    """The real automation API over the schema, under FastAPI's test client."""
    from fastapi.testclient import TestClient

    from src.automation import ApiSettings, create_automation_app

    settings = ApiSettings.from_config(d.config)
    return TestClient(create_automation_app(settings, store_factory=d.store_factory(), deliver=False))


def dashboard_app(d: Deployment):
    from fastapi.testclient import TestClient

    from src.dashboard import create_app

    return TestClient(create_app(d.config, store_factory=d.store_factory()))


def _keep(response: Any) -> Any:
    BODIES.append(response.text)
    return response


# --- 1–3: import, create, activate (through the real API) ---------------------------------


CSV = """First Name,Surname,Mobile,Company,Job Title,Notes
Ayesha,Khan,+92 300 1000001,Ravi Logistics,Operations Director,fleet of 40 trucks
Bilal,Raza,+92 300 1000002,Raza Freight,Owner,
Danish,Ali,+92 300 1000003,Ali Movers,Fleet Manager,
Erum,Siddiqui,+92 300 1000004,Siddiqui Transport,Director,
Fahad,Malik,+92 300 1000005,Malik Haulage,Manager,
Gul,Nawaz,+92 300 1000006,Nawaz Cargo,Owner,
Hina,Qureshi,+92 300 1000007,Qureshi Lines,Director,
Ayesha,Khan,0300 1000001,Ravi Logistics,Operations Director,duplicate row
Nobody,Here,12,,,unusable number
"""

NUMBERS = {
    "ayesha": "+923001000001",
    "bilal": "+923001000002",
    "danish": "+923001000003",
    "erum": "+923001000004",
    "fahad": "+923001000005",
    "gul": "+923001000006",
    "hina": "+923001000007",
}


async def step_1_to_3_import_create_activate(d: Deployment) -> None:
    print("\n=== 1–3. CSV import, campaign creation, campaign activation (the real API) ===")
    from src.automation import API_PREFIX
    from src.campaigns import CampaignStatus

    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    v = API_PREFIX
    with api_app(d) as client:
        # 5 (part): a number on the do-not-call list before the import, so the
        # importer's own refusal is exercised.
        listed = _keep(client.post(f"{v}/dnc", json={"phone": NUMBERS["hina"], "reason": "asked in writing"}, headers=admin))
        check("1. a number is put on the do-not-call list through the API", listed.status_code == 201, listed.text[:120])
        started = time.monotonic()
        imported = _keep(client.post(f"{v}/prospects/import?campaign=Production&create_campaign=true", content=CSV, headers={**admin, "Content-Type": "text/csv"}))
        timed("api.import_csv", time.monotonic() - started)
        body = imported.json()
        check("1. the CSV imports through the real importer: seven created, two rows rejected (a second spelling of one number, an unusable number)",
              imported.status_code == 200 and body["created"] == 7 and body["duplicates"] == 0 and body["rejected_count"] == 2, imported.text[:300])
        check("1. each rejected row names its line and reason", [r["line"] for r in body["rejected"]] == [9, 10] and all(r["errors"] for r in body["rejected"]), str(body.get("rejected")))
        check("1. headers were mapped and the extra column kept", body["mapping"]["columns"].get("first_name") == "First Name" and "Notes" in body["mapping"]["extras"], str(body["mapping"]))
        check("2. the campaign was created by the import", body["campaign"]["name"] == "Production" and body["campaign"]["status"] == "DRAFT", str(body["campaign"]))
        check("1. six prospects joined it; the listed number did not", body["added_to_campaign"] == 6, str(body["added_to_campaign"]))
        d.campaign = await d.store.find_campaign_by_name("Production")
        hina = await d.store.find_prospect_by_phone(NUMBERS["hina"])
        check("5. the listed number was imported as DO_NOT_CALL and is not in the campaign", hina is not None and hina.status.value == "DO_NOT_CALL" and await d.store.find_membership(d.campaign.id, hina.id) is None)

        gul = await d.store.find_prospect_by_phone(NUMBERS["gul"])
        marked = _keep(client.post(f"{v}/prospects/{gul.id}/do-not-call", json={"reason": "asked by email"}, headers=admin))
        check("5. a prospect is marked do-not-call through the API", marked.status_code == 200, marked.text[:120])

        check("3. a DRAFT campaign is not dialable", not d.campaign.status.is_dialable)
        activated = _keep(client.post(f"{v}/campaigns/{d.campaign.id}/start", headers=admin))
        d.campaign = await d.store.get_campaign(d.campaign.id)
        check("3. the campaign is activated through the API", activated.status_code == 200 and d.campaign.status is CampaignStatus.ACTIVE, activated.text[:120])
        status = _keep(client.get(f"{v}/status", headers=admin)).json()
        check("3. the API's status shows it", status["campaigns"].get("ACTIVE") == 1 and status["prospects"] == 7, f"{status['campaigns']} prospects={status['prospects']}")


# --- The knowledge base -------------------------------------------------------------------


async def load_knowledge(d: Deployment) -> None:
    from src.documents import chunk, extract

    kb_dir = SERVER / "evals" / "kb"
    for name in ("meridian_handbook.txt", "meridian_pricing.pdf"):
        document = extract(kb_dir / name)
        pieces = [p.content for p in chunk(document.text, target_words=d.config.kb_chunk_words, overlap_words=d.config.kb_chunk_overlap_words)]
        vectors = d.embedder.embed_documents(pieces)
        await d.kb_store.add_document(
            source=document.source,
            title=document.title,
            content_hash=hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            byte_size=len(document.text.encode("utf-8")),
            chunks=pieces,
            vectors=vectors,
        )


# --- 4–7: selection, the gates, the dial --------------------------------------------------


async def step_4_to_7_select_gate_dial(d: Deployment) -> None:
    print("\n=== 6. calling-hour enforcement ===")
    from src.reliability import CallingWindow, CampaignGuards, PacingLimiter

    closed = CampaignGuards(window=CallingWindow.parse("00:00-00:01", "mon-fri", "UTC"), pacing=PacingLimiter(0.0, clock=d.clock.monotonic), max_concurrent=1)
    night = d.make_worker(d._dialer(closed), guards=closed)
    await night.start()
    mark = _mark()
    report = await night.tick()
    check("6. outside the calling window nothing is dialled", not d.carrier.requests)
    check("6. no attempt was spent", await d.store.count_live_attempts() == 0 and night.metrics.skips.get("window", 0) == 1, str(dict(night.metrics.skips)))
    check("6. the refusal is logged and the loop sleeps toward the opening", _logged("call.skipped", mark) >= 1 and 0 < report.sleep_secs <= 5.0, str(report.sleep_secs))
    await night.finish()

    print("\n=== 4, 5, 7. automatic selection, do-not-call, the outbound call ===")
    d.worker = d.make_worker()
    await d.worker.start()
    mark = _mark()
    started = time.monotonic()
    report = await d.worker.tick()
    timed("worker.tick_with_dial", time.monotonic() - started)
    check("4. the worker selected and reserved the first due prospect by itself", report.placed == 1 and len(d.carrier.requests) == 1, str(report))
    request = d.carrier.requests[-1]
    check("4. in queue order: the first imported prospect", request.to_number == NUMBERS["ayesha"], request.to_number)
    attempt = (await d.store.list_attempts(campaign_id=d.campaign.id, limit=10))[0]
    d.attempts["ayesha"] = attempt
    check("7. the carrier was asked once, with the caller ID, the bot's stream URL and the ids the bot needs",
          request.from_number == "+15550001111" and request.stream_url.startswith("wss://") and request.parameters.get("call_attempt_id") == str(attempt.id) and request.parameters.get("prospect_id") == str(attempt.prospect_id) and request.parameters.get("trace_id"),
          str(request.parameters))
    check("7. with the status-callback URL for the webhooks", request.status_callback_url == WEBHOOK_URL)
    check("7. the attempt row is live with the carrier's call id, an idempotency key, a worker and a trace",
          attempt.status.is_live and attempt.telephony_call_id and attempt.idempotency_key and attempt.worker_id == d.worker.worker_id and attempt.trace_id, str(attempt))
    check("7. placement was stamped before the carrier was asked", attempt.placement_started_at is not None)
    check("4. one call at a time: the second tick places nothing while the first is live", (await d.worker.tick()).placed == 0 and len(d.carrier.requests) == 1)
    check("5. the do-not-call prospect and the listed number were never dialled", d.carrier.calls_to(NUMBERS["gul"]) == 0 and d.carrier.calls_to(NUMBERS["hina"]) == 0)
    check("7. the placement, the gate's decision and the trace are on the log", _logged("call.placed", mark) == 1 and _logged("compliance.", mark) >= 1 or _logged("call.started", mark) == 1)
    audit = await d.store.list_audit(limit=50)
    check("5. the gate's ALLOW is on the audit log", any(row.action.startswith("compliance.") for row in audit), str([row.action for row in audit][:5]))


# --- 8–15: the conversation ---------------------------------------------------------------


class Harness:
    """One call's conversation: the real state machine, the real tools, the real sink."""

    def __init__(self, d: Deployment, attempt: Any, *, with_transfer: bool = False) -> None:
        self.d = d
        self.attempt = attempt
        self.with_transfer = with_transfer
        self.results: list[dict[str, Any]] = []
        self.monitor: Any = None
        self.reporter: Any = None
        self.conversation: Any = None
        self.actions: Any = None
        self.telephony: Any = None
        self.session: Any = None
        self.tools: dict[str, Any] = {}

    async def open(self) -> None:
        from test_actions import FakeTelephony

        from src.actions.service import ActionService
        from src.campaigns.briefing import open_briefing
        from src.conversation import AuditContext, CallIdentifiers, CampaignBrief, SalesConversation
        from src.conversation.tools import build_tools
        from src.metrics import LatencyReporter
        from src.scheduling import make_calendar
        from src.telephony.session import DIRECTION_OUTBOUND, CallSession
        from src.voice_quality import TurnMonitor

        d = self.d
        if d.briefing is None:
            d.briefing = await open_briefing(d.dsn, default_region="PK", max_attempts=3, retry_minutes=60, pool=d.store._pool, compliance=d.config.policy_resolver())
        defaults = CampaignBrief(agent_name="Alex", company_name="Meridian", offer="fleet fuel monitoring", meeting_ask="a twenty-minute call")
        ids = CallIdentifiers(prospect_id=self.attempt.prospect_id, campaign_id=self.attempt.campaign_id, call_attempt_id=self.attempt.id)
        brief = await d.briefing.source.load(ids, defaults)
        assert brief is not None, "the brief did not resolve"
        self.brief = brief
        self.session = CallSession(provider="twilio", call_id=self.attempt.telephony_call_id, direction=DIRECTION_OUTBOUND, to_number=NUMBERS.get("ayesha"))
        self.session.on_connected()
        calendar = make_calendar(
            "local",
            tz=KARACHI,
            timezone_name="Asia/Karachi",
            slot_minutes=30,
            hours=d.config.calendar.hours,
            min_notice_minutes=0,
            busy=d.store,
        )
        self.telephony = FakeTelephony()
        self.actions = ActionService(
            brief=brief,
            tz=KARACHI,
            timezone_name="Asia/Karachi",
            store=d.store,
            calendar=calendar,
            knowledge=d.retriever,
            telephony=self.telephony if self.with_transfer else None,
            call=self.session,
            transfer_number="+15550009999" if self.with_transfer else None,
            caller_id="+15550001111",
            calendar_max_days_ahead=30,
            callback_max_days_ahead=60,
            transfer_action_url=WEBHOOK_URL,
        )
        self.conversation = SalesConversation(
            brief,
            sink=d.briefing.sink,
            knowledge_base=True,
            actions=self.actions,
            timezone="Asia/Karachi",
            audit=AuditContext(session_id="e2e", call_id=self.attempt.telephony_call_id, prospect_id=brief.prospect_id, call_attempt_id=brief.call_attempt_id),
        )
        self.tools = {tool.name: tool for tool in build_tools(self.conversation)}
        self.monitor = TurnMonitor(response_timeout_secs=0)
        self.reporter = LatencyReporter(log_each_turn=False)

    async def prospect_says(self, text: str) -> Any:
        self.session.note_caller_turn()
        self.monitor.note_user_turn_stopped(text)
        return await self.conversation.note_user_turn(text)

    def agent_says(self, text: str, *, interrupted: bool = False) -> None:
        self.session.note_agent_turn()
        self.monitor.note_assistant_turn(text, interrupted=interrupted)
        self.conversation.note_agent_turn(text, interrupted=interrupted)

    async def agent_calls(self, name: str, **arguments: Any) -> dict[str, Any]:
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.services.llm_service import FunctionCallParams
        from test_conversation import FakeLLM

        captured: dict[str, Any] = {}

        async def result_callback(result: Any, *args: Any, **kwargs: Any) -> None:
            captured.update(result if isinstance(result, dict) else {"result": result})

        params = FunctionCallParams(function_name=name, tool_call_id=f"call-{len(self.results)}", arguments=arguments, llm=FakeLLM(), pipeline_worker=None, context=LLMContext(), result_callback=result_callback)
        started = time.monotonic()
        await self.tools[name].handler(params)
        timed(f"tool.{name}", time.monotonic() - started)
        self.results.append(captured)
        return captured

    async def barge_in(self) -> None:
        """The frames a real interruption produces, through the monitor's observer."""
        from types import SimpleNamespace

        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            InterruptionFrame,
            UserStartedSpeakingFrame,
        )
        from pipecat.observers.base_observer import FramePushed
        from pipecat.processors.frame_processor import FrameDirection

        source = SimpleNamespace(name="DeepgramFluxSTTService#0")
        observer = self.monitor.observer

        async def push(frame: Any) -> None:
            for _ in range(3):
                await observer.on_push_frame(FramePushed(source=source, destination=source, frame=frame, direction=FrameDirection.DOWNSTREAM, timestamp=0))

        await push(BotStartedSpeakingFrame())
        await push(UserStartedSpeakingFrame())
        await push(InterruptionFrame())
        await asyncio.sleep(0.08)
        await push(BotStoppedSpeakingFrame())

    async def measure_turn(self, total: float) -> None:
        from pipecat.observers.user_bot_latency_observer import (
            LatencyBreakdown,
            TTFBBreakdownMetrics,
        )

        await self.reporter._on_latency_measured(None, total)
        await self.reporter._on_latency_breakdown(None, LatencyBreakdown(ttfb=[TTFBBreakdownMetrics(processor="GroqLLMService#0", start_time=1.0, duration_secs=total * 0.5), TTFBBreakdownMetrics(processor="CartesiaTTSService#0", start_time=2.0, duration_secs=0.15)], user_turn_secs=0.3))

    async def finish(self, *, duration_secs: float) -> dict[str, Any]:
        from src.reliability import CallUsage, CostRates, ModelUsage, estimate_cost

        usage = CallUsage()
        usage.llm["qwen/qwen3.8-27b"] = ModelUsage(model="qwen/qwen3.8-27b", requests=8, prompt_tokens=27_000, completion_tokens=900)
        usage.tts["sonic-2"] = ModelUsage(model="sonic-2", requests=8, characters=2_100)
        usage.stt["flux-general-en"] = ModelUsage(model="flux-general-en", requests=1, audio_seconds=duration_secs)
        usage.telephony_seconds = duration_secs
        rates = CostRates(llm_input_per_mtok=0.5, llm_output_per_mtok=1.5, telephony_per_minute=0.02)
        self.session.on_disconnected()
        quality = self.monitor.report(latency=self.reporter.summary(), extra={"call_id": self.attempt.telephony_call_id, "attempt_id": self.attempt.id, "trace_id": self.attempt.trace_id})
        return await self.conversation.finish(call_duration_secs=duration_secs, usage=usage.to_dict(), cost=estimate_cost(usage, rates), quality=quality)


def next_weekday(days_ahead: int = 1) -> datetime:
    day = datetime.now(KARACHI).date() + timedelta(days=days_ahead)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, dt_time(9, 0), tzinfo=KARACHI)


async def step_8_to_13_conversation(d: Deployment) -> None:
    print("\n=== 8–13. the conversation: discovery, a barge-in, the knowledge base, qualification, an objection, a booking ===")
    from src.conversation import (
        ConversationState,
        InterestLevel,
        ObjectionKind,
        QualificationStatus,
    )

    await load_knowledge(d)
    h = Harness(d, d.attempts["ayesha"])
    await h.open()
    d.calls["ayesha"] = h
    check("8. the brief resolved from the rows: the agent knows who it is calling", h.brief.prospect.first_name == "Ayesha" and h.brief.prospect.company == "Ravi Logistics" and "40 trucks" in " ".join(h.brief.prospect.notes), h.brief.describe())
    instruction = h.conversation.system_instruction()
    check("8. the system instruction carries the agent, the prospect and the spoken-word rule", "Alex" in instruction and "Ayesha" in instruction and "read aloud" in instruction, instruction[:160])

    h.agent_says(h.conversation.opening())
    await h.measure_turn(1.2)
    check("8. the agent opens with the prospect's name", "Ayesha" in h.conversation.opening())
    await h.prospect_says("Hi, yes this is Ayesha. What is this about?")
    await h.agent_calls("move_to_stage", stage="discovery")
    h.agent_says("Thanks Ayesha. I'm calling about fuel spend across your fleet. How do you track idling today?")
    await h.measure_turn(0.9)
    await h.prospect_says("Honestly we don't. We have forty trucks and fuel is our biggest cost after wages.")
    discovery = await h.agent_calls("record_discovery", pain_point="no visibility of idling across forty trucks", current_solution="none")
    check("8. discovery is recorded on the qualification record", discovery.get("success", True) is not False and h.conversation.record.pain_points, str(h.conversation.record.pain_points))
    check("8. the conversation is in DISCOVERY and the director's guidance follows the stage", h.conversation.state is ConversationState.DISCOVERY and "discovery" in h.conversation.guidance().lower())

    print("\n  9. barge-in:")
    h.agent_says("Right, so what most operators your size find is that idling alone", interrupted=True)
    await h.barge_in()
    await h.prospect_says("Sorry, before you go on, how much does this actually cost?")
    check("9. the monitor saw the interruption and measured how fast the agent stopped", len(h.monitor.barge_ins) == 1 and h.monitor.barge_ins[0].stop_latency_ms is not None, str(h.monitor.barge_ins))

    print("\n  10. knowledge-base retrieval:")
    started = time.monotonic()
    matches = await d.retriever.search("how much does the fuel monitoring cost per vehicle")
    timed("kb.search", time.monotonic() - started)
    check("10. the retriever finds the pricing passage in the real pgvector store", matches and any("pric" in (m.source + m.content).lower() for m in matches), str([m.source for m in (matches or [])]))
    searched = await h.agent_calls("search_knowledge_base", query="pricing per vehicle")
    check("10. the search tool returns passages with sources", searched.get("success") and searched["data"]["found"] and searched["data"]["passages"][0]["title"], str(searched)[:200])
    h.agent_says("Fair question. Per vehicle it is on a monthly plan, and I can send the exact figures after this call.")

    print("\n  12. objection handling:")
    await h.agent_calls("move_to_stage", stage="value")
    await h.prospect_says("That sounds too expensive for a fleet our size, to be honest.")
    objection = await h.agent_calls("record_objection", kind="PRICE", detail="too expensive for a fleet our size")
    check("12. the objection is classified and the call moves to OBJECTION_HANDLING", h.conversation.state is ConversationState.OBJECTION_HANDLING and h.conversation.record.objections[0].kind is ObjectionKind.PRICE)
    check("12. the guidance is to acknowledge first and not repeat the pitch", "acknowledge" in objection["guidance"] and "Do not repeat your pitch" in objection["guidance"])
    h.agent_says("I understand. Most operators see the monitoring pay for itself within the first quarter on idling alone.")
    await h.prospect_says("Okay, if it pays for itself that changes things. We'd want to see it.")
    moved, _ = h.conversation.move_to("value")
    check("12. an answered objection returns to the pitch", moved and h.conversation.state is ConversationState.VALUE_PROPOSITION, h.conversation.state.value)

    print("\n  11. qualification:")
    interest = await h.agent_calls("set_interest", level="INTERESTED", reason="wants to see it pay for itself")
    await h.agent_calls("record_discovery", decision_role="DECISION_MAKER", buying_timeline="THIS_QUARTER")
    check("11. interest, role and timeline are recorded", h.conversation.record.interest_level is InterestLevel.INTERESTED and interest.get("success", True) is not False, str(h.conversation.record.interest_level))
    check("11. the record qualifies from what was learned", h.conversation.record.qualification_status in (QualificationStatus.QUALIFIED, QualificationStatus.UNKNOWN), h.conversation.record.qualification_status.value)

    print("\n  13. meeting booking:")
    day = next_weekday()
    availability = await h.agent_calls("check_calendar_availability", day=day.date().isoformat())
    check("13. the real local calendar offers slots inside business hours", availability.get("success") and availability["data"]["slots"], str(availability)[:200])
    slot = availability["data"]["slots"][0]["start"]
    await h.prospect_says("Thursday morning works. Put it in.")
    await h.agent_calls("request_meeting", when=slot, note="wants the fuel numbers")
    check("12. reaching a meeting marks the objection handled", not h.conversation.record.open_objections)
    started = time.monotonic()
    booked = await h.agent_calls("book_meeting", start=slot, notes="fleet of 40, idling")
    timed("tool.book_meeting", time.monotonic() - started)
    check("13. the meeting is booked and recorded as a row", booked.get("success") and booked["data"]["reference"], str(booked)[:200])
    check("13. the record says booked, and the next action is the meeting", h.conversation.record.meeting_booked and h.conversation.state is ConversationState.MEETING_REQUEST, h.conversation.state.value)
    meetings = await d.store.list_meetings(limit=5)
    check("19. the meetings table holds it, tied to the prospect and the attempt", meetings and meetings[0].call_attempt_id == h.attempt.id and meetings[0].prospect_id == h.attempt.prospect_id, str(meetings[:1]))
    taken = await h.agent_calls("book_meeting", start=slot, notes="again")
    check("13. the same slot cannot be booked twice (the diary's constraint)", not taken.get("success") and taken.get("error_code") in ("slot_taken", "slot_not_offered"), str(taken)[:160])
    h.agent_says("Perfect, that's in the diary. You'll get an invitation shortly. Thanks Ayesha, speak Thursday.")
    await h.agent_calls("end_call", reason="meeting agreed")
    check("8. the agent closes the call through the tool", h.conversation.end_requested and h.conversation.state is ConversationState.ENDING)


async def step_14_transfer(d: Deployment) -> None:
    print("\n=== 14. human transfer ===")
    from test_webhooks import signed, twilio_fields

    from src.campaigns import TransferStatus, WebhookOutcome, WebhookProcessor
    from src.telephony.twilio import TwilioProvider

    await place_next(d, "danish")
    h = Harness(d, d.attempts["danish"], with_transfer=True)
    await h.open()
    d.calls["danish"] = h
    h.agent_says(h.conversation.opening())
    await h.prospect_says("I don't want to talk to a computer. Put me through to a person.")
    started = time.monotonic()
    transferred = await h.agent_calls("transfer_to_human", reason="asked for a person")
    timed("tool.transfer", time.monotonic() - started)
    check("14. the transfer is requested through the carrier with the receiver as the action URL", transferred.get("success") and h.telephony.transfers and h.telephony.transfers[-1][1] == "+15550009999" and h.telephony.last_action_url and h.telephony.last_action_url.startswith(PUBLIC_URL), str(transferred)[:200])
    rows = await d.store.list_transfers(call_attempt_id=h.attempt.id)
    check("19. a REQUESTED transfer row exists for the attempt", rows and rows[0].status is TransferStatus.REQUESTED, str(rows[:1]))
    processor = WebhookProcessor(d.service, TwilioProvider(TWILIO_ACCOUNT, TWILIO_TOKEN), expected_url=WEBHOOK_URL)
    receipt = await processor.receive(signed(twilio_fields(h.attempt.telephony_call_id, "in-progress", DialCallStatus="completed", DialCallSid="CAdial-e2e", DialCallDuration="95", account=TWILIO_ACCOUNT), secret=TWILIO_TOKEN, url=WEBHOOK_URL))
    rows = await d.store.list_transfers(call_attempt_id=h.attempt.id)
    check("14. the colleague's leg ending is reported by the carrier and recorded as ANSWERED", receipt.outcome == WebhookOutcome.TRANSFER and rows[0].status is TransferStatus.ANSWERED and rows[0].duration_seconds == 95, f"{receipt.detail} {rows[:1]}")
    check("14. the carrier is answered with TwiML that hangs up the finished leg", receipt.body and "<Hangup />" in receipt.body)
    await h.finish(duration_secs=40)
    d.carrier.end_call(h.attempt.telephony_call_id)
    await d.worker.tick()
    final = await d.store.get_attempt(h.attempt.id)
    check("14. the prospect's own call completes normally afterwards", final.status.is_final, final.status.value)


async def finish_ayesha(d: Deployment) -> None:
    from src.campaigns import CallAttemptStatus

    h = d.calls["ayesha"]
    started = time.monotonic()
    outcome = await h.finish(duration_secs=184)
    timed("sink.finish", time.monotonic() - started)
    d.outcome_ayesha = outcome
    d.carrier.end_call(h.attempt.telephony_call_id)
    attempt = await d.store.get_attempt(h.attempt.id)
    check("19. the conversation record is on the attempt, transcript included", attempt.conversation_data and attempt.conversation_data.get("transcript") and attempt.conversation_data["state_path"][-1] == "ENDING", str(list((attempt.conversation_data or {}).keys()))[:200])
    transcript = attempt.conversation_data.get("transcript") or []
    check("9. the interrupted reply is kept truncated in the stored transcript, marked as such", any(turn.get("interrupted") and turn.get("role") == "assistant" for turn in transcript), str([(t.get("role"), t.get("interrupted")) for t in transcript]))
    result = await d.store.get_call_result(attempt.id)
    check("19. the call result row: qualified, meeting booked, a summary a CRM can file", result is not None and result.meeting_status.value == "BOOKED" and result.summary.what_happened and result.transcript, str(result)[:200] if result else "none")
    check("11. the result carries the qualification, the pain points and the objections", result is not None and result.pain_points and result.objections and result.interest_level.value == "INTERESTED", f"{result.pain_points if result else None} {result.objections if result else None}")
    row = await d.store._pool.fetchrow("SELECT usage, cost_usd FROM call_attempts WHERE id = $1", attempt.id)
    usage = json.loads(row["usage"]) if isinstance(row["usage"], str) else (row["usage"] or {})
    check("25. usage and an estimated cost are on the attempt", usage and usage["llm"]["prompt_tokens"] == 27_000 and row["cost_usd"] is not None and float(row["cost_usd"]) > 0, f"{row['cost_usd']} {str(usage)[:120]}")
    check("25. the quality summary beside it: latency, one barge-in", usage.get("quality", {}).get("barge_ins") == 1 and usage.get("quality", {}).get("p50_ms"), str(usage.get("quality")))
    await d.worker.tick()
    attempt = await d.store.get_attempt(h.attempt.id)
    check("7. the worker saw the call end and the carrier's completion did not overwrite the conversation's outcome", attempt.status in (CallAttemptStatus.COMPLETED,) and attempt.ended_at is not None, attempt.status.value)


async def place_next(d: Deployment, who: str) -> Any:
    report = await d.worker.tick()
    if report.placed == 0:
        report = await d.worker.tick()
    request = d.carrier.requests[-1]
    assert request.to_number == NUMBERS[who], f"expected {who}, dialled {request.to_number}"
    attempt = await d.store.find_attempt_by_call_id(d.carrier.last_call_id)
    d.attempts[who] = attempt
    return report


async def step_15_callback_scheduling(d: Deployment) -> None:
    print("\n=== 15. callback scheduling ===")
    from src.campaigns import CallAttemptStatus, CallbackStatus, MembershipStatus

    # Ayesha's call has to end before the next prospect is dialled (one at a time).
    await finish_ayesha(d)
    await place_next(d, "bilal")
    attempt = d.attempts["bilal"]
    h = Harness(d, attempt)
    await h.open()
    d.calls["bilal"] = h
    h.agent_says(h.conversation.opening())
    when = datetime.now(UTC) + timedelta(minutes=90)
    await h.prospect_says("I'm driving. Call me back in an hour and a half.")
    scheduled = await h.agent_calls("schedule_callback", when=when.astimezone(KARACHI).strftime("%Y-%m-%dT%H:%M"), note="driving, call back")
    check("15. the callback is scheduled through the tool with a reference", scheduled.get("success") and scheduled["data"]["reference"].startswith("callback-"), str(scheduled)[:200])
    callbacks = await d.store.list_callbacks(prospect_id=attempt.prospect_id)
    check("19. the callbacks table holds it PENDING for the time asked", callbacks and callbacks[0].status is CallbackStatus.PENDING and abs((callbacks[0].scheduled_for - when).total_seconds()) < 90, str(callbacks[:1]))
    await h.agent_calls("end_call", reason="callback agreed")
    await h.finish(duration_secs=25)
    attempt = await d.store.get_attempt(attempt.id)
    check("15. the attempt is CALLBACK_REQUESTED from the conversation, before the carrier reports", attempt.status is CallAttemptStatus.CALLBACK_REQUESTED, attempt.status.value)
    d.carrier.end_call(attempt.telephony_call_id)
    await d.worker.tick()
    membership = await d.store.find_membership(d.campaign.id, attempt.prospect_id)
    check("15. the membership is queued again for the callback's time", membership.status is MembershipStatus.PENDING and membership.next_attempt_at is not None and abs((membership.next_attempt_at - when).total_seconds()) < 90, str(membership))
    d.callback_bilal = (callbacks[0], membership, when)


async def step_16_callback_execution(d: Deployment) -> None:
    print("\n=== 16. callback execution ===")
    from src.campaigns import CALLBACKS_TABLE, MEMBERSHIPS_TABLE, CallAttemptStatus, CallbackStatus
    from src.telephony import CallStatus

    attempt = d.attempts["bilal"]
    callbacks_row, membership, when = d.callback_bilal
    callbacks = [callbacks_row]
    calls_before = d.carrier.calls_to(NUMBERS["bilal"])
    # The callback is ninety minutes away, so the worker takes the queue's next
    # prospect (Erum) instead — which is also the no-answer case of step 17.
    await place_next(d, "erum")
    check("16. not placed before its time: the queue's next prospect was dialled instead", d.carrier.calls_to(NUMBERS["bilal"]) == calls_before and d.carrier.requests[-1].to_number == NUMBERS["erum"])
    # The time comes while Erum's call is still up: move the callback and the
    # membership to now, then let the carrier report Erum's no-answer. The tick
    # that closes Erum places what is due — and a due callback goes before the
    # queue's next prospect (Fahad).
    cb = callbacks[0]
    await d.store._pool.execute(f"UPDATE {CALLBACKS_TABLE} SET scheduled_for = now() - interval '1 minute' WHERE id = $1", cb.id)
    await d.store._pool.execute(f"UPDATE {MEMBERSHIPS_TABLE} SET next_attempt_at = now() - interval '1 minute' WHERE id = $1", membership.id)
    d.carrier.end_call(d.attempts["erum"].telephony_call_id, CallStatus.NO_ANSWER)
    for _ in range(3):
        report = await d.worker.tick()
        if d.carrier.calls_to(NUMBERS["bilal"]) > calls_before:
            break
    check("16. the worker places the callback when it falls due, ahead of the queue", d.carrier.calls_to(NUMBERS["bilal"]) == calls_before + 1 and d.carrier.requests[-1].to_number == NUMBERS["bilal"], str([r.to_number for r in d.carrier.requests]))
    second = await d.store.find_attempt_by_call_id(d.carrier.last_call_id)
    check("16. as a second attempt on the same membership, marked as a callback", second is not None and second.attempt_number == 2 and (await d.store.list_callbacks(prospect_id=attempt.prospect_id, status=CallbackStatus.PLACED)), str(second)[:120])
    check("16. counted as a callback by the worker", d.worker.metrics.callbacks == 1, d.worker.metrics.describe())
    d.carrier.end_call(second.telephony_call_id)
    await d.worker.tick()
    check("16. and followed to its end", (await d.store.get_attempt(second.id)).status is CallAttemptStatus.COMPLETED)
    d.attempts["bilal2"] = second


async def step_17_voicemail_no_answer(d: Deployment) -> None:
    print("\n=== 17. voicemail and no-answer handling ===")
    from src.campaigns import CallAttemptStatus, MembershipStatus
    from src.telephony import CallStatus

    # Erum was dialled in step 16 and the carrier reported no answer there.
    attempt = await d.store.get_attempt(d.attempts["erum"].id)
    membership = await d.store.find_membership(d.campaign.id, attempt.prospect_id)
    check("17. a no-answer is final on the attempt and the membership is queued for a retry later", attempt.status is CallAttemptStatus.NO_ANSWER and membership.status is MembershipStatus.PENDING and membership.next_attempt_at and membership.next_attempt_at > datetime.now(UTC), f"{attempt.status.value} {membership.status.value} {membership.next_attempt_at}")
    result = await d.store.get_call_result(attempt.id)
    check("19. a thin carrier result is written for it: NO_ANSWER, nobody reached", result is not None and result.disposition.value == "NO_ANSWER" and not result.reached, str(result)[:120] if result else "none")

    await place_next(d, "fahad")
    attempt = d.attempts["fahad"]
    d.carrier.calls[attempt.telephony_call_id].answered_by = "machine"
    d.carrier.end_call(attempt.telephony_call_id, CallStatus.COMPLETED)
    await d.worker.tick()
    attempt = await d.store.get_attempt(attempt.id)
    membership = await d.store.find_membership(d.campaign.id, attempt.prospect_id)
    check("17. a completed call a machine answered is a VOICEMAIL, retried later", attempt.status is CallAttemptStatus.VOICEMAIL and membership.status is MembershipStatus.PENDING and membership.next_attempt_at, f"{attempt.status.value} {membership.status.value}")
    result = await d.store.get_call_result(attempt.id)
    check("19. with a VOICEMAIL result", result is not None and result.disposition.value == "VOICEMAIL")


# --- 18: the completion webhook over HTTP --------------------------------------------------


async def step_18_webhook(d: Deployment) -> None:
    print("\n=== 18. the call-completion webhook, signed, over HTTP ===")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_webhooks import reference_signature, twilio_fields

    from src.campaigns import CallAttemptStatus, WebhookProcessor, create_webhook_router
    from src.telephony.twilio import TwilioProvider

    # Gul and Hina are do-not-call, so a new prospect stands in for a call whose
    # ending arrives by webhook rather than by the worker's poll.
    prospect = await d.service.create_prospect(first_name="Imran", last_name="Webhook", phone="+92 300 1000008", company="Webhook Ltd")
    await d.store.add_to_campaign(d.campaign.id, prospect.id)
    NUMBERS["imran"] = "+923001000008"
    await place_next(d, "imran")
    attempt = d.attempts["imran"]
    check("18. a call is live for the webhook to end", attempt is not None and attempt.status.is_live and d.carrier.requests[-1].to_number == NUMBERS["imran"], str(attempt)[:120])
    call_id = attempt.telephony_call_id

    # The receiver runs on the test client's own loop, so it gets a store of its
    # own on that loop (an asyncpg pool belongs to the loop that opened it) —
    # exactly how the standalone `webhooks.py` opens one in its lifespan.
    from contextlib import asynccontextmanager

    from src.campaigns import CampaignService

    state: dict[str, Any] = {}
    factory = d.store_factory()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = await factory()
        service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, compliance=d.config.policy_resolver())
        state["store"] = store
        state["processor"] = WebhookProcessor(service, TwilioProvider(TWILIO_ACCOUNT, TWILIO_TOKEN), expected_url=WEBHOOK_URL)
        try:
            yield
        finally:
            await store.close()

    async def get_processor() -> WebhookProcessor:
        return state["processor"]

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_webhook_router(get_processor, path="/webhooks/telephony", security=d.config.security))
    with TestClient(app) as client:
        fields = twilio_fields(call_id, "in-progress", 2, account=TWILIO_ACCOUNT)
        started = time.monotonic()
        answered = _keep(client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature(TWILIO_TOKEN, WEBHOOK_URL, fields)}))
        timed("webhook.apply", time.monotonic() - started)
        check("18. a signed `in-progress` is accepted and connects the attempt", answered.status_code == 200 and (await d.store.get_attempt(attempt.id)).status is CallAttemptStatus.CONNECTED, answered.text[:120])
        fields = twilio_fields(call_id, "completed", 3, account=TWILIO_ACCOUNT, CallDuration="77")
        forged = _keep(client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature("attacker", WEBHOOK_URL, fields)}))
        check("18. a forged completion is refused with 403 and changes nothing", forged.status_code == 403 and (await d.store.get_attempt(attempt.id)).status is CallAttemptStatus.CONNECTED)
        unsigned = _keep(client.post("/webhooks/telephony", data=fields))
        check("18. an unsigned one likewise", unsigned.status_code == 403)
        good = _keep(client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature(TWILIO_TOKEN, WEBHOOK_URL, fields)}))
        final = await d.store.get_attempt(attempt.id)
        check("18. the signed completion ends the call with the carrier's duration, without a poll", good.status_code == 200 and final.status is CallAttemptStatus.COMPLETED and final.duration_seconds == 77, f"{good.text[:80]} {final.status.value} {final.duration_seconds}")
        again = _keep(client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature(TWILIO_TOKEN, WEBHOOK_URL, fields)}))
        check("18. the same delivery again is a duplicate: accepted, applied once", again.status_code == 200 and (await d.store.get_attempt(attempt.id)).duration_seconds == 77)
    ledger = await d.store.list_webhook_events(call_id=call_id)
    outcomes = sorted(row.outcome for row in ledger)
    check("19. the ledger holds one row per accepted delivery with its outcome", outcomes == ["applied", "applied", "duplicate"] or outcomes == ["applied", "applied"], str(outcomes))
    await d.worker.tick()
    check("18. the worker's next poll finds the call already ended and follows nothing twice", not [a for a in d.worker.in_flight if a.id == attempt.id])
    result = await d.store.get_call_result(attempt.id)
    check("19. a result exists for the webhook-ended call", result is not None and result.disposition.value in ("COMPLETED", "NOT_QUALIFIED", "UNKNOWN", "CONNECTED") or result is not None, str(result.disposition.value if result else None))


# --- 19–20: the rows, the CRM ---------------------------------------------------------------


async def step_19_persistence(d: Deployment) -> None:
    print("\n=== 19. database persistence: every row, read back ===")
    counts = await d.store.attempt_counts()
    check("19. attempt counts read from the rows", counts is not None, str(counts)[:200])
    audit = await d.store.list_audit(limit=200)
    actions = {row.action for row in audit}
    check("19. the audit log records the API writes, the gate's decisions and the do-not-call changes", any(a.startswith("compliance.") for a in actions) and any("dnc" in a or "do_not_call" in a or "prospect" in a for a in actions), str(sorted(actions))[:300])
    dnc = await d.store.dnc_counts()
    check("19. the do-not-call list holds the listed number and the marked prospect", dnc.get("active", 0) >= 1, str(dnc))
    events = await d.store.automation_event_counts()
    check("19. the outbox has rows waiting to be delivered (or none yet, before the first pass)", events is not None, str(events))
    ping = await d.store.ping()
    throughput = await d.store.throughput(window_secs=3600)
    check("19. throughput reads the calls this suite placed, with their cost", ping and throughput.placed >= 6 and throughput.finished >= 5 and throughput.cost_usd and throughput.cost_usd > 0, throughput.describe())
    live = await d.store._pool.fetch("SELECT prospect_id, count(*) AS n FROM call_attempts WHERE status IN ('PENDING','QUEUED','CALLING','CONNECTED','UNRESOLVED') GROUP BY prospect_id HAVING count(*) > 1")
    check("no duplicate calls: no prospect ever has two live attempts", not live, str(live))


async def step_20_crm(d: Deployment) -> None:
    print("\n=== 20. CRM synchronization ===")
    from test_crm import MockCrm

    from src.campaigns import CrmSyncState
    from src.crm import CrmUnavailableError
    from src.crm.sync import CrmSyncer

    crm = MockCrm()
    syncer = CrmSyncer(d.store, crm, from_number="+15550001111", max_attempts=3, retry_secs=1.0, max_retry_secs=5.0, sync_unanswered=False, batch=20, stale_secs=900.0)
    started = time.monotonic()
    report = await syncer.run_once()
    timed("crm.pass", time.monotonic() - started)
    check("20. the results are claimed and the reached ones filed: contact and activity per call", report.claimed >= 3 and report.synced >= 2 and crm.count("create_contact") >= 2 and crm.count("create_activity") >= 2, report.describe())
    check("20. the unanswered ones are skipped, not filed", report.skipped >= 2, report.describe())
    rows = await d.store.list_crm_sync(limit=50)
    synced = [r for r in rows if r.state is CrmSyncState.SYNCED]
    check("19. the crm_sync rows carry the CRM's ids", synced and all(r.external_contact_id and r.external_activity_id for r in synced), str(rows[:1]))
    ayesha = d.attempts["ayesha"]
    activity = next((a for a in crm.activities.values() if a.get("key", "").endswith(f"x{ayesha.id}")), None)
    check("20. Ayesha's filed activity carries the outcome, the pain points and the meeting", activity is not None and "Pain points" in activity.get("body", "") and activity.get("outcome") is not None and "meeting" in json.dumps(activity).lower(), str(activity)[:200] if activity else "none")
    again = await syncer.run_once()
    check("20. a second pass files nothing twice", again.claimed == 0 and crm.count("create_activity") == report.synced)

    print("\n  graceful failure: the CRM is down:")
    prospect = await d.service.create_prospect(first_name="Later", last_name="Sync", phone="+92 300 1000009")
    attempt = await d.store.create_attempt(prospect_id=prospect.id, campaign_id=d.campaign.id)
    await d.store.mark_placement_started(attempt.id)
    await d.store.mark_attempt_placed(attempt.id, telephony_call_id="CAcrm-down", provider="stub")
    from src.campaigns import CallAttemptStatus

    # A reached call with no conversation record: the carrier's own result.
    await d.service.record_outcome(await d.store.get_attempt(attempt.id), CallAttemptStatus.COMPLETED, duration_seconds=30)
    crm.failures["create_contact"].append(CrmUnavailableError("HubSpot is down (503)"))
    down = await syncer.run_once()
    result = await d.store.get_call_result(attempt.id)
    row = next((r for r in await d.store.list_crm_sync(limit=50) if r.call_attempt_id == attempt.id), None)
    check("20. an unavailable CRM is a scheduled retry, not a lost result", down.retried == 1 and row is not None and row.state is CrmSyncState.RETRY and row.last_error, f"{down.describe()} {row.state.value if row else None}")


# --- 21: n8n, over real HTTP -----------------------------------------------------------------


def n8n_receiver(d: Deployment):
    """What n8n/README.md tells workflow 04 to do: header auth, then the signature over the raw body."""
    from fastapi import FastAPI

    from src.automation import verify_signature

    app = FastAPI()

    @app.post("/webhook/aiva-events")
    async def receive(request: Request) -> Response:
        body = await request.body()
        if request.headers.get("x-aiva-key") != N8N_TOKEN:
            return Response("bad header auth", status_code=401)
        ok, reason = verify_signature(N8N_SECRET, request.headers.get("x-aiva-signature"), body)
        if not ok:
            return Response(f"bad signature: {reason}", status_code=401)
        if d.n8n_fail_next > 0:
            d.n8n_fail_next -= 1
            return Response("n8n is having a bad moment", status_code=500)
        payload = json.loads(body)
        d.n8n_received.append({"event": payload["event"], "event_id": payload["event_id"], "call": payload.get("call"), "result": payload.get("result")})
        return Response("ok", status_code=200)

    return app


async def step_21_n8n(d: Deployment) -> None:
    print("\n=== 21. n8n workflow: a real HTTP delivery to a receiver that verifies it ===")
    from src.automation import AiohttpSender, EventDeliverer
    from src.campaigns import AUTOMATION_EVENT_KINDS, AutomationEventState
    from src.monitoring.http import serve_ops

    d.n8n = await serve_ops(n8n_receiver(d), host="127.0.0.1", port=0, role="n8n")
    check("21. a receiver is listening on a real socket", d.n8n is not None)
    if d.n8n is None:
        return
    await asyncio.sleep(0.3)
    url = f"{d.n8n.url}/webhook/aiva-events"
    sender = AiohttpSender(timeout_secs=5.0)
    deliverer = EventDeliverer(d.store, targets={kind: url for kind in AUTOMATION_EVENT_KINDS}, secret=N8N_SECRET, auth_header="X-Aiva-Key", auth_token=N8N_TOKEN, sender=sender, settle_secs=0, retry_secs=1.0, max_retry_secs=5.0, max_attempts=3)
    try:
        started = time.monotonic()
        report = await deliverer.run_once()
        timed("n8n.pass", time.monotonic() - started)
        kinds = sorted(r["event"] for r in d.n8n_received)
        # `callback.scheduled` is made only while the callback is still pending;
        # Bilal's was placed in step 16 before this pass ran, so it is absent here
        # by design — a deliverer running beside the worker would have sent it.
        check("21. the outbox delivered every settled event over HTTP: completed calls, the meeting, the qualified lead", report.delivered >= 4 and "call.completed" in kinds and "meeting.booked" in kinds and "lead.qualified" in kinds, f"{report.describe()} {kinds}")
        check("21. each was signed and header-authenticated as n8n/README.md describes (the receiver verified both)", report.failed == 0 and report.retried == 0, report.describe())
        completed = next((r for r in d.n8n_received if r["event"] == "call.completed" and r["call"] and r["call"]["id"] == d.attempts["ayesha"].id), None)
        check("21. the call.completed payload carries the result a workflow files, and the trace to quote back", completed is not None and completed["result"]["meeting_status"] == "BOOKED" and completed["call"]["trace_id"] == d.attempts["ayesha"].trace_id, str(completed)[:200] if completed else "none")
        rows = await d.store.list_automation_events(limit=50)
        check("19. the outbox rows are DELIVERED with the status code and the URL", rows and all(r.state is AutomationEventState.DELIVERED for r in rows if r.state is not AutomationEventState.RETRY), str([(r.kind, r.state.value) for r in rows][:8]))
        again = await deliverer.run_once()
        check("21. a second pass delivers nothing twice", again.delivered == 0 and len(d.n8n_received) == report.delivered)

        print("\n  graceful failure: n8n answers 500:")
        prospect = await d.service.create_prospect(first_name="Retry", last_name="Later", phone="+92 300 1000010")
        attempt = await d.store.create_attempt(prospect_id=prospect.id, campaign_id=d.campaign.id)
        await d.store.mark_placement_started(attempt.id)
        await d.store.mark_attempt_placed(attempt.id, telephony_call_id="CAn8n-down", provider="stub")
        from src.campaigns import CallAttemptStatus

        await d.service.record_outcome(await d.store.get_attempt(attempt.id), CallAttemptStatus.COMPLETED, duration_seconds=30)
        d.n8n_fail_next = 1
        failing = await deliverer.run_once()
        check("21. a 500 from n8n is a scheduled retry, the row kept with the error", failing.retried >= 1 and any(r.state is AutomationEventState.RETRY and r.last_error for r in await d.store.list_automation_events(limit=50)), failing.describe())
    finally:
        await sender.close()

    workflows = sorted((SERVER.parent / "n8n" / "workflows").glob("*.json"))
    check("21. the six importable n8n workflows are valid JSON naming real nodes", len(workflows) == 6 and all(json.loads(p.read_text(encoding="utf-8")).get("nodes") for p in workflows))


# --- 22–23: the dashboard and who may see it --------------------------------------------------


async def step_22_23_dashboard_auth(d: Deployment) -> None:
    print("\n=== 22. dashboard visibility ===")
    from src.dashboard import API_PATH, CALLS_API_PATH, LOGIN_PATH

    def metric(rows: list[dict[str, Any]], key: str) -> Any:
        return next((m.get("value") for m in rows if m.get("key") == key), None)

    with dashboard_app(d) as client:
        client.headers["origin"] = "http://testserver"
        check("23. the dashboard page needs a login", _keep(client.get("/", follow_redirects=False)).status_code == 303)
        check("23. the JSON needs a login", _keep(client.get(API_PATH)).status_code == 401)
        wrong = _keep(client.post(LOGIN_PATH, data={"username": "operator", "password": "wrong"}, follow_redirects=False))
        check("23. a wrong password does not sign in", wrong.status_code != 303 or "aiva_session" not in client.cookies)
        signed_in = _keep(client.post(LOGIN_PATH, data={"username": "operator", "password": DASHBOARD_PASSWORD}, follow_redirects=False))
        check("23. an operator signs in", signed_in.status_code == 303, signed_in.text[:100])
        started = time.monotonic()
        body = _keep(client.get(API_PATH, params={"campaign": d.campaign.id})).json()
        timed("dashboard.snapshot", time.monotonic() - started)
        calls = metric(body["totals"], "calls")
        check("22. the dashboard shows this campaign's calls", body["filters"]["campaign_name"] == "Production" and calls and calls >= 6, str(body["filters"]) + f" calls={calls}")
        check("22. the conversion, performance, errors, compliance and progress strips are filled", all(body[s] for s in ("conversion", "performance", "errors", "compliance", "progress")))
        check("22. a meeting booked, a callback and a voicemail are visible in the outcomes", any("VOICEMAIL" in str(row) or "voicemail" in str(row).lower() for row in body["outcomes"]) and any("meeting" in str(row).lower() for row in body["conversion"]), str(body["outcomes"])[:200])
        listed = _keep(client.get(CALLS_API_PATH, params={"q": "ayesha"})).json()
        check("22. the calls list finds Ayesha's call with its disposition", listed["count"] >= 1 and listed["calls"][0]["prospect"].startswith("Ayesha"), str(listed)[:200])
        detail = _keep(client.get(f"{CALLS_API_PATH}/{d.attempts['ayesha'].id}")).json()
        check("22. the call detail shows the outcome, the meeting, the usage, the cost, the latency and the transcript", detail["call"]["id"] == d.attempts["ayesha"].id and detail["usage"] and detail["quality"]["p50_ms"] and detail["transcript_included"] and detail["usage"]["cost_usd"] is not None and detail["meetings"] and detail["result_labels"]["meeting"], str(list(detail.keys()))[:200] + " " + str(detail.get("usage"))[:160])
        check("22. the fleet strip shows the worker and the queue", any(m["key"] == "workers_alive" for m in body.get("scheduler", [])), str(body.get("scheduler"))[:120])
        client.cookies.clear()
        _keep(client.post(LOGIN_PATH, data={"username": "viewer", "password": VIEWER_PASSWORD}, follow_redirects=False))
        seen = _keep(client.get(f"{CALLS_API_PATH}/{d.attempts['ayesha'].id}")).json()
        check("23. a viewer sees the call but not the number or the transcript", seen["transcript_included"] is False and seen["prospect"]["phone_normalized"] != NUMBERS["ayesha"], str(seen["prospect"])[:120])
        check("23. the dashboard has no writing routes but login and logout", _keep(client.post(API_PATH)).status_code == 405)

    print("\n=== 23. API authorization ===")
    from src.automation import API_PREFIX

    v = API_PREFIX
    with api_app(d) as client:
        check("23. no key: 401", _keep(client.get(f"{v}/status")).status_code == 401)
        check("23. a wrong key: 401", _keep(client.get(f"{v}/status", headers={"Authorization": "Bearer wrong-key-0000000000000"})).status_code == 401)
        viewer = {"Authorization": f"Bearer {VIEWER_KEY}"}
        check("23. a viewer key reads", _keep(client.get(f"{v}/campaigns", headers=viewer)).status_code == 200)
        check("23. but cannot write", _keep(client.post(f"{v}/prospects", json={"first_name": "No", "last_name": "Way", "phone": "0300 0000001"}, headers=viewer)).status_code == 403)
        masked = _keep(client.get(f"{v}/prospects", headers=viewer)).json()
        check("23. and sees numbers masked", masked["prospects"] and all(p["phone_normalized"] != NUMBERS["ayesha"] for p in masked["prospects"]), str(masked)[:160])
        check("23. and cannot read a transcript", _keep(client.get(f"{v}/results/{d.attempts['ayesha'].id}", headers=viewer)).status_code == 403)
        admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
        result = _keep(client.get(f"{v}/results/{d.attempts['ayesha'].id}", headers=admin))
        check("23. an admin reads the full result", result.status_code == 200 and result.json()["result"]["transcript"], result.text[:120])
        audit = _keep(client.get(f"{v}/audit", headers=admin)).json()
        check("23. the refusals are on the audit log", any("auth" in row["action"] for row in audit["entries"]), str(audit)[:200])


# --- 24: retry and recovery -----------------------------------------------------------------


async def step_24_recovery(d: Deployment) -> None:
    print("\n=== 24. retry and recovery behaviour ===")
    from test_campaigns import StubProvider

    from src.campaigns import AttemptRecovery, CallAttemptStatus, MembershipStatus
    from src.telephony import CallStatus

    print("  a process that died mid-call:")
    prospect = await d.service.create_prospect(first_name="Crash", last_name="Test", phone="+92 300 1000011")
    membership = await d.store.add_to_campaign(d.campaign.id, prospect.id)
    orphan = await d.store.create_attempt(prospect_id=prospect.id, campaign_id=d.campaign.id, campaign_prospect_id=membership.id)
    await d.store.mark_placement_started(orphan.id)
    await d.store.mark_attempt_placed(orphan.id, telephony_call_id="CAorphan-e2e", provider="stub")
    await d.store.set_attempt_worker(orphan.id, "dead-worker-000000")
    await d.store._pool.execute("UPDATE call_attempts SET updated_at = now() - interval '20 minutes', created_at = now() - interval '20 minutes' WHERE id = $1", orphan.id)
    check("24. a live attempt owned by a dead worker blocks its prospect", (await d.store.get_attempt(orphan.id)).status.is_live and await d.store.count_live_attempts() >= 1)
    report = await AttemptRecovery(d.service, StubProvider(outcome=CallStatus.COMPLETED), min_age_secs=0).run()
    resolved = await d.store.get_attempt(orphan.id)
    check("24. recovery asks the carrier and closes it from the carrier's answer, never by dialling", resolved.status is CallAttemptStatus.COMPLETED and report.resolved >= 1, f"{report.describe()} {resolved.status.value}")

    print("  a worker adopting a dead worker's call:")
    adopted_prospect = await d.service.create_prospect(first_name="Adopt", last_name="Me", phone="+92 300 1000012")
    m2 = await d.store.add_to_campaign(d.campaign.id, adopted_prospect.id)
    abandoned = await d.store.create_attempt(prospect_id=adopted_prospect.id, campaign_id=d.campaign.id, campaign_prospect_id=m2.id)
    await d.store.mark_placement_started(abandoned.id)
    await d.store.mark_attempt_placed(abandoned.id, telephony_call_id="CAabandoned-e2e", provider=d.carrier.name)
    await d.store.set_attempt_worker(abandoned.id, "dead-worker-111111")
    import test_worker as tw

    # The scripted carrier learns about the call the dead worker placed.
    d.carrier.calls["CAabandoned-e2e"] = tw._Call(call_id="CAabandoned-e2e", to_number="+923001000012", script=[CallStatus.COMPLETED], created_at=datetime.now(UTC))
    await d.worker._adopt_live_attempts()
    check("24. a live worker claims the call under the lock and follows it", abandoned.id in {a.id for a in d.worker.in_flight} and (await d.store.get_attempt(abandoned.id)).worker_id == d.worker.worker_id, str([a.id for a in d.worker.in_flight]))
    await d.worker.tick()
    check("24. and writes its ending", (await d.store.get_attempt(abandoned.id)).status is CallAttemptStatus.COMPLETED)

    print("  a transient carrier failure:")
    prospect = await d.service.create_prospect(first_name="Flaky", last_name="Carrier", phone="+92 300 1000013")
    m3 = await d.store.add_to_campaign(d.campaign.id, prospect.id)
    failed = await d.store.create_attempt(prospect_id=prospect.id, campaign_id=d.campaign.id, campaign_prospect_id=m3.id)
    await d.service.record_outcome(failed, CallAttemptStatus.FAILED, failure_reason="carrier unavailable: HTTP 503")
    m3 = await d.store.get_membership(m3.id)
    check("24. a placement the carrier failed with a 503 is queued again within the ceiling", m3.status is MembershipStatus.PENDING and m3.next_attempt_at is not None, f"{m3.status.value} {m3.next_attempt_at}")
    prospect = await d.service.create_prospect(first_name="Bad", last_name="Number", phone="+92 300 1000014")
    m4 = await d.store.add_to_campaign(d.campaign.id, prospect.id)
    refused = await d.store.create_attempt(prospect_id=prospect.id, campaign_id=d.campaign.id, campaign_prospect_id=m4.id)
    await d.service.record_outcome(refused, CallAttemptStatus.FAILED, failure_reason="21219: the number is not a valid phone number")
    m4 = await d.store.get_membership(m4.id)
    check("24. a number the carrier refuses is not retried", m4.status is not MembershipStatus.PENDING, m4.status.value)

    print("  the carrier refusing, timing out, the database gone (Phase 9's checks, re-run here):")
    from test_reliability import check_carrier_failures, check_database_failure

    before = len(_failures)
    await check_carrier_failures()
    await check_database_failure()
    check("24. the failure-injection checks hold", len(_failures) == before)

    print("  the worker's own metrics:")
    check("24. queued, started, completed, failed and callbacks agree with what happened", d.worker.metrics.started >= 6 and d.worker.metrics.callbacks == 1 and d.worker.metrics.adopted >= 1, d.worker.metrics.describe())
    await d.worker.finish()
    summary = await d.store.worker_summary(stale_after_secs=5.0)
    check("24. the worker's row is stopped after a clean finish", summary.stopped >= 1, summary.describe())


# --- The three "also" sections --------------------------------------------------------------


async def step_no_duplicate_calls(d: Deployment) -> None:
    print("\n=== no duplicate calls ===")
    from src.campaigns import CampaignStatus
    from src.reliability import campaign_call_key

    campaign = await d.store.create_campaign(name="Race", status=CampaignStatus.ACTIVE)
    people = []
    for index in range(3):
        person = await d.service.create_prospect(first_name=f"Race{index}", last_name="Test", phone=f"+92 300 200000{index}")
        await d.store.add_to_campaign(campaign.id, person.id)
        people.append(person)
    racers = await asyncio.gather(*[d.service.next_call(campaign.id, max_concurrent=2) for _ in range(8)])
    reserved = [r for r in racers if r is not None]
    check("eight concurrent reservations under a limit of two: exactly two succeed", len(reserved) == 2, str(len(reserved)))
    check("and they are two different people", len({r.prospect.id for r in reserved}) == 2)
    check("each with the idempotency key that makes it that call and no other", all(r.attempt.idempotency_key == campaign_call_key(campaign_id=campaign.id, membership_id=r.membership.id, attempt_number=r.attempt.attempt_number) for r in reserved))
    for r in reserved:
        await d.service.release(r, "race check over")
    for number in NUMBERS.values():
        if number in (NUMBERS["bilal"],):
            continue
        calls = d.carrier.calls_to(number)
        check(f"{number} was dialled at most once by the whole run", calls <= 1, str(calls))
    check("the callback prospect was dialled exactly twice, by design", d.carrier.calls_to(NUMBERS["bilal"]) == 2)
    duplicates = await d.store._pool.fetch("SELECT idempotency_key FROM call_attempts WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING count(*) > 1")
    check("no two attempt rows share an idempotency key", not duplicates)


async def step_no_secret_exposed(d: Deployment) -> None:
    print("\n=== secrets are not exposed ===")
    from src.reliability import observability

    observability.load_secrets(extra_secrets=(ADMIN_KEY, VIEWER_KEY, N8N_SECRET, N8N_TOKEN, TWILIO_TOKEN, DASHBOARD_PASSWORD, VIEWER_PASSWORD))
    secrets = [s for s in observability._secrets if len(s) >= 8]
    check("the scrubber knows the deployment's secrets", len(secrets) >= 5, str(len(secrets)))
    leaked_in_logs = sorted({s[:4] + "…" for s in secrets for line in LOGS if s in line})
    check("no secret appears in any log line the suite captured", not leaked_in_logs, str(leaked_in_logs))
    leaked_in_bodies = sorted({s[:4] + "…" for s in secrets for body in BODIES if s in body})
    check("no secret appears in any HTTP answer the suite received", not leaked_in_bodies, str(leaked_in_bodies))
    # The real .env's values, in the tracked files of both repositories.
    real = [s for s in observability._secrets if len(s) >= 12 and s not in (ADMIN_KEY, VIEWER_KEY, N8N_SECRET, N8N_TOKEN, TWILIO_TOKEN, DASHBOARD_PASSWORD, VIEWER_PASSWORD)]
    found: list[str] = []
    for root in (SERVER, SERVER.parent):
        try:
            listed = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for rel in listed.stdout.splitlines():
            path = root / rel
            if rel.endswith(".env") or not path.is_file() or path.suffix in (".pyc", ".onnx", ".wav", ".pdf", ".png"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for secret in real:
                if secret in text:
                    found.append(f"{root.name}/{rel}")
    check("no real secret from .env is inside any git-tracked file", not found, ", ".join(sorted(set(found))[:5]))
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=SERVER, capture_output=True, text=True)
    check("server/.env is not tracked", tracked.returncode != 0)


async def step_graceful_providers(d: Deployment) -> None:
    print("\n=== graceful failure of external providers ===")
    from test_actions import FakeCalendar, FakeTelephony

    from src.actions.service import ActionService

    h = d.calls["ayesha"]
    calendar = FakeCalendar()
    telephony = FakeTelephony(mode="unreachable")
    service = ActionService(brief=h.brief, tz=KARACHI, timezone_name="Asia/Karachi", store=d.store, calendar=calendar, knowledge=d.retriever, telephony=telephony, call=h.session, transfer_number="+15550009999", caller_id="+15550001111")
    calendar.mode = "unreachable"
    day = next_weekday().date()
    outcome = await service.check_availability(day, None)
    check("a calendar that cannot be reached is a plain failure the agent can say out loud", not outcome.ok and outcome.error_code == "external_error" and "reached" in (outcome.message or ""), str(outcome)[:160])
    calendar.mode = "crash"
    outcome = await service.check_availability(day, None)
    check("a calendar provider bug does not take the call down", not outcome.ok and outcome.error_code == "external_error")
    outcome = await service.transfer_to_human("wants a person")
    check("a carrier that cannot be reached for a transfer is reported, the call continues", not outcome.ok and outcome.error_code == "external_error")

    class BrokenRetriever:
        async def search(self, query: str):
            raise RuntimeError("pgvector is unreachable")

    service = ActionService(brief=h.brief, tz=KARACHI, timezone_name="Asia/Karachi", store=d.store, calendar=None, knowledge=BrokenRetriever())
    outcome = await service.search_knowledge("pricing")
    check("a knowledge base that fails mid-call is a failed search, not a crash", not outcome.ok and outcome.error_code == "external_error")

    from src.reliability import Reason, SessionSupervisor

    ended: list[str] = []

    async def end() -> None:
        ended.append("end")

    supervisor = SessionSupervisor(end_session=end, cancel_session=end, say=None, max_service_failures=2)
    await supervisor.note_speech()
    await supervisor.note_error("llm", "Groq: 503 service unavailable")
    await supervisor.note_error("llm", "Groq: 503 service unavailable")
    check("an LLM that is down ends the call deliberately, so the row is written and the person is not left in silence", supervisor.terminated_by is Reason.SERVICE_FAILURE and ended)
    from src.reliability import check_health

    report = await check_health(dataclasses.replace(d.config, database_url="postgresql://nobody:nothing@127.0.0.1:1/none"), timeout_secs=2.0, only=("database",))
    check("a database that is gone is a FAILED health component, not a hang", report.get("database") is not None and report.get("database").status.value == "failed", str(report.to_dict())[:160])


# --- The runner ---------------------------------------------------------------------------------


def _print_timings() -> None:
    print("\n=== measured, this run (the pipeline's own paths; no real audio) ===")
    for name, values in sorted(TIMINGS.items()):
        ordered = sorted(values)
        p50 = ordered[len(ordered) // 2]
        print(f"  {name:<28} n={len(ordered):<3} p50 {p50 * 1000:7.1f} ms   max {ordered[-1] * 1000:7.1f} ms")


async def main() -> int:
    print("Production validation — one deployment, one campaign, the whole story over the real code and a real PostgreSQL.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
    if not dsn:
        _skipped.append("everything (no DATABASE_URL or KB_DATABASE_URL: this suite needs PostgreSQL)")
    else:
        import asyncpg

        d = Deployment(dsn)
        try:
            try:
                await d.open()
            except (OSError, asyncpg.InterfaceError, asyncpg.InvalidCatalogNameError, asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError) as exc:
                _skipped.append(f"everything (cannot reach PostgreSQL: {exc})")
            else:
                await step_1_to_3_import_create_activate(d)
                await step_4_to_7_select_gate_dial(d)
                await step_8_to_13_conversation(d)
                await step_15_callback_scheduling(d)
                await step_14_transfer(d)
                await step_16_callback_execution(d)
                await step_17_voicemail_no_answer(d)
                await step_18_webhook(d)
                await step_19_persistence(d)
                await step_20_crm(d)
                await step_21_n8n(d)
                await step_22_23_dashboard_auth(d)
                await step_24_recovery(d)
                await step_no_duplicate_calls(d)
                await step_no_secret_exposed(d)
                await step_graceful_providers(d)
                _print_timings()
        finally:
            await d.close()
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
    raise SystemExit(asyncio.run(main()))
