"""Production validation: everything automated, in one run, into one report. Phase 23.

Run it from the `server/` directory::

    uv run validate.py                      # config hygiene, health, posture, the 22 check scripts
    uv run validate.py --skip-tests         # only the parts that take seconds
    uv run validate.py --evals              # also the two eval suites (vendor keys, a working TTS; minutes)
    uv run validate.py measure              # figures from the rows: success rate, latency, duplicates, cost
    uv run validate.py live --to +9230...   # the controlled real-phone test; dials only with --dial --yes

**What it is for.** The twenty-two check scripts each prove one phase; the
health check proves the vendors answer; `security.py check` proves the
posture; `tests/test_production.py` proves the whole story over a real
PostgreSQL. This runs all of them, reads the numbers the rows already hold,
and writes `validation-report.md` (and `.json`) with one line per
requirement of the go-live list — **verified**, **failed**, or **requires
manual verification** — so the readiness document is written from evidence
and not from memory.

**What it will not do.** Place a call, book a meeting, file a contact or
post to n8n on its own: every one of those reaches a real vendor and a real
person, and each is listed as a manual step with the exact command. `live`
prints the procedure and refuses to dial without both `--dial` and `--yes`.

Exit status: 0 when every automated check passed (manual items are listed,
not counted); 1 when any failed; 2 when the configuration could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SERVER = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER))
load_dotenv(SERVER / ".env", override=True)

from src.config import Config, ConfigError  # noqa: E402
from src.reliability import Status, check_health, configure_logging  # noqa: E402

configure_logging(level="WARNING", component="validate")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CONFIG = 2

REPORT_MD = SERVER / "validation-report.md"
REPORT_JSON = SERVER / "validation-report.json"

#: Every check script, and the phase it stands for.
SCRIPTS = (
    ("test_conversation", "the sales conversation (Phase 6)"),
    ("test_results", "the call result (Phase 8)"),
    ("test_reliability", "failure injection (Phase 9)"),
    ("test_performance", "usage, cost, pooling, concurrency (Phase 11)"),
    ("test_actions", "the tools (Phase 7)"),
    ("test_scheduling", "the calendar (Phase 7)"),
    ("test_knowledge", "retrieval (Phase 3)"),
    ("test_telephony", "the carriers (Phase 4)"),
    ("test_realtime", "turn-taking wiring (Phase 2)"),
    ("test_campaigns", "prospects, campaigns, the queue (Phase 5)"),
    ("test_voice_quality", "turn monitoring, voicemail (Phase 12)"),
    ("test_worker", "the scheduler (Phase 13)"),
    ("test_webhooks", "carrier webhooks (Phase 14)"),
    ("test_crm", "CRM sync (Phase 15)"),
    ("test_booking_transfer", "booking and transfer (Phase 16)"),
    ("test_automation", "the n8n API and outbox (Phase 17)"),
    ("test_security", "authentication, authorisation, hardening (Phase 18)"),
    ("test_compliance", "compliance controls (Phase 19)"),
    ("test_dashboard", "the dashboard (Phases 10, 20)"),
    ("test_scaling", "multi-worker scaling (Phase 21)"),
    ("test_monitoring", "monitoring and observability (Phase 22)"),
    ("test_production", "the end-to-end story over PostgreSQL (Phase 23)"),
    ("test_app", "the unified application (Phase 24)"),
    ("test_engine", "the campaign execution engine (Phase 25)"),
    ("test_spoken_text", "what the caller may hear (Phase 26)"),
    ("test_latency", "per-turn latency instrumentation (Phase 29)"),
    ("test_tts_fallback", "the TTS fallback (Phase 30)"),
    ("test_tool_advertising", "the prompt budget and per-stage tool advertising (Phase 31)"),
    ("test_tool_round_trips", "which tools need a second LLM request (Phase 32)"),
    ("test_client_theme", "the browser client in the application's design (Phase 33)"),
    ("test_knowledge_index", "the in-memory knowledge index and the retrieval timeout (Phase 37)"),
)

#: The go-live list, each item with what proves it automatically and what a
#: person still has to do. `auto` names sections of the evidence gathered
#: below; `manual` is a sentence with the command in it.
REQUIREMENTS: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("1. CSV/prospect import", ("test_campaigns", "test_automation", "test_production"), None),
    ("2. Campaign creation", ("test_campaigns", "test_automation", "test_production"), None),
    ("3. Campaign activation", ("test_campaigns", "test_worker", "test_production"), None),
    ("4. Automatic prospect selection", ("test_worker", "test_scaling", "test_production"), None),
    ("5. DNC enforcement", ("test_compliance", "test_worker", "test_production"), None),
    ("6. Calling-hour enforcement", ("test_reliability", "test_worker", "test_production"), None),
    ("7. Automatic outbound call", ("test_worker", "test_production"), "one real call through the configured carrier: `uv run validate.py live --to <your phone>` (no call has ever been answered on this system)"),
    ("8. Human conversation", ("test_conversation", "test_production"), "the sales eval suite against the deployed model: `uv run validate.py --evals`, then a real call"),
    ("9. Barge-in", ("test_voice_quality", "test_production"), "the audio eval suite (`evals/suite.yaml`) and `tests/phone_drill.py barge_in` against a running bot; a real handset"),
    ("10. Knowledge-base retrieval", ("test_knowledge", "test_production"), "`uv run ingest.py add <your documents>` then `evals/sales/unknown_question.yaml` against the deployed model"),
    ("11. Qualification", ("test_conversation", "test_results", "test_production"), "read a real call's result: `uv run campaign.py result <attempt>`"),
    ("12. Objection handling", ("test_conversation", "test_production"), "`evals/sales/price_objection.yaml` against the deployed model"),
    ("13. Meeting booking", ("test_actions", "test_booking_transfer", "test_scheduling", "test_production"), "one real Cal.com booking on a real call (`uv run health.py calendar` first); no live booking has been observed"),
    ("14. Human transfer", ("test_booking_transfer", "test_production"), "one real transfer to `TELEPHONY_TRANSFER_NUMBER`, then `uv run campaign.py transfers`; no live transfer has been observed"),
    ("15. Callback scheduling", ("test_actions", "test_worker", "test_production"), "`evals/sales/callback.yaml` against the deployed model"),
    ("16. Callback execution", ("test_worker", "test_production"), "a scheduled callback placed by `campaign.py run` at its time, on a real carrier"),
    ("17. Voicemail/no-answer handling", ("test_voice_quality", "test_worker", "test_production"), "`tests/phone_drill.py voicemail`, then one real call to a voicemail with `TELEPHONY_MACHINE_DETECTION=async`"),
    ("18. Call completion webhook", ("test_webhooks", "test_scaling", "test_production"), "one real delivery from the carrier: `uv run campaign.py webhooks` after the first real call (needs `TELEPHONY_PUBLIC_URL` and the signing key)"),
    ("19. Database persistence", ("test_campaigns", "test_dashboard", "test_production", "measure"), None),
    ("20. CRM synchronization", ("test_crm", "test_production"), "one real filing: `CRM_PROVIDER=hubspot`, `uv run health.py crm`, `uv run campaign.py crm-sync --once`, `crm-status`; no real HubSpot filing has been observed"),
    ("21. n8n workflow", ("test_automation", "test_production"), "import `n8n/workflows/04-qualified-lead-notification.json` into a live n8n, activate it, `uv run automation.py --once`, `uv run campaign.py events`"),
    ("22. Dashboard visibility", ("test_dashboard", "test_security", "test_production"), "open `uv run dashboard.py` in a browser behind TLS and check one real call's detail page"),
    ("23. Authentication/authorization", ("test_security", "test_production", "posture"), "sign in over HTTPS behind the real proxy (`SECURITY_REQUIRE_HTTPS=true`) and confirm the `Secure` cookie and the redirect"),
    ("24. Retry/recovery behavior", ("test_reliability", "test_scaling", "test_worker", "test_production"), "kill one of two `campaign.py run` processes mid-call and watch the other adopt it (`campaign.py workers`)"),
    ("25. Cost/usage tracking", ("test_performance", "test_production", "measure"), "set `COST_*` rates and read one real call's cost on its detail page"),
    ("No duplicate calls", ("test_reliability", "test_scaling", "test_performance", "test_production", "measure"), "after the first real campaign: `uv run validate.py measure` reports zero prospects with two live attempts"),
    ("No unauthorized dashboard/API access", ("test_security", "test_production", "posture"), None),
    ("Secrets not exposed", ("test_security", "test_monitoring", "test_production", "posture"), "rotate every key that was in `server/.env` while it was tracked, if that repository was ever pushed"),
    ("Graceful failure of external providers", ("test_reliability", "test_booking_transfer", "test_crm", "test_automation", "test_production"), None),
    ("Latency", ("test_production",), "measured on real audio only: `tests/phone_drill.py all` against a running bot and one real call (`validate.py live`); the last measured figures are in HANDOFF.md §10"),
    ("Call success rate", ("measure",), "needs real calls: `uv run validate.py measure` after the first campaign"),
    ("Health, readiness, metrics", ("test_monitoring", "health"), "point a Prometheus at every port and confirm `aiva_up` per role; probe `/readyz` from the orchestrator"),
)


@dataclass
class Finding:
    """One automated check's outcome."""

    section: str
    name: str
    status: str  # pass | fail | warn | skip
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"section": self.section, "name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class Evidence:
    findings: list[Finding] = field(default_factory=list)
    scripts: dict[str, dict[str, Any]] = field(default_factory=dict)
    measures: dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.time)

    def add(self, section: str, name: str, status: str, detail: str = "") -> None:
        self.findings.append(Finding(section, name, status, detail))

    @property
    def failed(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "fail"]

    def section_ok(self, section: str) -> bool | None:
        """True when every finding in a section passed, False on a failure, None when nothing ran."""
        rows = [f for f in self.findings if f.section == section and f.status in ("pass", "fail", "warn")]
        if not rows:
            return None
        return not any(f.status == "fail" for f in rows)


