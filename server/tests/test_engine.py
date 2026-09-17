"""Phase 25 checks: the campaign execution engine inside the unified application.

What is checked, over the in-memory store the automation checks use and the
scripted carrier the scheduler checks use, with the real application, the
real engine, the real worker, dialer, gate and service — nothing ticked by
hand: the engine runs as it does in production, in the application's own
event loop, and the checks watch the rows through the application's routes.

* start a campaign through the API and the engine dials every contact, one
  after another, each exactly once, with the contact's ids on the handshake,
  writes each outcome, moves to the next, and completes the campaign;
* the progress route and the event stream report every counter as it moves;
* pause stops new calls while the call in progress ends normally; resume
  places the next; stop ends the campaign and the queue it never reached is
  reported as cancelled;
* a failed call is recorded with its reason and the campaign continues;
* the deployment's concurrency and a campaign's own ceiling hold;
* nobody is dialled twice; an explicit retry is the one way to dial again;
* a process that dies mid-call is replaced: the next engine adopts the call
  and follows it to its end;
* shutdown is bounded: no new calls, a short wait, then a hand-over;
* an application without a carrier serves with an idle engine;
* the boundary: the engine imports nothing from the conversation.

Run from `server/`:

    uv run python tests/test_engine.py

Exit status is non-zero when any check fails. PostgreSQL is needed only for the
last section (the ceiling and the counters in SQL); it is skipped without one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")

from loguru import logger  # noqa: E402

_failures: list[str] = []
_USERS: str | None = None
ADMIN_KEY = "engine-admin-key-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}", "X-Requested-With": "fetch"}
NUMBERS = ["+923001110001", "+923001110002", "+923001110003", "+923001110004"]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


class RealClock:
    """The scheduler runs on real time here; the store and the carrier follow it."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def advance(self, secs: float) -> None:  # the fake clock's surface, unused
        pass


class SharedStore:
    """The one store, handed to every part; `close` is a no-op so the parts may share it."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def close(self) -> None:
        return None


# --- Building the application with the engine ----------------------------------------------------


def build(*, engine: bool = True, provider: bool = True, auto_complete: bool = True, env: dict[str, str] | None = None):
    """The unified application over the in-memory store, the engine dialling through a scripted carrier."""
    from test_automation import FakeStore
    from test_security import _fake_collect
    from test_worker import ScriptedCarrier

    from src.app import create_unified_app
    from src.config import Config
    from src.dashboard import web
    from src.security import hash_password
    from src.telephony import CallStatus

    global _USERS
    if _USERS is None:
        _USERS = f"operator:operator:{hash_password('engine-operator-pw')}"
    os.environ.update(
        {
            "DASHBOARD_USERS": _USERS,
            "DASHBOARD_SESSION_SECRET": "engine-session-secret-0123456789abcdef0123456789",
            "AUTOMATION_API_KEYS": ADMIN_KEY,
            "DATABASE_URL": "postgresql://x:y@localhost/unused",
            "KB_DATABASE_URL": "postgresql://x:y@localhost/unused",
            "KB_ENABLED": "false",
            "SECURITY_API_RATE_LIMIT": "5000",
            "DEFAULT_PHONE_REGION": "PK",
            "MAX_CONCURRENT_CALLS": "1",
            "CALL_PACING_SECS": "0",
            "ENFORCE_CALLING_HOURS": "false",
            "WORKER_POLL_SECS": "0.5",
            "WORKER_IDLE_SECS": "1",
            "WORKER_HEARTBEAT_SECS": "1",
            "WORKER_STALE_SECS": "5",
            "WORKER_ADOPT_SECS": "1",
            "WORKER_SHUTDOWN_SECS": "1",
            "WORKER_REPORT_SECS": "5",
            "WORKER_RECOVERY_INTERVAL_SECS": "3600",
            "WORKER_AUTO_COMPLETE": "true" if auto_complete else "false",
            "WORKER_EMBEDDED": "true",
            "TELEPHONY_PUBLIC_URL": "https://bot.example.test",
            "TELEPHONY_FROM_NUMBER": "+15550001111",
        }
    )
    os.environ.update(env or {})
    os.environ.pop("DASHBOARD_AUTH_DISABLED", None)
    web.collect = _fake_collect
    config = Config.from_env()
    clock = RealClock()
    store = FakeStore(clock=clock)
    shared = SharedStore(store)
    carrier = ScriptedCarrier(clock, script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])

    async def factory() -> Any:
        return shared

    app = create_unified_app(
        config,
        store_factory=factory,
        bot_url="http://bot.test:7860",
        deliver=False,
        engine=engine,
        provider_factory=(lambda: carrier) if provider else None,
    )
    return app, store, carrier, config


def seed(client: Any, name: str, numbers: list[str]) -> tuple[int, list[int]]:
    """A campaign with these contacts, through the API. Returns (campaign id, prospect ids)."""
    ids = []
    for i, number in enumerate(numbers):
        r = client.post("/automation/api/v1/prospects", json={"first_name": f"Contact{i}", "last_name": name, "phone": number, "company": "Acme"}, headers=AUTH)
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["prospect"]["id"])
    r = client.post("/automation/api/v1/campaigns", json={"name": name}, headers=AUTH)
    assert r.status_code == 201, r.text
    campaign = r.json()["campaign"]["id"]
    r = client.post(f"/automation/api/v1/campaigns/{campaign}/prospects", json={"prospect_ids": ids}, headers=AUTH)
    assert r.status_code == 200, r.text
    return campaign, ids


def progress(client: Any, campaign: int) -> dict[str, Any]:
    r = client.get(f"/api/app/campaigns/{campaign}/progress", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def wait_for(client: Any, campaign: int, predicate: Any, *, timeout: float = 20.0, step: float = 0.2) -> dict[str, Any]:
    """Poll the progress route until `predicate(progress)` holds, or give up and return the last."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = progress(client, campaign)
        if predicate(last):
            return last
        time.sleep(step)
    return last


