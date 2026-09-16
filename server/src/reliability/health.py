"""Is every dependency actually reachable? Asked cheaply, before it matters. Phase 9.

Six things have to work for a call to happen — the database, the carrier, STT,
the LLM, TTS and the application's own configuration — and until now the way to
find out was to place a call and listen. Two of those failures are silent: a
retired model id and a rejected key both produce a bot that answers the phone
and says nothing.

**Nothing here places a call, synthesises audio or runs an inference.** Every
check is the cheapest authenticated read each vendor offers — list the models,
list the voices, read the account — because a health check that costs money or
rings a phone is one nobody runs. The cost of that choice is honest and worth
stating: these prove *the credential is accepted and the service is reachable*,
not that a call would sound right. The eval suite is what proves the second.

**A check never raises and never hangs.** Each one has its own timeout and
turns every failure into a `Component` with a message; the report is the return
value, not an exception. So `health.py` exits non-zero on a red component
rather than on a traceback, and the bot's startup probe can run the same code
without being able to stop the bot from booting.

**Model names are checked against the catalogue where a provider lists one.**
This is the Groq failure this project has already met: the catalogue rotates,
and a retired model id fails at the first turn of a live call with a 404 that
nothing in the bot's log explains. Listing the models costs one request and
turns it into a line at startup.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import aiohttp
from loguru import logger

from ..config import Config
from .observability import redact


class Status(StrEnum):
    """How a component answered."""

    OK = "ok"
    DEGRADED = "degraded"
    """Reachable, but something is wrong that will bite later — a model id the
    provider does not list, a knowledge base with no documents in it."""

    FAILED = "failed"
    SKIPPED = "skipped"
    """Not configured, or not checkable. Not a failure: a bot with no carrier is
    a working browser bot, and saying "failed" would train people to ignore the
    report."""

    @property
    def is_problem(self) -> bool:
        """Whether this status should make a health command exit non-zero."""
        return self is Status.FAILED


@dataclass(frozen=True)
class Component:
    """One dependency's answer.

    Attributes:
        name: What was checked (`database`, `llm`, …).
        status: How it answered.
        detail: What answered, or what went wrong. Scrubbed of credentials.
        latency_ms: How long the check took, when it made a request.
    """

    name: str
    status: Status
    detail: str = ""
    latency_ms: int | None = None

    def describe(self) -> str:
        """One line for a terminal."""
        timing = f" ({self.latency_ms} ms)" if self.latency_ms is not None else ""
        return f"{self.status.value.upper():<9} {self.name:<12} {self.detail}{timing}"

    def to_dict(self) -> dict[str, Any]:
        """Plain data, for `--json`."""
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


@dataclass
class HealthReport:
    """Every component's answer, and whether the system is fit to make calls."""

    components: list[Component] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """Whether nothing is outright failed."""
        return not any(component.status.is_problem for component in self.components)

    @property
    def degraded(self) -> list[Component]:
        """Components that answered but warned."""
        return [c for c in self.components if c.status is Status.DEGRADED]

    def get(self, name: str) -> Component | None:
        """One component by name."""
        return next((c for c in self.components if c.name == name), None)

    def summary(self) -> str:
        """One line: how many of each status."""
        counts: dict[str, int] = {}
        for component in self.components:
            counts[component.status.value] = counts.get(component.status.value, 0) + 1
        return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        """Plain data, for `--json`."""
        return {
            "healthy": self.healthy,
            "summary": self.summary(),
            "components": [component.to_dict() for component in self.components],
        }


# --- The checks ---------------------------------------------------------------