# --- Configuration hygiene ----------------------------------------------------------------


def check_config(config: Config, evidence: Evidence) -> None:
    from security import _env_duplicates, _env_tracked

    duplicates = _env_duplicates()
    evidence.add("config", ".env defines every variable once", "warn" if duplicates else "pass", f"defined more than once, last line wins: {', '.join(duplicates)}" if duplicates else "")
    tracked = _env_tracked()
    evidence.add("config", "server/.env is not tracked by git", "fail" if tracked else ("pass" if tracked is False else "skip"), "rotate every key it ever held" if tracked else "")
    evidence.add("config", f"providers: {config.stt_provider} / {config.llm_provider}:{config.llm_model} / {config.tts_provider}", "pass")
    telephony = config.telephony
    evidence.add("config", f"carrier: {telephony.provider}", "pass" if telephony.is_configured else "warn", "" if telephony.is_configured else "credentials, TELEPHONY_FROM_NUMBER or TELEPHONY_PUBLIC_URL missing: no outbound call can be placed")
    evidence.add("config", "carrier webhooks verifiable", "pass" if telephony.can_verify_webhooks else "warn", "" if telephony.can_verify_webhooks else telephony.describe_webhooks())
    evidence.add("config", f"calendar: {config.calendar.provider}", "pass" if config.calendar.enabled else "warn", "" if config.calendar.enabled else "no calendar: the agent cannot book")
    evidence.add("config", f"CRM: {config.crm.provider}", "pass" if config.crm.enabled else "warn", "" if config.crm.enabled else "no CRM: results are not filed anywhere")
    evidence.add("config", "automation API keys", "pass" if config.automation.api_enabled else "warn", "" if config.automation.api_enabled else "AUTOMATION_API_KEYS unset: automation.py refuses to serve")
    evidence.add("config", "n8n delivery URL", "pass" if config.automation.delivery_enabled else "warn", "" if config.automation.delivery_enabled else "no AUTOMATION_WEBHOOK_URL: events are not delivered")
    evidence.add("config", "dashboard users", "pass" if config.security.users else "warn", "" if config.security.users else "DASHBOARD_USERS unset: dashboard.py refuses to start")
    evidence.add("config", "HTTPS required", "pass" if config.security.require_https else "warn", "" if config.security.require_https else "SECURITY_REQUIRE_HTTPS unset: fine on loopback, not behind a public address")
    evidence.add("config", "compliance jurisdictions", "pass" if config.compliance.jurisdictions else "warn", "" if config.compliance.jurisdictions else "none configured: the operator decides the rules per country (COMPLIANCE.md)")
    evidence.add("config", "cost rates", "pass" if config.cost.llm_input_per_mtok is not None or config.cost.telephony_per_minute is not None else "warn", "" if config.cost.llm_input_per_mtok is not None else "no COST_* rates: usage is measured, cost is not estimated")
    evidence.add("config", "monitoring", "pass" if config.monitoring.enabled else "warn", config.monitoring.describe())