def transition(client: Any, campaign: int, action: str) -> dict[str, Any]:
    r = client.post(f"/automation/api/v1/campaigns/{campaign}/{action}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


# --- The checks ----------------------------------------------------------------------------------


def check_automatic_execution() -> None:
    print("\n=== a started campaign is executed, contact after contact, to completion ===")
    from fastapi.testclient import TestClient

    app, store, carrier, config = build()
    seen_live: list[int] = []

    async def note_live(queued: Any) -> None:
        seen_live.append(await store.count_live_attempts())

    store.after_reserve = note_live
    with TestClient(app) as client:
        status = client.get("/api/app/engine", headers=AUTH).json()
        check("the engine is running inside the application", status["state"] == "running", json.dumps(status)[:200])
        check("with a worker id, the scripted carrier and the deployment's limits", bool(status["worker_id"]) and status["provider"] == "scripted" and "max 1 concurrent" in (status["limits"] or ""), status.get("limits"))
        ready = client.get("/readyz")
        check("/readyz reports the engine", ready.status_code == 200 and any(c["name"] == "engine" and c["ok"] for c in ready.json()["checks"]), ready.text[:200])

        campaign, ids = seed(client, "Auto", NUMBERS[:3])
        before = progress(client, campaign)
        check("a draft campaign's progress: 3 contacts queued, nothing placed", before["contacts"] == 3 and before["queued"] == 3 and before["attempts"] == 0 and before["status"] == "DRAFT", json.dumps(before)[:200])
        time.sleep(1.5)
        check("a draft is never dialled", carrier.requests == [] and progress(client, campaign)["attempts"] == 0)

        transition(client, campaign, "start")
        final = wait_for(client, campaign, lambda p: p["finished"] and p["attempts"] >= 3, timeout=30)
        check("every contact was dialled, one after another, and the campaign completed itself", final["status"] == "COMPLETED" and final["attempts"] == 3 and final["answered"] == 3 and final["members_completed"] == 3, json.dumps(final)[:300])
        check("the counters at the end: 100%, nothing remaining, nothing cancelled", final["progress_pct"] == 100 and final["remaining"] == 0 and final["cancelled"] == 0 and final["queued"] == 0 and final["done"] == 3)
        check("each contact exactly once", sorted(r.to_number for r in carrier.requests) == sorted(NUMBERS[:3]), str([r.to_number for r in carrier.requests]))
        check("never two calls at once (MAX_CONCURRENT_CALLS=1)", seen_live and max(seen_live) == 1, str(seen_live))
        params = carrier.requests[0].parameters
        check("the contact, campaign, attempt and trace ids rode on the handshake", all(k in params for k in ("prospect_id", "campaign_id", "call_attempt_id", "trace_id")) and params["campaign_id"] == str(campaign), str(sorted(params)))
        attempts = client.get(f"/automation/api/v1/calls?campaign_id={campaign}&limit=10", headers=AUTH).json()["calls"]
        check("the calls list shows three completed calls with a carrier id each", len(attempts) == 3 and all(a["status"] == "COMPLETED" and a.get("telephony_call_id") for a in attempts), str([(a["status"], a.get("telephony_provider")) for a in attempts]))
        rows = store.attempts.values()
        check("every attempt row has a carrier call id, a worker and a trace", all(a.telephony_call_id and a.worker_id and a.trace_id for a in rows if a.campaign_id == campaign))
        members = client.get(f"/automation/api/v1/campaigns/{campaign}/prospects", headers=AUTH).json()["members"]
        check("every membership is COMPLETED with one attempt", all(m["membership"]["status"] == "COMPLETED" and m["membership"]["attempt_count"] == 1 for m in members))
        time.sleep(1.5)
        check("and nothing is dialled again afterwards", len(carrier.requests) == 3)


def check_pause_resume_stop() -> None:
    print("\n=== pause, resume, stop ===")
    from fastapi.testclient import TestClient

    from src.telephony import CallStatus

    app, store, carrier, config = build()
    # Longer calls: the worker asks the carrier once per poll, so four ANSWERED steps keeps a call up for a few seconds.
    carrier.script = [CallStatus.RINGING] + [CallStatus.ANSWERED] * 4 + [CallStatus.COMPLETED]
    with TestClient(app) as client:
        campaign, ids = seed(client, "Controls", NUMBERS[:3])
        transition(client, campaign, "start")
        live = wait_for(client, campaign, lambda p: p["calling"] + p["connected"] + p["reserved"] >= 1, timeout=10)
        check("the first call is in progress", live["attempts"] == 1 and live["in_progress"] == 1, json.dumps(live)[:200])
        paused = transition(client, campaign, "pause")
        check("pause is accepted while a call is up", paused["campaign"]["status"] == "PAUSED")
        ended = wait_for(client, campaign, lambda p: p["answered"] >= 1 and p["in_progress"] == 0, timeout=40)
        check("the call in progress ended normally under the pause", ended["answered"] == 1 and ended["completed"] == 1 and ended["members_completed"] == 1, json.dumps(ended)[:200])
        time.sleep(2.0)
        after = progress(client, campaign)
        check("and no new call started while paused", after["attempts"] == 1 and len(carrier.requests) == 1 and after["queued"] == 2, json.dumps(after)[:200])
        engine = client.get("/api/app/engine", headers=AUTH).json()
        check("the engine stays running with nothing in flight", engine["state"] == "running" and engine["in_flight"] == [])

        transition(client, campaign, "resume")
        second = wait_for(client, campaign, lambda p: p["attempts"] >= 2, timeout=10)
        check("resume places the next contact", second["attempts"] == 2 and second["in_progress"] == 1, json.dumps(second)[:200])
        stopped = transition(client, campaign, "complete")
        check("stop (complete) is accepted with a call up", stopped["campaign"]["status"] == "COMPLETED")
        done = wait_for(client, campaign, lambda p: p["answered"] >= 2 and p["in_progress"] == 0, timeout=40)
        check("the call in progress ended normally after the stop", done["answered"] == 2 and done["in_progress"] == 0, json.dumps(done)[:200])
        time.sleep(2.0)
        final = progress(client, campaign)
        check("the third contact was never dialled and is reported as cancelled", final["attempts"] == 2 and final["cancelled"] == 1 and final["finished"] and final["remaining"] == 0, json.dumps(final)[:200])
        check("the carrier saw exactly two calls", len(carrier.requests) == 2)


def check_failure_and_continue() -> None:
    print("\n=== a failed call is recorded and the campaign continues ===")
    from fastapi.testclient import TestClient

    from src.telephony import CallStatus

    app, store, carrier, config = build()
    carrier.scripts_by_number[NUMBERS[0]] = [CallStatus.RINGING, CallStatus.FAILED]
    carrier.scripts_by_number[NUMBERS[1]] = [CallStatus.RINGING, CallStatus.NO_ANSWER]
    with TestClient(app) as client:
        campaign, ids = seed(client, "Failures", NUMBERS[:3])
        transition(client, campaign, "start")
        final = wait_for(client, campaign, lambda p: p["attempts"] >= 3 and p["in_progress"] == 0, timeout=20)
        check("all three were attempted", final["attempts"] == 3, json.dumps(final)[:300])
        check("one failed, one no-answer, one completed — each counted under its own name", final["failed"] == 1 and final["no_answer"] == 1 and final["completed"] == 1 and final["answered"] == 1, json.dumps(final)[:300])
        failed = [a for a in store.attempts.values() if a.campaign_id == campaign and a.status.value == "FAILED"]
        check("the failed attempt carries a reason", failed and bool(failed[0].failure_reason), failed[0].failure_reason if failed else "none")
        members = {m["prospect"]["phone_normalized"]: m["membership"] for m in client.get(f"/automation/api/v1/campaigns/{campaign}/prospects", headers=AUTH).json()["members"]}
        check("the failed number is exhausted (no retry for a bad number); the no-answer is queued again for later", members[NUMBERS[0]]["status"] == "EXHAUSTED" and members[NUMBERS[1]]["status"] == "PENDING" and members[NUMBERS[1]]["next_attempt_at"] is not None, str({k: v["status"] for k, v in members.items()}))
        check("the campaign stays ACTIVE while a retry is scheduled", final["status"] == "ACTIVE" and final["scheduled"] == 1, final["status"])


def check_concurrency() -> None:
    print("\n=== concurrency: the deployment's limit and a campaign's own ceiling ===")
    from fastapi.testclient import TestClient

    from src.telephony import CallStatus

    app, store, carrier, config = build(env={"MAX_CONCURRENT_CALLS": "3"})
    carrier.script = [CallStatus.RINGING] + [CallStatus.ANSWERED] * 4 + [CallStatus.COMPLETED]
    peak: dict[int, int] = {}

    async def note(queued: Any) -> None:
        live = await store.count_live_attempts(campaign_id=queued.campaign.id)
        peak[queued.campaign.id] = max(peak.get(queued.campaign.id, 0), live)

    store.after_reserve = note
    with TestClient(app) as client:
        capped, _ = seed(client, "Capped", NUMBERS[:3])
        r = client.put(f"/automation/api/v1/campaigns/{capped}/configuration", json={"max_concurrent_calls": 1}, headers=AUTH)
        check("a campaign's own ceiling is accepted by the configuration route", r.status_code == 200 and "max_concurrent_calls" in r.json()["changed"], r.text[:120])
        transition(client, capped, "start")
        final = wait_for(client, capped, lambda p: p["finished"], timeout=40)
        check("under a ceiling of 1 the calls ran one at a time although the deployment allows 3", final["attempts"] == 3 and peak.get(capped) == 1, f"peak={peak.get(capped)}")

        open_, _ = seed(client, "Open", NUMBERS[:3])
        transition(client, open_, "start")
        final = wait_for(client, open_, lambda p: p["finished"], timeout=40)
        check("without one, the deployment's limit of 3 lets the calls overlap", final["attempts"] == 3 and peak.get(open_, 0) >= 2, f"peak={peak.get(open_)}")
        r = client.put(f"/automation/api/v1/campaigns/{open_}/configuration", json={"max_concurrent_calls": 0}, headers=AUTH)
        check("0 clears the ceiling", r.status_code == 200 and r.json()["campaign"]["configuration"].get("max_concurrent_calls") is None, r.text[:160])


def check_duplicates_and_retry() -> None:
    print("\n=== nobody twice, unless asked ===")
    from fastapi.testclient import TestClient

    app, store, carrier, config = build(auto_complete=False)
    with TestClient(app) as client:
        campaign, ids = seed(client, "Once", NUMBERS[:2])
        transition(client, campaign, "start")
        final = wait_for(client, campaign, lambda p: p["answered"] >= 2, timeout=20)
        check("both contacts reached", final["answered"] == 2 and final["status"] == "ACTIVE")
        time.sleep(2.5)
        check("a campaign left ACTIVE does not dial a reached contact again", len(carrier.requests) == 2 and progress(client, campaign)["attempts"] == 2)

        r = client.post(f"/automation/api/v1/campaigns/{campaign}/prospects/{ids[0]}/retry", headers=AUTH)
        check("an explicit retry is accepted and becomes a callback due now", r.status_code in (200, 201, 202) and r.json().get("callback"), r.text[:160])
        again = wait_for(client, campaign, lambda p: p["attempts"] >= 3, timeout=15)
        check("the retried contact is dialled once more, ahead of the queue and past the attempt limit", again["attempts"] == 3 and carrier.requests[-1].to_number == NUMBERS[0] and carrier.calls_to(NUMBERS[0]) == 2 and carrier.calls_to(NUMBERS[1]) == 1, str([r.to_number for r in carrier.requests]))
        done = wait_for(client, campaign, lambda p: p["answered"] >= 3, timeout=15)
        check("and reached again; the other contact was not touched", done["answered"] == 3 and carrier.calls_to(NUMBERS[1]) == 1)
        r = client.post(f"/automation/api/v1/campaigns/{campaign}/prospects/999999/retry", headers=AUTH)
        check("a retry for an unknown contact is refused", r.status_code == 404, r.text[:100])


def check_restart_recovery() -> None:
    print("\n=== a process that dies mid-call is replaced ===")
    from fastapi.testclient import TestClient

    from src.telephony import CallStatus

    app, store, carrier, config = build()
    carrier.script = [CallStatus.RINGING] + [CallStatus.ANSWERED] * 30 + [CallStatus.COMPLETED]
    with TestClient(app) as client:
        campaign, ids = seed(client, "Crash", NUMBERS[:2])
        transition(client, campaign, "start")
        live = wait_for(client, campaign, lambda p: p["in_progress"] >= 1 and p["attempts"] == 1, timeout=10)
        check("the first call is up", live["in_progress"] == 1)
        engine = app.state.engine
        task = engine._task
        first_worker = engine.status()["worker_id"]
        # The crash: the loop is killed without deregistering — the heartbeat
        # row stays, the attempt row stays live, nobody is following it.
        worker = engine.worker

        async def _no_finish() -> None:  # a crash deregisters nothing and hands nothing over
            return None

        worker.finish = _no_finish
        task.get_loop().call_soon_threadsafe(task.cancel)
        deadline = time.monotonic() + 5
        while not task.done() and time.monotonic() < deadline:
            time.sleep(0.05)
        check("the loop is gone", task.done())
        status = client.get("/api/app/engine", headers=AUTH).json()
        check("the application reports the engine stopped, and still serves", status["state"] in ("stopped", "failed") and client.get("/api/app/session", headers=AUTH).status_code == 200, status["state"])
    still = [a for a in store.attempts.values() if a.campaign_id == campaign]
    check("the attempt row is still live, owned by the dead worker", len(still) == 1 and still[0].status.is_live and still[0].worker_id == first_worker, f"{still[0].status.value if still else None} {still[0].worker_id if still else None}")

    # The replacement: the same store, a new process (a new application).
    app2 = None
    from src.app import create_unified_app

    async def factory() -> Any:
        return SharedStore(store)

    app2 = create_unified_app(config, store_factory=factory, bot_url="http://bot.test:7860", deliver=False, engine=True, provider_factory=lambda: carrier)
    with TestClient(app2) as client:
        second_worker = client.get("/api/app/engine", headers=AUTH).json()["worker_id"]
        check("a new worker, a new name", second_worker and second_worker != first_worker, f"{first_worker} -> {second_worker}")
        adopted = wait_for(client, campaign, lambda p: any(a.worker_id == second_worker for a in store.attempts.values() if a.campaign_id == campaign), timeout=15)
        owner = [a.worker_id for a in store.attempts.values() if a.campaign_id == campaign][0]
        check("the new engine adopts the abandoned call once its owner is stale (no redial)", owner == second_worker and len(carrier.requests) == 1, f"owner={owner} requests={len(carrier.requests)}")
        final = wait_for(client, campaign, lambda p: p["finished"], timeout=40)
        check("follows it to its end, dials the second contact, and completes the campaign", final["status"] == "COMPLETED" and final["answered"] == 2 and final["attempts"] == 2, json.dumps(final)[:200])


def check_graceful_shutdown() -> None:
    print("\n=== shutdown: no new calls, a bounded wait, then a hand-over ===")
    from fastapi.testclient import TestClient

    from src.telephony import CallStatus

    app, store, carrier, config = build()
    carrier.script = [CallStatus.RINGING] + [CallStatus.ANSWERED] * 30 + [CallStatus.COMPLETED]
    started = None
    with TestClient(app) as client:
        campaign, ids = seed(client, "Shutdown", NUMBERS[:2])
        transition(client, campaign, "start")
        wait_for(client, campaign, lambda p: p["in_progress"] >= 1, timeout=10)
        started = time.monotonic()
    elapsed = time.monotonic() - started
    check("the application stopped within the shutdown wait plus the hand-over (WORKER_SHUTDOWN_SECS=1)", elapsed < 15, f"{elapsed:.1f}s")
    status = app.state.engine.status()
    check("the engine reports stopped", status["state"] == "stopped", status["state"])
    live = [a for a in store.attempts.values() if a.campaign_id == campaign and a.status.is_live]
    check("the call in progress was handed over, not failed: its row is still live for the next worker", len(live) == 1, str([a.status.value for a in store.attempts.values()]))
    check("the second contact was not dialled during the shutdown", len(carrier.requests) == 1)
    workers = asyncio.run(store.list_workers(include_stopped=True))
    mine = [w for w in workers if w.worker_id == status["worker_id"]] if workers and hasattr(workers[0], "worker_id") else []
    check("the worker deregistered (status stopped) so its work is adoptable at once", not mine or str(getattr(mine[0], "status", "")).lower() in ("stopped", "draining"), str(getattr(mine[0], "status", "?")) if mine else "no row")


def check_without_carrier() -> None:
    print("\n=== no carrier: the application serves, the engine is idle, nothing dials ===")
    from fastapi.testclient import TestClient

    for name in ("TELEPHONY_PROVIDER", "TELEPHONY_FROM_NUMBER", "TELEPHONY_PUBLIC_URL", "TWILIO_ACCOUNT_SID", "SIGNALWIRE_PROJECT_ID"):
        os.environ.pop(name, None)
    app, store, carrier, config = build(provider=False)
    with TestClient(app) as client:
        status = client.get("/api/app/engine", headers=AUTH).json()
        check("the engine is idle with a reason", status["state"] == "idle" and bool(status["reason"]), status.get("reason"))
        check("/readyz is still ready (idle is what was asked for)", client.get("/readyz").status_code == 200)
        campaign, ids = seed(client, "NoCarrier", NUMBERS[:1])
        transition(client, campaign, "start")
        time.sleep(1.5)
        check("a started campaign is not dialled", progress(client, campaign)["attempts"] == 0 and carrier.requests == [])
    app, store, carrier, config = build(engine=False)
    with TestClient(app) as client:
        status = client.get("/api/app/engine", headers=AUTH).json()
        check("engine=False (--no-engine / WORKER_EMBEDDED=false) reports off", status["state"] == "off" and "campaign.py run" in (status["reason"] or ""), status.get("reason"))


def check_stream() -> None:
    print("\n=== the event stream ===")
    from fastapi.testclient import TestClient

    app, store, carrier, config = build()
    with TestClient(app) as client:
        campaign, ids = seed(client, "Stream", NUMBERS[:2])
        events: list[tuple[str, dict[str, Any]]] = []
        # The test client serves one request at a time, so the campaign is started before the stream is opened.
        transition(client, campaign, "start")
        with client.stream("GET", f"/api/app/stream?campaign={campaign}&max_secs=6&poll_secs=0.2", headers=AUTH) as response:
            check("the stream is server-sent events", response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream"), response.headers.get("content-type"))
            name = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    events.append((name, json.loads(line[6:])))
                    name = None
                if any(n == "campaign" and d.get("finished") for n, d in events):
                    break
        names = [n for n, _ in events]
        check("hello, then the engine, then the campaign", names[:3] == ["hello", "engine", "campaign"], str(names[:6]))
        moves = [d for n, d in events if n == "campaign"]
        check("progress events arrive as the calls move: running → completed", any(d["status"] == "ACTIVE" for d in moves) and moves[-1]["status"] == "COMPLETED" and moves[-1]["answered"] == 2 and len(moves) >= 3, str([(d["status"], d["attempts"], d["answered"]) for d in moves]))
        check("only changes are sent (no two identical consecutive campaign events)", all(json.dumps({k: v for k, v in a.items() if k != "updated_at"}, sort_keys=True) != json.dumps({k: v for k, v in b.items() if k != "updated_at"}, sort_keys=True) for a, b in zip(moves, moves[1:])))
        check("a stranger gets 401, not a stream", client.get("/api/app/stream").status_code == 401)
        r = client.get(f"/api/app/campaigns/999999/progress", headers=AUTH)
        check("progress of an unknown campaign is 404", r.status_code == 404)


def check_boundary() -> None:
    print("\n=== the boundary ===")
    import re

    engine_src = (SERVER / "src" / "app" / "engine.py").read_text(encoding="utf-8")
    runtime_src = (SERVER / "src" / "campaigns" / "runtime.py").read_text(encoding="utf-8")
    check("the engine imports nothing from the conversation, the bot, or the dashboard", not re.search(r"from \.\.(conversation|dashboard)|import bot\b", engine_src + runtime_src))
    check("the CLI assembles the scheduler through the same builder", "build_worker(" in (SERVER / "campaign.py").read_text(encoding="utf-8"))


def check_sql_ceiling() -> None:
    print("\n=== the campaign's ceiling and the progress counters in SQL, against PostgreSQL ===")
    from dotenv import load_dotenv

    # `build()` pointed the application at a database that does not exist;
    # the real one is whatever `.env` says (it may name only KB_DATABASE_URL).
    for name in ("DATABASE_URL", "KB_DATABASE_URL"):
        os.environ.pop(name, None)
    load_dotenv(SERVER / ".env", override=True)
    dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
    if not dsn:
        print("  SKIP  no DATABASE_URL or KB_DATABASE_URL")
        return

    async def run() -> None:
        import uuid

        from test_campaigns import with_temp_schema

        from src.campaigns import CampaignService, CampaignStatus

        try:
            store, admin, schema = await with_temp_schema(dsn)
        except Exception as exc:  # noqa: BLE001 - no database here is a skip, not a failure
            print(f"  SKIP  cannot reach PostgreSQL: {(str(exc).splitlines() or [type(exc).__name__])[0]}")
            return
        try:
            service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60)
            campaign = await service.create_campaign(f"Ceiling {uuid.uuid4().hex[:6]}")
            await service.set_status(campaign.id, CampaignStatus.ACTIVE)
            for n in range(1, 4):
                prospect = await service.create_prospect(first_name="Row", last_name=str(n), phone=f"0301 {n:07d}")
                await store.add_to_campaign(campaign.id, prospect.id)
            draft = await store.campaign_progress(campaign.id)
            check("campaign_progress before any call: 3 contacts, 3 queued, nothing placed", draft["contacts"] == 3 and draft["queued"] == 3 and draft["attempts"] == 0 and draft["live"] == 0, json.dumps(draft))
            await store.update_campaign_configuration(campaign.id, "max_concurrent_calls", 1)
            first = await service.next_call(campaign.id, max_concurrent=0, worker_id="a")
            second = await service.next_call(campaign.id, max_concurrent=0, worker_id="b")
            check("under a ceiling of 1 the second reservation is refused while the first is live", first is not None and second is None)
            await store.update_campaign_configuration(campaign.id, "max_concurrent_calls", 2)
            third = await service.next_call(campaign.id, max_concurrent=0, worker_id="b")
            fourth = await service.next_call(campaign.id, max_concurrent=0, worker_id="c")
            check("raising it to 2 lets one more through, and no more", third is not None and fourth is None)
            await store.update_campaign_configuration(campaign.id, "max_concurrent_calls", "nonsense")
            fifth = await service.next_call(campaign.id, max_concurrent=0, worker_id="d")
            check("nonsense in the JSON means no ceiling of its own", fifth is not None)
            live = await store.campaign_progress(campaign.id)
            # `queued` counts the reservations too (reserved, not yet ringing), so it stays 3.
            check("campaign_progress counts the reservations: 3 reserved, 3 live, 3 in progress, nothing pending", live["reserved"] == 3 and live["live"] == 3 and live["in_progress"] == 3 and live["pending"] == 0 and live["queued"] == 3 and live["attempts"] == 3, json.dumps(live))
        finally:
            await store.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(run())


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    check_automatic_execution()
    check_pause_resume_stop()
    check_failure_and_continue()
    check_concurrency()
    check_duplicates_and_retry()
    check_restart_recovery()
    check_graceful_shutdown()
    check_without_carrier()
    check_stream()
    check_boundary()
    check_sql_ceiling()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