#: Where each LLM provider lists its models, and how it wants to be authenticated.
#: A provider missing from here is skipped rather than guessed at.
_LLM_CATALOGUE: dict[str, tuple[str, Callable[[str], dict[str, str]]]] = {
    "groq": ("https://api.groq.com/openai/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/models",
        lambda key: {"x-goog-api-key": key},
    ),
    "openai": ("https://api.openai.com/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
    "cerebras": ("https://api.cerebras.ai/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
    "openrouter": ("https://openrouter.ai/api/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
    "mistral": ("https://api.mistral.ai/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
    "anthropic": (
        "https://api.anthropic.com/v1/models",
        lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
    ),
    "ollama": ("http://localhost:11434/api/tags", lambda _key: {}),
}


class HealthChecker:
    """Runs the health checks. One instance per run; owns its HTTP session."""

    def __init__(self, config: Config, *, timeout_secs: float = 6.0) -> None:
        """Create the checker.

        Args:
            config: What is configured, so each check knows what to probe.
            timeout_secs: Ceiling per component. Short: a health check that
                takes a minute to report a dead dependency is one more thing
                that has hung.
        """
        self._config = config
        self._timeout = timeout_secs
        self._session: aiohttp.ClientSession | None = None

    async def run(self, only: tuple[str, ...] = ()) -> HealthReport:
        """Check everything, or only the named components.

        Runs the checks concurrently: they are independent, and a serial run
        waits for the slowest of six timeouts rather than for one.
        """
        checks: list[tuple[str, Callable[[], Awaitable[Component]]]] = [
            ("application", self._check_application),
            ("database", self._check_database),
            ("scheduler", self._check_scheduler),
            ("knowledge", self._check_knowledge),
            ("stt", self._check_stt),
            ("llm", self._check_llm),
            ("tts", self._check_tts),
            ("tts_fallback", self._check_tts_fallback),
            ("telephony", self._check_telephony),
            ("calendar", self._check_calendar),
            ("crm", self._check_crm),
        ]
        wanted = [check for name, check in checks if not only or name in only]
        try:
            results = await asyncio.gather(*(self._guarded(check) for check in wanted))
        finally:
            await self.close()
        return HealthReport(components=list(results))

    async def close(self) -> None:
        """Release the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _guarded(self, check: Callable[[], Awaitable[Component]]) -> Component:
        """Run one check, turning a hang or a crash into a reported failure."""
        name = check.__name__.removeprefix("_check_")
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout + 1.0):
                return await check()
        except TimeoutError:
            return Component(
                name, Status.FAILED, f"no answer within {self._timeout:g}s", _ms(started)
            )
        except Exception as exc:  # noqa: BLE001 - a check must never take the run down
            logger.debug(f"HEALTH | {name} raised", exc_info=True)
            return Component(
                name, Status.FAILED, redact(f"{exc.__class__.__name__}: {exc}"), _ms(started)
            )

    # --- Individual components -------------------------------------------------

    async def _check_application(self) -> Component:
        """The process itself: configuration parsed, and what it is set up to do."""
        return Component(
            "application",
            Status.OK,
            f"{self._config.describe_knowledge()} knowledge base, "
            f"sales {'on' if self._config.sales.enabled else 'off'}",
        )

    async def _check_database(self) -> Component:
        """PostgreSQL: reachable, and the campaign tables exist."""
        if not self._config.database_url:
            return Component("database", Status.SKIPPED, "DATABASE_URL is not set")

        from ..campaigns import CampaignStore, CampaignStoreError

        started = time.monotonic()
        try:
            store = await CampaignStore.connect(self._config.database_url, timeout=self._timeout)
        except CampaignStoreError as exc:
            return Component(
                "database", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]), _ms(started)
            )
        try:
            prospects = await store.count_prospects()
            live = await store.count_live_attempts()
        finally:
            await store.close()

        detail = f"{prospects} prospect(s), {live} live attempt(s)"
        # A live attempt older than a few minutes on a system with no bot
        # running is exactly what recovery exists for, and worth surfacing here
        # rather than only in the dialer's log.
        status = Status.DEGRADED if live else Status.OK
        if live:
            detail += " — run `campaign.py recover` if no calls are in progress"
        return Component("database", status, detail, _ms(started))

    async def _check_scheduler(self) -> Component:
        """The worker fleet: who is alive, who went quiet, and the queue depth. Phase 21."""
        if not self._config.database_url:
            return Component("scheduler", Status.SKIPPED, "DATABASE_URL is not set")

        from ..campaigns import CampaignStore, CampaignStoreError

        stale_secs = self._config.worker.stale_secs
        started = time.monotonic()
        try:
            store = await CampaignStore.connect(self._config.database_url, timeout=self._timeout)
        except CampaignStoreError as exc:
            return Component(
                "scheduler", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]), _ms(started)
            )
        try:
            try:
                summary = await store.worker_summary(stale_after_secs=stale_secs)
                depth = await store.queue_depth(max_attempts=self._config.campaign_max_attempts)
            except CampaignStoreError as exc:
                if "does not exist" in str(exc):
                    return Component(
                        "scheduler",
                        Status.DEGRADED,
                        "no scheduler_workers table — run `campaign.py init` before running several workers",
                        _ms(started),
                    )
                raise
        finally:
            await store.close()

        detail = f"{summary.describe()}; queue: {depth.describe()}"
        if summary.stale:
            return Component(
                "scheduler",
                Status.DEGRADED,
                detail + f" — {summary.stale} worker(s) stopped beating; a live worker adopts their calls",
                _ms(started),
            )
        if depth.due_now and not summary.alive:
            return Component(
                "scheduler",
                Status.DEGRADED,
                detail + " — work is due and no worker is running (`campaign.py run`)",
                _ms(started),
            )
        return Component("scheduler", Status.OK, detail, _ms(started))

    async def _check_knowledge(self) -> Component:
        """The knowledge base: reachable, and not empty."""
        if not self._config.kb_enabled:
            return Component("knowledge", Status.SKIPPED, "KB_ENABLED is false")

        from ..embeddings import make_embedder
        from ..knowledge_store import KnowledgeStore, KnowledgeStoreError

        started = time.monotonic()
        embedder = make_embedder(self._config.embedding_model)
        try:
            store = await KnowledgeStore.connect(
                self._config.kb_database_url,
                dimensions=embedder.dimensions,
                embed_model=embedder.model_name,
            )
        except KnowledgeStoreError as exc:
            return Component(
                "knowledge", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]), _ms(started)
            )
        try:
            documents, chunks = await store.counts()
        finally:
            await store.close()

        if chunks == 0:
            return Component(
                "knowledge",
                Status.DEGRADED,
                "no documents — the agent will say it has no information",
                _ms(started),
            )
        return Component(
            "knowledge", Status.OK, f"{documents} document(s), {chunks} chunk(s)", _ms(started)
        )

    async def _check_stt(self) -> Component:
        """Deepgram: the key is accepted. Streams no audio."""
        key = self._config.stt_api_key
        if not key:
            return Component("stt", Status.SKIPPED, "no API key configured")
        return await self._probe(
            "stt",
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {key}"},
            describe=lambda body: f"{self._config.stt_provider}:{self._config.stt_model} — key accepted",
        )

    async def _check_llm(self) -> Component:
        """The LLM provider: the key is accepted, and the configured model is listed."""
        provider = self._config.llm_provider
        target = _LLM_CATALOGUE.get(provider)
        if target is None:
            return Component("llm", Status.SKIPPED, f"no catalogue endpoint known for {provider}")
        key = self._config.llm_api_key
        if key is None and provider != "ollama":
            return Component("llm", Status.SKIPPED, "no API key configured")

        url, headers_for = target
        wanted = self._config.llm_model
        return await self._probe(
            "llm",
            url,
            headers=headers_for(key or ""),
            describe=lambda body: f"{provider}:{wanted} — key accepted",
            # The check that catches a retired model id before a live call does.
            verify=lambda body: _model_present(body, wanted),
            degraded=(
                f"{provider} does not list {wanted!r}. It may have been retired — "
                f"a live call would fail at the first turn. Set {provider.upper()}_MODEL."
            ),
        )

    async def _check_tts(self) -> Component:
        """The TTS provider: the key is accepted. Synthesises nothing."""
        return await self._probe_tts("tts", self._config.tts_provider, self._config.tts_api_key)

    async def _check_tts_fallback(self) -> Component:
        """Phase 30: the fallback TTS provider's key, when a fallback is on. Synthesises nothing.

        The same credential probe as `tts`, against the fallback's provider,
        so a fallback that could not take over is red before a call needs it —
        without spending any of its credits on the check.
        """
        if not self._config.tts_fallback_enabled:
            return Component("tts_fallback", Status.SKIPPED, "no fallback (TTS_FALLBACK_ENABLED=false)")
        return await self._probe_tts(
            "tts_fallback", self._config.tts_fallback_provider, self._config.tts_fallback_api_key
        )

    async def _probe_tts(self, name: str, provider: str, key: str | None) -> Component:
        """Cartesia, Deepgram or ElevenLabs: the key is accepted. Synthesises nothing.

        Phase 23: ElevenLabs gained a probe of its own. Until then a
        `TTS_PROVIDER=elevenlabs` key was sent to Cartesia's endpoint and the
        component read "credentials rejected" for the wrong reason.
        """
        if not key:
            return Component(name, Status.SKIPPED, "no API key configured")
        if provider == "deepgram":
            return await self._probe(
                name,
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {key}"},
                describe=lambda body: "deepgram — key accepted",
            )
        if provider == "elevenlabs":
            model = self._config.elevenlabs_model
            return await self._probe(
                name,
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": key},
                describe=lambda body: f"elevenlabs:{model} — key accepted",
                # Phase 30: a *restricted* ElevenLabs key (text-to-speech only)
                # answers 401 "missing the permission user_read" here — and on
                # every other request that costs nothing — while the TTS
                # websocket accepts it (verified live 2026-09-14). That is a
                # recognised key this check cannot confirm, not a rejected one.
                unauthorized=lambda body: (
                    Component(
                        name,
                        Status.DEGRADED,
                        f"elevenlabs:{model} — key recognised but restricted "
                        f"({_message(body)[:80]}); only synthesis can confirm it",
                    )
                    if "missing the permission" in _message(body)
                    else None
                ),
            )
        return await self._probe(
            name,
            "https://api.cartesia.ai/voices",
            headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10"},
            describe=lambda body: "cartesia — key accepted",
        )

    async def _check_telephony(self) -> Component:
        """The carrier: the account reads back. Places no call.

        Also reports the settings that are not credentials but are just as
        fatal on a real call — a missing caller ID, a missing public URL —
        because both produce a call that cannot be placed and neither is
        visible in an authentication check.
        """
        telephony = self._config.telephony
        if not telephony.has_credentials:
            return Component(
                "telephony", Status.SKIPPED, f"{telephony.provider} has no credentials configured"
            )

        from ..telephony import TelephonyError, make_provider

        started = time.monotonic()
        provider = make_provider(telephony, timeout_secs=self._timeout)
        try:
            described = await provider.check_credentials()
        except NotImplementedError:
            return Component(
                "telephony",
                Status.SKIPPED,
                f"{provider.name} has no cheap credential check",
                _ms(started),
            )
        except TelephonyError as exc:
            return Component(
                "telephony", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]), _ms(started)
            )
        finally:
            await provider.close()

        gaps = [
            name
            for name, value in (
                ("TELEPHONY_FROM_NUMBER", telephony.from_number),
                ("TELEPHONY_PUBLIC_URL", telephony.public_url),
            )
            if not value
        ]
        if gaps:
            return Component(
                "telephony",
                Status.DEGRADED,
                f"{provider.name}: {described}, but {' and '.join(gaps)} not set, so no call "
                f"can be placed",
                _ms(started),
            )
        return Component("telephony", Status.OK, f"{provider.name}: {described}", _ms(started))

    async def _check_calendar(self) -> Component:
        """The calendar: the key reads the event type back. Books nothing. Phase 16.

        Cal.com only. The local calendar needs no account, and a bot with
        `CALENDAR_PROVIDER=none` has nothing to check. Degraded when the event
        type's length differs from `CALENDAR_SLOT_MINUTES`, because that
        books the wrong length silently.
        """
        calendar = self._config.calendar
        if not calendar.enabled:
            return Component("calendar", Status.SKIPPED, "no calendar (CALENDAR_PROVIDER=none)")
        if calendar.provider == "local":
            detail = f"local calendar, {calendar.business_hours} {calendar.business_days}, no account needed"
            if not self._config.database_url:
                return Component("calendar", Status.DEGRADED, detail + " — but no DATABASE_URL to record bookings in")
            return Component("calendar", Status.OK, detail)

        from zoneinfo import ZoneInfo

        from ..scheduling import CalendarError, make_calendar

        started = time.monotonic()
        try:
            provider = make_calendar(
                calendar.provider,
                tz=ZoneInfo(calendar.timezone) if calendar.timezone.upper() != "UTC" else ZoneInfo("UTC"),
                timezone_name=calendar.timezone,
                slot_minutes=calendar.slot_minutes,
                hours=calendar.hours,
                min_notice_minutes=calendar.min_notice_minutes,
                calcom_api_key=calendar.calcom_api_key,
                calcom_event_type_id=calendar.calcom_event_type_id,
                calcom_api_base=calendar.calcom_api_base,
                calcom_timeout_secs=min(calendar.calcom_timeout_secs, self._timeout),
            )
        except ValueError as exc:
            return Component("calendar", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]))
        if provider is None:
            return Component("calendar", Status.SKIPPED, "no calendar")
        try:
            described = await provider.check_credentials()
        except NotImplementedError:
            return Component("calendar", Status.SKIPPED, f"{provider.name} has no cheap credential check", _ms(started))
        except CalendarError as exc:
            return Component("calendar", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]), _ms(started))
        finally:
            await provider.close()
        status = Status.DEGRADED if "differs" in described else Status.OK
        return Component("calendar", status, f"{provider.name}: {described}", _ms(started))

    async def _check_crm(self) -> Component:
        """The CRM: the token reads one contact back. Files nothing. Phase 15."""
        crm = self._config.crm
        if not crm.enabled:
            return Component("crm", Status.SKIPPED, "no CRM configured (CRM_PROVIDER=none)")

        from ..config import ConfigError
        from ..crm import CrmError, make_crm_provider

        started = time.monotonic()
        try:
            provider = make_crm_provider(crm, timeout_secs=self._timeout)
        except ConfigError as exc:
            return Component("crm", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]))
        try:
            described = await provider.check_credentials()
        except NotImplementedError:
            return Component("crm", Status.SKIPPED, f"{provider.name} has no cheap credential check", _ms(started))
        except CrmError as exc:
            return Component("crm", Status.FAILED, redact((str(exc).splitlines() or [type(exc).__name__])[0]), _ms(started))
        finally:
            await provider.close()
        return Component("crm", Status.OK, f"{provider.name}: {described}", _ms(started))

    # --- HTTP ------------------------------------------------------------------

    async def _probe(
        self,
        name: str,
        url: str,
        *,
        headers: dict[str, str],
        describe: Callable[[Any], str],
        verify: Callable[[Any], bool] | None = None,
        degraded: str = "",
        unauthorized: Callable[[Any], Component | None] | None = None,
    ) -> Component:
        """GET a vendor's cheapest authenticated endpoint and report what happened.

        `unauthorized`, given the body of a 401/403, may return a component
        for a refusal that is not a rejected credential (a key that lacks a
        permission, say); `None` keeps the usual "credentials rejected".
        """
        started = time.monotonic()
        session = await self._http()
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as response:
                body = await _json(response)
                if response.status in (401, 403):
                    explained = unauthorized(body) if unauthorized is not None else None
                    if explained is not None:
                        return Component(explained.name, explained.status, explained.detail, _ms(started))
                    return Component(
                        name, Status.FAILED, f"credentials rejected (HTTP {response.status})", _ms(started)
                    )
                if response.status >= 400:
                    return Component(
                        name,
                        Status.FAILED,
                        redact(f"HTTP {response.status}: {_message(body)}"),
                        _ms(started),
                    )
                if verify is not None and not verify(body):
                    return Component(name, Status.DEGRADED, degraded, _ms(started))
                return Component(name, Status.OK, describe(body), _ms(started))
        except TimeoutError:
            return Component(
                name, Status.FAILED, f"no answer within {self._timeout:g}s", _ms(started)
            )
        except aiohttp.ClientError as exc:
            return Component(
                name, Status.FAILED, redact(f"unreachable: {exc.__class__.__name__}"), _ms(started)
            )

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session


async def check_health(config: Config, *, timeout_secs: float = 6.0, only: tuple[str, ...] = ()) -> HealthReport:
    """Run every health check and return the report. Never raises."""
    return await HealthChecker(config, timeout_secs=timeout_secs).run(only)


def _model_present(body: Any, wanted: str) -> bool:
    """Whether `wanted` appears in a models listing.

    Tolerant of the three shapes providers use (`{"data": [...]}`,
    `{"models": [...]}`, `{"models": [{"name": ...}]}` for Ollama) and, when
    none of them is recognisable, returns True — an unreadable catalogue is not
    evidence that a model is missing, and reporting DEGRADED on it would train
    people to ignore the check.
    """
    if not wanted:
        return True
    entries = None
    if isinstance(body, dict):
        for key in ("data", "models"):
            if isinstance(body.get(key), list):
                entries = body[key]
                break
    if entries is None:
        return True
    names = set()
    for entry in entries:
        if isinstance(entry, dict):
            for key in ("id", "name", "model"):
                if isinstance(entry.get(key), str):
                    names.add(entry[key].removeprefix("models/"))
        elif isinstance(entry, str):
            names.add(entry)
    if not names:
        return True
    # Ollama reports `llama3.2:latest` for a model configured as `llama3.2`.
    return wanted in names or any(name.split(":", 1)[0] == wanted for name in names)


def _message(body: Any) -> str:
    """A vendor's error text, from whichever field it used."""
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "err_msg"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:200]
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"][:200]
    return str(body)[:200]


async def _json(response: aiohttp.ClientResponse) -> Any:
    """Decode a response body, tolerating a non-JSON one."""
    try:
        return await response.json(content_type=None)
    except Exception:  # noqa: BLE001 - a body that is not JSON is itself the answer
        return {"message": (await response.text())[:200]}


def _ms(started: float) -> int:
    """Milliseconds since `started`."""
    return int(round((time.monotonic() - started) * 1000))


__all__ = ["Component", "HealthChecker", "HealthReport", "Status", "check_health"]