# --- Health and posture --------------------------------------------------------------------


async def check_vendors(config: Config, evidence: Evidence) -> None:
    report = await check_health(config, timeout_secs=config.reliability.health_timeout_secs)
    for component in report.components:
        status = {"ok": "pass", "degraded": "warn", "failed": "fail", "skipped": "skip"}[component.status.value]
        evidence.add("health", component.name, status, component.detail)


def check_posture(evidence: Evidence) -> None:
    result = subprocess.run([sys.executable, str(SERVER / "security.py"), "check"], cwd=SERVER, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    for line in result.stdout.splitlines():
        match = re.match(r"^\s+(OK|WARN|FAIL)\s+(.*)$", line)
        if match:
            level, text = match.groups()
            evidence.add("posture", text.strip(), {"OK": "pass", "WARN": "warn", "FAIL": "fail"}[level])
    if result.returncode not in (0, 2) or not result.stdout:
        evidence.add("posture", "security.py check ran", "fail", (result.stderr or result.stdout)[-300:])


# --- The check scripts ------------------------------------------------------------------------


async def run_scripts(evidence: Evidence, *, only: set[str] | None = None) -> None:
    async def run_one(name: str, what: str) -> None:
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(SERVER / "tests" / f"{name}.py"),
            cwd=SERVER, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        raw, _ = await process.communicate()
        text = raw.decode("utf-8", errors="replace")
        passed = sum(1 for line in text.splitlines() if line.startswith("  PASS"))
        failed = [line.strip()[6:] for line in text.splitlines() if line.startswith("  FAIL")]
        skipped = [line.strip()[2:] for line in text.splitlines() if line.startswith("  - ") and "SKIPPED" in text]
        evidence.scripts[name] = {"what": what, "exit": process.returncode, "passed": passed, "failed": failed, "skipped": skipped, "secs": round(time.monotonic() - started, 1)}
        status = "pass" if process.returncode == 0 and not failed else "fail"
        detail = f"{passed} passed" + (f", {len(failed)} FAILED: {failed[0]}" if failed else "") + (f", skipped: {skipped[0]}" if skipped else "") + f" ({evidence.scripts[name]['secs']}s)"
        if process.returncode != 0 and not failed:
            detail = f"exit {process.returncode}: " + text.strip().splitlines()[-1][:200] if text.strip() else f"exit {process.returncode}"
        evidence.add(name, what, status, detail)

    wanted = [(n, w) for n, w in SCRIPTS if only is None or n in only]
    await asyncio.gather(*(run_one(n, w) for n, w in wanted))


async def run_evals(evidence: Evidence) -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "SESSION_IDLE_TIMEOUT_SECS": "3600", "USER_IDLE_TIMEOUT_SECS": "120"}
    for suite, what in (("evals/sales/suite.yaml", "the sales conversations, text mode (Phase 6)"), ("evals/suite.yaml", "the voice agent, audio mode (Phase 2)")):
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "pipecat.evals", "suite", suite, cwd=SERVER, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        raw, _ = await process.communicate()
        text = raw.decode("utf-8", errors="replace")
        tail = text.strip().splitlines()[-1][:200] if text.strip() else ""
        evidence.add("evals", f"{suite}: {what}", "pass" if process.returncode == 0 else "fail", f"{tail} ({time.monotonic() - started:.0f}s)")


# --- Figures from the rows --------------------------------------------------------------------


async def measure(config: Config, evidence: Evidence) -> dict[str, Any]:
    """Success rate, latency, duplicates and cost, from what the rows hold. Read-only."""
    from src.campaigns import ATTEMPTS_TABLE, CampaignStore, CampaignStoreError

    figures: dict[str, Any] = {}
    if not config.database_url:
        evidence.add("measure", "database figures", "skip", "no DATABASE_URL")
        return figures
    try:
        store = await CampaignStore.connect(config.database_url, timeout=10.0)
    except CampaignStoreError as exc:
        evidence.add("measure", "database figures", "fail", str(exc).splitlines()[0])
        return figures
    try:
        pool = store._pool
        by_status = {r["status"]: int(r["n"]) for r in await pool.fetch(f"SELECT status, count(*) AS n FROM {ATTEMPTS_TABLE} GROUP BY status")}
        finished = sum(n for s, n in by_status.items() if s not in ("PENDING", "QUEUED", "CALLING", "CONNECTED", "UNRESOLVED"))
        reached = sum(n for s, n in by_status.items() if s in ("COMPLETED", "CALLBACK_REQUESTED", "NOT_INTERESTED", "DO_NOT_CALL"))
        figures["attempts_by_status"] = by_status
        figures["finished"] = finished
        figures["reached"] = reached
        figures["answer_rate"] = round(reached / finished, 3) if finished else None
        try:
            dispositions = {r["disposition"]: int(r["n"]) for r in await pool.fetch("SELECT disposition, count(*) AS n FROM call_results GROUP BY disposition")}
        except Exception:  # noqa: BLE001 - a schema without results
            dispositions = {}
        figures["results_by_disposition"] = dispositions
        good = sum(n for d, n in dispositions.items() if d in ("MEETING_BOOKED", "QUALIFIED", "CALLBACK_SCHEDULED"))
        figures["success_rate"] = round(good / sum(dispositions.values()), 3) if dispositions else None
        try:
            latency = await pool.fetchrow(
                f"SELECT count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY (usage->'quality'->>'p50_ms')::float) AS p50, "
                f"percentile_cont(0.95) WITHIN GROUP (ORDER BY (usage->'quality'->>'p95_ms')::float) AS p95 "
                f"FROM {ATTEMPTS_TABLE} WHERE usage->'quality'->>'p50_ms' IS NOT NULL"
            )
            figures["latency"] = {"calls_with_figures": int(latency["n"]), "p50_ms": round(float(latency["p50"])) if latency["p50"] is not None else None, "p95_ms": round(float(latency["p95"])) if latency["p95"] is not None else None}
        except Exception:  # noqa: BLE001 - a schema without the usage column
            figures["latency"] = {"calls_with_figures": 0, "p50_ms": None, "p95_ms": None}
        try:
            cost = await pool.fetchrow(f"SELECT count(*) FILTER (WHERE cost_usd IS NOT NULL) AS priced, coalesce(sum(cost_usd), 0) AS total FROM {ATTEMPTS_TABLE}")
            figures["cost"] = {"priced_calls": int(cost["priced"]), "total_usd": float(cost["total"])}
        except Exception:  # noqa: BLE001
            figures["cost"] = {"priced_calls": 0, "total_usd": 0.0}
        live_dupes = await pool.fetch(f"SELECT prospect_id, count(*) AS n FROM {ATTEMPTS_TABLE} WHERE status IN ('PENDING','QUEUED','CALLING','CONNECTED','UNRESOLVED') GROUP BY prospect_id HAVING count(*) > 1")
        key_dupes = await pool.fetch(f"SELECT idempotency_key FROM {ATTEMPTS_TABLE} WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING count(*) > 1")
        figures["duplicates"] = {"prospects_with_two_live_attempts": len(live_dupes), "repeated_idempotency_keys": len(key_dupes)}
        try:
            summary = await store.worker_summary(stale_after_secs=config.worker.stale_secs)
            depth = await store.queue_depth(max_attempts=config.campaign_max_attempts)
            figures["fleet"] = {"workers": summary.describe(), "queue": depth.describe()}
        except CampaignStoreError as exc:
            figures["fleet"] = {"error": str(exc).splitlines()[0]}
        throughput = await store.throughput(window_secs=config.monitoring.throughput_window_secs)
        figures["throughput"] = throughput.to_dict()
    finally:
        await store.close()

    evidence.add("measure", "no prospect has two live attempts", "pass" if figures["duplicates"]["prospects_with_two_live_attempts"] == 0 else "fail", str(figures["duplicates"]))
    evidence.add("measure", "no idempotency key is repeated", "pass" if figures["duplicates"]["repeated_idempotency_keys"] == 0 else "fail")
    evidence.add("measure", f"attempts by status: {by_status}", "pass")
    evidence.add("measure", f"answer rate {figures['answer_rate']} over {finished} finished attempt(s); success rate {figures['success_rate']} over {sum(dispositions.values())} result(s)", "pass" if finished else "skip", "" if finished else "no finished real calls yet")
    lat = figures["latency"]
    evidence.add("measure", f"response latency p50 {lat['p50_ms']} ms / p95 {lat['p95_ms']} ms over {lat['calls_with_figures']} call(s) with figures", "pass" if lat["calls_with_figures"] else "skip", "" if lat["calls_with_figures"] else "no call has written a quality summary yet")
    evidence.add("measure", f"cost: {figures['cost']['priced_calls']} priced call(s), ${figures['cost']['total_usd']:.4f} total", "pass" if figures["cost"]["priced_calls"] else "skip", "" if figures["cost"]["priced_calls"] else "no priced calls yet (set COST_* rates)")
    evidence.measures = figures
    return figures


# --- The report ----------------------------------------------------------------------------------


def requirement_status(evidence: Evidence, sections: tuple[str, ...]) -> str:
    seen = [evidence.section_ok(s) for s in sections]
    if any(s is False for s in seen):
        return "FAILED"
    if all(s is None for s in seen):
        return "NOT RUN"
    return "verified (automated)"


def write_report(evidence: Evidence, *, ran_tests: bool, ran_evals: bool) -> tuple[str, dict[str, Any]]:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    failed = evidence.failed
    lines = [
        "# Validation report",
        "",
        f"Generated {now} by `uv run validate.py` on `{os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', 'this machine')}`.",
        "",
        "**Verdict: " + ("every automated check passed" if not failed else f"{len(failed)} automated check(s) FAILED") + ". Items marked *requires manual verification* have not been proven on this system and must be done by a person before go-live.**",
        "",
        "## Requirements",
        "",
        "| Requirement | Automated evidence | Status | Requires manual verification |",
        "|---|---|---|---|",
    ]
    matrix: list[dict[str, Any]] = []
    for name, sections, manual in REQUIREMENTS:
        status = requirement_status(evidence, sections)
        evidence_text = ", ".join(f"`{s}`" for s in sections)
        lines.append(f"| {name} | {evidence_text} | {status} | {manual or '—'} |")
        matrix.append({"requirement": name, "sections": list(sections), "status": status, "manual": manual})
    lines += ["", "## Automated checks", ""]
    for section in ("config", "health", "posture", "measure", *[n for n, _ in SCRIPTS], "evals"):
        rows = [f for f in evidence.findings if f.section == section]
        if not rows:
            continue
        lines.append(f"### {section}")
        lines.append("")
        for f in rows:
            mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}[f.status]
            lines.append(f"- **{mark}** {f.name}" + (f" — {f.detail}" if f.detail else ""))
        lines.append("")
    if evidence.scripts:
        total = sum(s["passed"] for s in evidence.scripts.values())
        lines += [f"**{len(evidence.scripts)} check scripts, {total:,} checks passed, {sum(len(s['failed']) for s in evidence.scripts.values())} failed.**", ""]
    if not ran_tests:
        lines += ["The check scripts were not run in this invocation (`--skip-tests`).", ""]
    if not ran_evals:
        lines += ["The eval suites were not run in this invocation (pass `--evals`; they need vendor keys and a working TTS).", ""]
    lines += [
        "## Still requiring manual verification",
        "",
        *[f"- **{name}** — {manual}" for name, _sections, manual in REQUIREMENTS if manual],
        "",
        "See `PRODUCTION_READINESS.md` for the go-live checklist these feed.",
        "",
    ]
    payload = {
        "generated_at": now,
        "verdict": "pass" if not failed else "fail",
        "requirements": matrix,
        "findings": [f.to_dict() for f in evidence.findings],
        "scripts": evidence.scripts,
        "measures": evidence.measures,
        "ran_tests": ran_tests,
        "ran_evals": ran_evals,
    }
    return "\n".join(lines), payload


# --- The controlled real-phone test ------------------------------------------------------------


LIVE_PROCEDURE = """
The controlled real-phone test (Phase 12's `tests/live_call.py`, driven from here).

  Preconditions — every one is checked below before anything dials:
    1. `uv run health.py` is green: the carrier, STT, LLM and TTS keys are accepted.
    2. The bot is up:      uv run bot.py                          (terminal 1)
    3. A public address:   ngrok http 7860  -> TELEPHONY_PUBLIC_URL in .env; the bot restarted after.
    4. Your own phone as --to, in E.164. Nobody else's.
    5. No ACTIVE campaign you did not mean to run (`uv run campaign.py status`): this test
       places ONE manual call and touches no campaign, but `campaign.py run` would.

  What to do on the phone (the script `live_call.py` prints again when the call comes):
    answer; say who you are; answer one question briefly; interrupt the agent mid-sentence;
    stay silent for fifteen seconds; ask to be called back; hang up.

  What it checks: placed, rang, answered, billed seconds, the carrier's machine-detection
  verdict; the bot's report — greeted, turns each side, every turn answered, the barge-in
  stop latency, per-turn latency, how the call ended. It writes CALL_REPORT_DIR/<call>.json.

  Then:  uv run campaign.py webhooks        (the first real delivery, if the signing key is set)
         uv run validate.py measure          (the figures from the rows)
"""


async def live(config: Config, args: argparse.Namespace) -> int:
    print(LIVE_PROCEDURE)
    problems: list[str] = []
    telephony = config.telephony
    try:
        telephony.require_outbound()
    except ConfigError as exc:
        problems.append(str(exc).splitlines()[0])
    if not args.to or not re.fullmatch(r"\+[1-9]\d{6,14}", args.to):
        problems.append("--to must be your own number in E.164 (+<country><number>)")
    report = await check_health(config, timeout_secs=config.reliability.health_timeout_secs, only=("stt", "llm", "tts", "telephony"))
    for component in report.components:
        if component.status is Status.FAILED:
            problems.append(f"{component.name}: {component.detail}")
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:7860/readyz", timeout=3) as reply:  # noqa: S310 - loopback
            ready = json.loads(reply.read().decode("utf-8")).get("ready")
        if not ready:
            problems.append("the bot on 127.0.0.1:7860 is up but not ready (/readyz)")
    except Exception as exc:  # noqa: BLE001 - not up
        problems.append(f"the bot is not answering on 127.0.0.1:7860/readyz ({exc.__class__.__name__}): start `uv run bot.py`")
    print("Preconditions:")
    if problems:
        for problem in problems:
            print(f"  NOT MET  {problem}")
        print("\nNothing was dialled.")
        return EXIT_FAILED
    print("  every precondition is met")
    if not (args.dial and args.yes):
        print("\nNothing was dialled: add --dial --yes to place the call (it rings a phone and spends money).")
        return EXIT_OK
    command = [sys.executable, str(SERVER / "tests" / "live_call.py"), "--to", args.to, "--yes", "--expect-barge-in"]
    print(f"\nDialling through {telephony.provider}: {' '.join(command[1:])}\n")
    return subprocess.run(command, cwd=SERVER).returncode


# --- Main --------------------------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_NO_CONFIG

    if args.command == "live":
        return await live(config, args)

    evidence = Evidence()
    if args.command == "measure":
        figures = await measure(config, evidence)
        print(json.dumps(figures, indent=2, default=str))
        return EXIT_FAILED if evidence.failed else EXIT_OK

    print("Production validation\n")
    print("  configuration ...")
    check_config(config, evidence)
    print("  vendors (health.py) ...")
    await check_vendors(config, evidence)
    print("  posture (security.py check) ...")
    check_posture(evidence)
    print("  the rows (measure) ...")
    await measure(config, evidence)
    if not args.skip_tests:
        print(f"  {len(SCRIPTS)} check scripts, in parallel (a few minutes) ...")
        await run_scripts(evidence, only=set(args.only) if args.only else None)
    if args.evals:
        print("  the two eval suites (many minutes) ...")
        await run_evals(evidence)

    text, payload = write_report(evidence, ran_tests=not args.skip_tests, ran_evals=args.evals)
    REPORT_MD.write_text(text, encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print()
    for f in evidence.findings:
        if f.status in ("fail", "warn") or f.section in ("health", "measure") or f.section.startswith("test_"):
            mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}[f.status]
            print(f"  {mark:<5} {f.section:<18} {f.name}" + (f"  -- {f.detail}" if f.detail and f.status != "pass" else ""))
    total = sum(s["passed"] for s in evidence.scripts.values())
    print(f"\n  {len(evidence.scripts)} scripts, {total:,} checks passed, {len(evidence.failed)} finding(s) failed, {sum(1 for f in evidence.findings if f.status == 'warn')} warning(s).")
    print(f"  Report: {REPORT_MD}  (and .json)")
    print(f"  Manual verification still required: {sum(1 for _n, _s, m in REQUIREMENTS if m)} item(s) — listed in the report.\n")
    return EXIT_FAILED if evidence.failed else EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(prog="validate.py", description="Run every automated production check and write validation-report.md.")
    parser.add_argument("--skip-tests", action="store_true", help="do not run the check scripts (seconds instead of minutes)")
    parser.add_argument("--evals", action="store_true", help="also run the two eval suites (vendor keys, a working TTS, many minutes)")
    parser.add_argument("--only", nargs="*", metavar="SCRIPT", help="run only these check scripts (names without .py)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("measure", help="print the figures from the rows as JSON and exit")
    live_parser = sub.add_parser("live", help="the controlled real-phone test: check the preconditions, then dial only with --dial --yes")
    live_parser.add_argument("--to", help="your own phone, E.164")
    live_parser.add_argument("--dial", action="store_true", help="actually place the call")
    live_parser.add_argument("--yes", action="store_true", help="confirm that it rings a phone and spends money")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
