"""The HTTP surface an automation platform drives. Phase 17.

```
CSV / CRM / event  -->  n8n  --HTTP-->  this API  --rows-->  the worker  --dials-->  the person
                                                                     |
                             n8n  <--POST (signed)--  the outbox  <--rows--  the bot writes the result
```

**What it is.** A small, authenticated, JSON API over the campaign rows —
prospects, campaigns, calls, callbacks, results, the outbox — built on the
same `CampaignService` the CLI uses. Every write goes through the rules that
already exist: a prospect's number is normalised the same way an import
normalises it, a campaign starts through the same status write, a call is
queued through the same callback row the agent itself writes when a prospect
asks to be phoned back. The API adds no rule of its own about who may be
called; it adds a way in.

**It never dials.** `POST /calls` writes a callback row due now, and the
scheduler (`uv run campaign.py run`) places the call on its next tick under
the calling hours, the concurrency limit, the pacing and the campaign's
status, exactly as it places every other call. That is what keeps an
automation platform asynchronous and out of the audio path: n8n asks, the
row records the ask, the worker decides *when* — and the bot, holding the
conversation, never hears from either.

**Every write is idempotent**, twice over. The rows have natural keys — a
prospect is one per number, a campaign one per name, a membership one per
pair, a pending callback one per prospect — so repeating a request repeats
nothing. And a request that carries an `Idempotency-Key` header has its
*answer* kept for `AUTOMATION_IDEMPOTENCY_TTL_SECS`, so a client retrying
after a lost answer gets the same status and body back (`Idempotent-Replayed:
true`); the same key with a different body is refused.

**Authentication** is a bearer key from `AUTOMATION_API_KEYS`, checked in
constant time on every route but `/api/ping`. The app refuses to build
without one: every write here can make a phone ring.

**A separate process.** `uv run automation.py` serves this and, in the
same process, runs the outbox deliverer. It shares the database with the
bot and the worker and nothing else — the same seam `dashboard.py`,
`webhooks.py` and `campaign.py crm-sync` already use.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..campaigns.models import (
    AutomationEventState,
    CallbackStatus,
    CampaignStatus,
    MeetingStatus,
    ProspectStatus,
)
from ..campaigns.phone import normalize_phone
from ..campaigns.results import Disposition
from ..campaigns.service import CampaignService
from ..campaigns.store import CampaignStore, CampaignStoreError, DuplicateProspectError
from ..compliance import CONFIG_KEY as COMPLIANCE_KEY
from ..compliance import DncSource, PolicyResolver, parse_source
from ..config import (
    AUTOMATION_EVENT_KINDS,
    AutomationConfig,
    Config,
    ConfigError,
    MonitoringConfig,
    SecurityConfig,
)
from ..monitoring.collect import GaugeRefresher
from ..monitoring.http import Readiness, ReadyCheck, install_ops_routes, store_ready
from ..reliability.observability import Timer
from ..reliability.observability import event as log_event
from ..security import (
    COOKIE_NAME,
    AuditLog,
    AuditUnavailable,
    HttpPolicy,
    Permission,
    Principal,
    RateLimiter,
    Role,
    client_ip,
    install_security,
    is_loopback,
    parse_networks,
    read_session,
    redact_pii,
)
from .auth import extract_key, fingerprint
from .events import EventDeliverer, Sender
from .serialize import (
    attempt_dict,
    callback_dict,
    campaign_dict,
    event_dict,
    meeting_dict,
    membership_dict,
    prospect_dict,
    result_dict,
    transfer_dict,
)

API_PREFIX = "/api/v1"
PING_PATH = "/api/ping"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
API_VERSION = "18"

#: The most rows any list route returns in one answer.
MAX_LIMIT = 500

#: Phase 18. Control characters have no place in a name, a note or a header;
#: a NUL in particular is what turns a string into something else further
#: down (PostgreSQL refuses it; a CSV writer does not).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: What an `Idempotency-Key` may look like: the client chose it, so it is
#: bounded and printable rather than anything at all.
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")
#: The largest `custom_data` (plus any extra fields) a prospect may carry.
MAX_CUSTOM_DATA_BYTES = 16 * 1024
MAX_CUSTOM_DATA_KEYS = 100
#: The largest cell an import row may carry.
MAX_IMPORT_CELL_CHARS = 2000
MAX_IMPORT_ROW_KEYS = 60


# --- Settings ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiSettings:
    """What the API needs from the configuration, and nothing more.

    Its own object so the checks can build one by hand without a full
    `Config` (which demands vendor keys the API never uses).
    """

    automation: AutomationConfig
    database_url: str | None
    default_region: str | None = None
    max_attempts: int = 3
    retry_minutes: float = 60.0
    callback_max_days_ahead: int = 60
    timezone: str = "UTC"
    #: Phase 18: rate limits, HTTPS, CORS, the audit log. Defaults are the
    #: safe ones, so a check that builds settings by hand gets them too.
    security: SecurityConfig = field(default_factory=SecurityConfig.defaults)
    #: Phase 19: the compliance policy resolver. None means the two figures
    #: above apply everywhere and no campaign or jurisdiction overlay exists.
    compliance: PolicyResolver | None = None
    #: Phase 22: where /metrics is served from, and who may read it.
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    #: Phase 22: for the fleet gauges — how long a worker may go quiet.
    worker_stale_secs: float = 60.0

    @classmethod
    def from_config(cls, config: Config) -> ApiSettings:
        """The settings `automation.py` builds from `.env`."""
        return cls(
            automation=config.automation,
            database_url=config.database_url,
            default_region=config.default_phone_region,
            max_attempts=config.campaign_max_attempts,
            retry_minutes=config.campaign_retry_minutes,
            callback_max_days_ahead=config.callback_max_days_ahead,
            timezone=config.calendar.timezone,
            security=config.security,
            compliance=config.policy_resolver(),
            monitoring=config.monitoring,
            worker_stale_secs=config.worker.stale_secs,
        )


# --- Errors -------------------------------------------------------------------------------


class ApiError(Exception):
    """A refusal with a status, a machine-readable code and a sentence for a person."""

    def __init__(
        self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None, **details: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.details = {k: v for k, v in details.items() if v is not None}

    def response(self) -> JSONResponse:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = jsonable_encoder(self.details)
        return JSONResponse(body, status_code=self.status, headers=self.headers or None)


# --- Request bodies -----------------------------------------------------------------------


def _text(value: Any, *, name: str, multiline: bool = False) -> Any:
    """A text field, stripped, with control characters refused. Phase 18.

    The lengths are on the `Field`; this is the part a length cannot
    express: a NUL, an escape sequence, a newline in a name. Multiline
    fields (a description, a note) may carry newlines and tabs.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    if _CONTROL.search(value) or (not multiline and ("\n" in value or "\r" in value)):
        raise ValueError(f"{name} contains control characters")
    return value.strip()


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _check_mapping(data: dict[str, Any], *, name: str, max_keys: int, max_bytes: int) -> None:
    if len(data) > max_keys:
        raise ValueError(f"{name} may carry at most {max_keys} fields")
    for key in data:
        if not isinstance(key, str) or not key or len(key) > 100 or _CONTROL.search(key):
            raise ValueError(f"{name} has a field name that is empty, too long, or not printable")
    if _json_size(data) > max_bytes:
        raise ValueError(f"{name} may be at most {max_bytes} bytes as JSON")


class ProspectIn(BaseModel):
    """One prospect. Unknown fields are kept in `custom_data`, as a CSV's extra columns are."""

    model_config = ConfigDict(extra="allow")

    first_name: str = Field(min_length=1, max_length=200)
    last_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=64)
    email: str | None = Field(default=None, max_length=320)
    company: str | None = Field(default=None, max_length=300)
    job_title: str | None = Field(default=None, max_length=300)
    industry: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    website: str | None = Field(default=None, max_length=500)
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("first_name", "last_name", "phone", "email", "company", "job_title", "industry", "location", "website")
    @classmethod
    def _printable(cls, value: Any, info: Any) -> Any:
        cleaned = _text(value, name=info.field_name)
        if info.field_name in ("first_name", "last_name", "phone") and not cleaned:
            raise ValueError(f"{info.field_name} must not be blank")
        # A phone that is not a number is *not* refused here: Phase 17 stores
        # it as UNREACHABLE and says so, which is what an import does too.
        return cleaned

    @model_validator(mode="after")
    def _bounded(self) -> ProspectIn:
        extras = dict(self.model_extra or {})
        _check_mapping({**extras, **self.custom_data}, name="custom_data", max_keys=MAX_CUSTOM_DATA_KEYS, max_bytes=MAX_CUSTOM_DATA_BYTES)
        return self


class ImportIn(BaseModel):
    """A list of prospects, each a row with any of the CSV import's recognised headers."""

    rows: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)
    campaign: str | int | None = None
    create_campaign: bool = False
    dry_run: bool = False

    @field_validator("rows")
    @classmethod
    def _rows_bounded(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index, row in enumerate(rows, start=1):
            if len(row) > MAX_IMPORT_ROW_KEYS:
                raise ValueError(f"row {index} has more than {MAX_IMPORT_ROW_KEYS} columns")
            for key, value in row.items():
                if not isinstance(key, str) or len(key) > 100 or _CONTROL.search(key):
                    raise ValueError(f"row {index} has a column name that is too long or not printable")
                if isinstance(value, str) and (len(value) > MAX_IMPORT_CELL_CHARS or "\x00" in value):
                    raise ValueError(f"row {index}, column {key!r}: a cell may be at most {MAX_IMPORT_CELL_CHARS} characters")
        return rows

    @field_validator("campaign")
    @classmethod
    def _campaign_text(cls, value: Any) -> Any:
        return _text(value, name="campaign") if isinstance(value, str) else value


class CampaignIn(BaseModel):
    """A campaign to create."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = _text(value, name="name")
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description(cls, value: Any) -> Any:
        return _text(value, name="description", multiline=True)


class ConfigurationIn(BaseModel):
    """A campaign's own settings: the agent profile the brief reads, and its pacing. Phase 24.

    The keys are the ones `conversation/brief.py` overlays on the environment's
    `SALES_*` defaults, plus `pacing_secs` (Phase 21). A field left `None` is
    not touched; an empty string or list clears the key, so the environment's
    default applies again.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str | None = Field(default=None, max_length=120)
    company_name: str | None = Field(default=None, max_length=200)
    # Phase 28: the campaign's own always-available facts about the company.
    company_description: str | None = Field(default=None, max_length=1000)
    services: list[str] | None = Field(default=None, max_length=20)
    offer: str | None = Field(default=None, max_length=2000)
    value_points: list[str] | None = Field(default=None, max_length=20)
    qualification_criteria: list[str] | None = Field(default=None, max_length=20)
    meeting_ask: str | None = Field(default=None, max_length=500)
    notes: list[str] | None = Field(default=None, max_length=20)
    pacing_secs: float | None = Field(default=None, ge=0, le=3600)
    # Phase 25: this campaign's own live-call ceiling, under the deployment's
    # MAX_CONCURRENT_CALLS. 0 clears it (the deployment's limit alone).
    max_concurrent_calls: int | None = Field(default=None, ge=0, le=100)

    @field_validator("agent_name", "company_name", "company_description", "offer", "meeting_ask")
    @classmethod
    def _plain(cls, value: Any) -> Any:
        return _text(value, name="text", multiline=True) if isinstance(value, str) else value

    @field_validator("value_points", "qualification_criteria", "notes", "services")
    @classmethod
    def _lines(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = [_text(item, name="line", multiline=True) for item in value if isinstance(item, str)]
        return [line for line in cleaned if line]

    def changes(self) -> dict[str, Any]:
        """The keys to write: `None` skipped, empty cleared (written as None)."""
        out: dict[str, Any] = {}
        for key in ("agent_name", "company_name", "company_description", "offer", "meeting_ask"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value or None
        for key in ("value_points", "qualification_criteria", "notes", "services"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value or None
        if self.pacing_secs is not None:
            out["pacing_secs"] = self.pacing_secs or None
        if self.max_concurrent_calls is not None:
            out["max_concurrent_calls"] = self.max_concurrent_calls or None
        return out


class AddProspectsIn(BaseModel):
    """Who to add to a campaign: by id, by number, or everybody."""

    prospect_ids: list[int] = Field(default_factory=list, max_length=10_000)
    phones: list[str] = Field(default_factory=list, max_length=10_000)
    all: bool = False

    @field_validator("prospect_ids")
    @classmethod
    def _positive(cls, ids: list[int]) -> list[int]:
        if any(i < 1 for i in ids):
            raise ValueError("prospect ids are positive integers")
        return ids

    @field_validator("phones")
    @classmethod
    def _phones(cls, phones: list[str]) -> list[str]:
        cleaned = []
        for phone in phones:
            text = _text(phone, name="phone")
            if not text or len(text) > 64:
                raise ValueError("a phone must be 1 to 64 printable characters")
            cleaned.append(text)
        return cleaned


class CallRequestIn(BaseModel):
    """A call to place, or to place at a time: who, for which campaign, when."""

    prospect_id: int | None = Field(default=None, ge=1)
    phone: str | None = Field(default=None, max_length=64)
    campaign_id: int | None = Field(default=None, ge=1)
    campaign: str | None = Field(default=None, max_length=200)
    scheduled_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("phone", "campaign")
    @classmethod
    def _single_line(cls, value: Any, info: Any) -> Any:
        return _text(value, name=info.field_name)

    @field_validator("note")
    @classmethod
    def _note(cls, value: Any) -> Any:
        return _text(value, name="note", multiline=True)


class DncIn(BaseModel):
    """A number for the do-not-call list. Phase 19."""

    phone: str = Field(min_length=3, max_length=64)
    reason: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=20, description="verbal, api, import, registry or manual (default api)")
    note: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None

    @field_validator("phone", "source")
    @classmethod
    def _single_line(cls, value: Any, info: Any) -> Any:
        return _text(value, name=info.field_name)

    @field_validator("reason", "note")
    @classmethod
    def _multi(cls, value: Any, info: Any) -> Any:
        return _text(value, name=info.field_name, multiline=True)


class DncRemoveIn(BaseModel):
    """Why a number comes off the list, and whether its prospects reopen. Phase 19."""

    reason: str | None = Field(default=None, max_length=500)
    reinstate_prospects: bool = False

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: Any) -> Any:
        return _text(value, name="reason", multiline=True)


class ComplianceIn(BaseModel):
    """A campaign's compliance settings: the overlay keys, validated against the policy. Phase 19."""

    model_config = ConfigDict(extra="forbid")

    jurisdiction: str | None = Field(default=None, max_length=40)
    calling_hours: str | None = Field(default=None, max_length=200)
    calling_days: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=80)
    enforce_calling_hours: bool | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    retry_minutes: float | None = Field(default=None, ge=0, le=10080)
    retry_minutes_no_answer: float | None = Field(default=None, ge=0, le=10080)
    retry_minutes_busy: float | None = Field(default=None, ge=0, le=10080)
    retry_minutes_voicemail: float | None = Field(default=None, ge=0, le=10080)
    ai_disclosure: str | None = Field(default=None, max_length=400)
    ai_disclosure_required: bool | None = None
    recording_enabled: bool | None = None
    recording_disclosure: str | None = Field(default=None, max_length=400)
    recording_disclosure_required: bool | None = None

    @field_validator("jurisdiction", "calling_hours", "calling_days", "timezone", "ai_disclosure", "recording_disclosure")
    @classmethod
    def _printable(cls, value: Any, info: Any) -> Any:
        return _text(value, name=info.field_name)

    def overrides(self) -> dict[str, Any]:
        """Only the keys that were given."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class CampaignAction(StrEnum):
    """The lifecycle verbs `POST /campaigns/{campaign}/{action}` takes."""

    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    COMPLETE = "complete"
    CANCEL = "cancel"


#: For each verb: the status it moves to, and the statuses it may move from.
#: A campaign already in the target status is answered `changed: false`, and
#: one it may not move from is a 409 — so "start" twice is safe and "start"
#: after "complete" is refused, which is what an automation retrying
#: blindly needs.
_TRANSITIONS: dict[CampaignAction, tuple[CampaignStatus, frozenset[CampaignStatus]]] = {
    CampaignAction.START: (CampaignStatus.ACTIVE, frozenset({CampaignStatus.DRAFT, CampaignStatus.PAUSED})),
    CampaignAction.RESUME: (CampaignStatus.ACTIVE, frozenset({CampaignStatus.PAUSED, CampaignStatus.DRAFT})),
    CampaignAction.PAUSE: (CampaignStatus.PAUSED, frozenset({CampaignStatus.ACTIVE})),
    CampaignAction.COMPLETE: (
        CampaignStatus.COMPLETED,
        frozenset({CampaignStatus.DRAFT, CampaignStatus.ACTIVE, CampaignStatus.PAUSED}),
    ),
    CampaignAction.CANCEL: (
        CampaignStatus.CANCELLED,
        frozenset({CampaignStatus.DRAFT, CampaignStatus.ACTIVE, CampaignStatus.PAUSED}),
    ),
}

#: The sentence every queued call carries, so nobody reads 202 as "ringing".
_DIALLED_BY = (
    "the scheduler (`uv run campaign.py run`) on its next tick, once the campaign is ACTIVE, "
    "within calling hours, under the concurrency limit and pacing"
)


# --- The app ------------------------------------------------------------------------------

StoreFactory = Callable[[], Awaitable[Any]]


def create_automation_app(
    settings: ApiSettings | Config,
    *,
    store_factory: StoreFactory | None = None,
    deliver: bool = True,
    sender: Sender | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the automation API, and the deliverer that runs beside it.

    Args:
        settings: `ApiSettings`, or a full `Config` to take them from.
        store_factory: Where the store comes from. `CampaignStore.connect`
            by default; the checks hand in a fake.
        deliver: Run the outbox deliverer in this process while the app is
            up, when a webhook URL is configured. `automation.py --no-deliver`
            turns it off for a deployment that runs it elsewhere.
        sender: The deliverer's HTTP sender; the checks script one.
        clock: Where "now" comes from; the checks inject one.

    Raises:
        ConfigError: No API key is configured.
        CampaignStoreError: No database is configured.
    """
    if isinstance(settings, Config):
        settings = ApiSettings.from_config(settings)
    automation = settings.automation
    automation.require_api_keys()
    if not settings.database_url and store_factory is None:
        raise CampaignStoreError(
            "No database is configured, so the automation API has nothing to serve.\n"
            "  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to)."
        )

    now = clock or (lambda: datetime.now(UTC))
    state: dict[str, Any] = {"store": None, "service": None, "deliverer": None, "task": None}

    # Phase 18: the guards. One limiter per key label, one per address for
    # requests that carry no valid key; the audit writer over the store;
    # and the trusted networks behind `client_ip`.
    security = settings.security
    networks = parse_networks(security.trusted_proxies)
    key_limiter = RateLimiter(security.api_rate_limit, 60.0)
    anon_limiter = RateLimiter(security.anon_rate_limit, 60.0)

    def store_for_audit() -> Any:
        store = state["store"]
        if store is None:
            raise CampaignStoreError("the automation API is still starting")
        return store

    audit = AuditLog(store_for_audit, enabled=security.audit_enabled, strict=security.audit_strict, clock=now)
    monitoring = settings.monitoring
    refresher = (
        GaugeRefresher(
            store_for_audit,
            interval_secs=monitoring.refresh_secs,
            stale_secs=settings.worker_stale_secs,
            max_attempts=settings.max_attempts,
            window_secs=monitoring.throughput_window_secs,
        )
        if monitoring.enabled
        else None
    )

    async def open_store() -> Any:
        if store_factory is not None:
            return await store_factory()
        return await CampaignStore.connect(settings.database_url or "", min_size=1, max_size=4)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = await open_store()
        service = CampaignService(
            store,
            default_region=settings.default_region,
            max_attempts=settings.max_attempts,
            retry_minutes=settings.retry_minutes,
            clock=now,
            compliance=settings.compliance,
        )
        state["store"] = store
        state["service"] = service
        deliverer: EventDeliverer | None = None
        task: asyncio.Task[Any] | None = None
        if deliver and automation.delivery_enabled:
            deliverer = EventDeliverer(
                store,
                targets=automation.targets,
                secret=automation.webhook_secret,
                auth_header=automation.webhook_auth_header,
                auth_token=automation.webhook_auth_token,
                sender=sender,
                timeout_secs=automation.timeout_secs,
                max_attempts=automation.max_attempts,
                retry_secs=automation.retry_secs,
                max_retry_secs=automation.max_retry_secs,
                batch=automation.batch,
                stale_secs=automation.stale_secs,
                settle_secs=automation.settle_secs,
                since=automation.events_since,
                include_transcript=automation.include_transcript,
                clock=now,
            )
            task = asyncio.create_task(deliverer.run(poll_secs=automation.poll_secs), name="automation-deliverer")
        state["deliverer"] = deliverer
        state["task"] = task
        if refresher is not None:
            refresher.start()
        logger.info(log_event("automation.api_ready", outcome=automation.describe()))
        try:
            yield
        finally:
            if refresher is not None:
                await refresher.stop()
            if deliverer is not None:
                deliverer.request_stop()
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=30.0)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                except Exception as exc:  # noqa: BLE001 - shutting down; say so and carry on
                    logger.warning(log_event("automation.deliverer_crashed", error=str(exc)))
            if deliverer is not None:
                await deliverer.close()
            state.update(store=None, service=None, deliverer=None, task=None)
            await store.close()

    app = FastAPI(
        title="Ai-Voice-Agent automation API",
        description=(
            "The endpoints an automation platform (n8n) drives: prospects, campaigns, calls, "
            "callbacks, results and the outbound event log. Every route but `/api/ping` needs "
            "`Authorization: Bearer <AUTOMATION_API_KEYS>`. Nothing here dials: a call request "
            "is a row the scheduler places on its next tick."
        ),
        version=API_VERSION,
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs" if automation.docs_enabled else None,
        openapi_url=f"{API_PREFIX}/openapi.json" if automation.docs_enabled else None,
        redoc_url=None,
    )
    # Phase 18: security headers, HTTPS enforcement, the body cap, CORS.
    install_security(
        app,
        HttpPolicy(
            require_https=security.require_https,
            trusted_proxies=security.trusted_proxies,
            cors_origins=security.cors_origins,
            max_body_bytes=security.max_body_bytes,
        ),
        kind="api",
    )
    # Phase 22: /healthz, /readyz, /metrics — outside the keyed router, since
    # a probe holds no key — plus a request id echoed on every answer and a
    # count per route template. Readiness is the database and the deliverer
    # loop still running when one was started.
    if monitoring.enabled:

        async def readiness() -> Readiness:
            checks = [await store_ready(store_for_audit)]
            task: asyncio.Task[Any] | None = state["task"]
            if task is not None:
                checks.append(ReadyCheck("deliverer", not task.done(), "delivering" if not task.done() else "the deliverer loop has stopped"))
            return Readiness(checks)

        install_ops_routes(app, "api", readiness=readiness, token=monitoring.token, version=API_VERSION)

    # --- Plumbing -------------------------------------------------------------------------

    def store_of() -> Any:
        store = state["store"]
        if store is None:
            raise CampaignStoreError("the automation API is still starting")
        return store

    def service_of() -> CampaignService:
        service = state["service"]
        if service is None:
            raise CampaignStoreError("the automation API is still starting")
        return service

    def ip_of(request: Request) -> str:
        return client_ip(request, networks)

    def principal_of(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if principal is None:  # pragma: no cover - every routed request passed require_key
            raise ApiError(401, "unauthorized", "This request was not authenticated.")
        return principal

    def _session_principal(request: Request) -> Principal | None:
        """The dashboard's session cookie as a principal, or None. Phase 24."""
        if security.dashboard_auth_disabled and is_loopback(ip_of(request)):
            return Principal(name="anonymous", role=Role.OPERATOR, via="anonymous")
        secret = security.session_secret
        token = request.cookies.get(COOKIE_NAME)
        if not secret or not token:
            return None
        session, _reason = read_session(secret, token, now=time.time())
        return session.principal() if session is not None else None

    def too_many(decision: Any, *, what: str) -> ApiError:
        return ApiError(
            429,
            "rate_limited",
            f"Too many {what}: at most {decision.limit} per minute. Try again in {decision.retry_after_header}s.",
            headers={"Retry-After": decision.retry_after_header},
            retry_after_secs=int(decision.retry_after_header),
        )

    async def require_key(request: Request) -> Principal:
        """Phase 17's key check, with Phase 18's roles, limits and audit trail.

        The role comes from which list the key is in (`AutomationConfig.role_for_key`,
        constant time across every key). A request with no valid key spends
        the address's anonymous budget *before* anything is written, so a
        script guessing keys cannot also fill the audit table.
        """
        ip = ip_of(request)
        presented = extract_key(request.headers)
        match = automation.role_for_key(presented)
        if match is None and presented is None:
            # Phase 24: the unified application's browser session. The same
            # signed cookie the dashboard issues, read with the same secret;
            # a write from a cookie must also carry the fetch header, so a
            # cross-site form post — which never has it — cannot spend a
            # session. Loopback with the dashboard's login switched off is
            # the local development case, exactly as the dashboard treats it.
            principal = _session_principal(request)
            if principal is not None:
                if request.method not in ("GET", "HEAD", "OPTIONS") and not request.headers.get("x-requested-with"):
                    await audit.record("auth.refused", principal=principal, ip=ip, outcome="cookie write without X-Requested-With", method=request.method, path=request.url.path)
                    raise ApiError(403, "csrf", "A browser session must send `X-Requested-With` on every write.")
                decision = key_limiter.check(f"session:{principal.name}")
                if not decision.allowed:
                    await audit.record("auth.rate_limited", principal=principal, ip=ip, method=request.method, path=request.url.path)
                    raise too_many(decision, what="requests for this session")
                request.state.principal = principal
                request.state.ip = ip
                return principal
        if match is None:
            decision = anon_limiter.check(ip)
            if not decision.allowed:
                raise too_many(decision, what="unauthenticated requests")
            reason = "no API key" if presented is None else "unknown API key"
            logger.warning(log_event("automation.refused", outcome=f"{request.method} {request.url.path}", error=reason, ip=ip))
            await audit.record("auth.refused", ip=ip, outcome=reason, method=request.method, path=request.url.path)
            raise ApiError(
                401,
                "unauthorized",
                "Send the API key as `Authorization: Bearer <key>` (or `X-API-Key`). "
                "Keys are configured in AUTOMATION_API_KEYS (and the operator / viewer lists).",
            )
        role, label = match
        principal = Principal(name=label, role=role, via="api_key")
        decision = key_limiter.check(label)
        if not decision.allowed:
            await audit.record("auth.rate_limited", principal=principal, ip=ip, method=request.method, path=request.url.path)
            raise too_many(decision, what="requests for this key")
        request.state.principal = principal
        request.state.ip = ip
        return principal

    def need(permission: Permission) -> Callable[..., Awaitable[Principal]]:
        """A dependency that refuses, with a 403 and an audit row, a principal lacking a permission."""

        async def dependency(request: Request) -> Principal:
            principal = principal_of(request)
            if not principal.can(permission):
                await audit.record(
                    "auth.forbidden",
                    principal=principal,
                    ip=ip_of(request),
                    outcome=f"needs {permission.value}",
                    method=request.method,
                    path=request.url.path,
                )
                raise ApiError(
                    403,
                    "forbidden",
                    f"This needs the `{permission.value}` permission; the key holds the {principal.role.value} role.",
                    role=principal.role.value,
                    required=permission.value,
                )
            return principal

        return dependency

    need_read = need(Permission.READ)
    need_pii = need(Permission.READ_PII)
    need_write = need(Permission.WRITE)
    need_manage = need(Permission.MANAGE)

    async def require_pii(request: Request, *, what: str) -> Principal:
        """A route that is readable by a viewer, asked for the part that is not."""
        principal = principal_of(request)
        if not principal.can(Permission.READ_PII):
            await audit.record("auth.forbidden", principal=principal, ip=ip_of(request), outcome=f"needs read_pii for {what}", path=request.url.path)
            raise ApiError(403, "forbidden", f"Reading {what} needs the `read_pii` permission; the key holds the {principal.role.value} role.", role=principal.role.value, required=Permission.READ_PII.value)
        return principal

    async def audited(request: Request, action: str, *, target: tuple[str, Any] | None = None, outcome: str = "ok", **detail: Any) -> None:
        """Record a sensitive action by the request's principal."""
        await audit.record(action, principal=principal_of(request), ip=ip_of(request), target=target, outcome=outcome, **detail)

    class _PiiRoute(APIRoute):
        """Masks the people out of every answer to a principal without `read_pii`. Phase 18.

        One walk over the JSON (`security.redact_pii`) rather than a
        decision per route: a viewer's key sees `+92••••••••67` wherever a
        number would be, `null` wherever a transcript would be, and `{}`
        for custom fields, whichever route produced them and whatever a
        future serializer adds under those names.
        """

        def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
            original = super().get_route_handler()

            async def handler(request: Request) -> Response:
                response = await original(request)
                principal = getattr(request.state, "principal", None)
                body = getattr(response, "body", b"")
                # A JSON answer whatever class FastAPI chose for it: a
                # `JSONResponse`, or (for a route with a return annotation)
                # a plain `Response` already rendered as JSON.
                is_json = response.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"
                if principal is not None and not principal.can(Permission.READ_PII) and is_json and body:
                    try:
                        masked = redact_pii(json.loads(bytes(body)))
                    except ValueError:  # pragma: no cover - our own JSON
                        return response
                    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")}
                    return JSONResponse(masked, status_code=response.status_code, headers=headers)
                return response

            return handler

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return exc.response()

    @app.exception_handler(AuditUnavailable)
    async def _audit_unavailable(request: Request, exc: AuditUnavailable) -> JSONResponse:
        return ApiError(503, "audit_unavailable", str(exc)).response()

    @app.exception_handler(CampaignStoreError)
    async def _store_error(request: Request, exc: CampaignStoreError) -> JSONResponse:
        logger.error(log_event("automation.store_unavailable", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
        return ApiError(503, "database_unavailable", (str(exc).splitlines() or [type(exc).__name__])[0]).response()

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return ApiError(422, "invalid_request", "The request did not validate.", problems=exc.errors()).response()

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return ApiError(exc.status_code, code, str(exc.detail)).response()

    async def idempotent(
        request: Request,
        scope: str,
        body_fingerprint: str,
        handler: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
    ) -> JSONResponse:
        """Run a write once per `Idempotency-Key`, replaying the stored answer on a repeat."""
        raw_key = request.headers.get(IDEMPOTENCY_HEADER)
        key = raw_key.strip() if raw_key else None
        if not key:
            status, body = await handler()
            return JSONResponse(jsonable_encoder(body), status_code=status)
        if not _IDEMPOTENCY_KEY.match(key):
            raise ApiError(
                422,
                "invalid_idempotency_key",
                f"{IDEMPOTENCY_HEADER} must be 1 to 200 characters of letters, digits, `.`, `_`, `:` or `-`.",
            )

        store = store_of()
        stored = await store.get_api_request(scope, key)
        if stored is not None:
            if stored.fingerprint != body_fingerprint:
                raise ApiError(
                    422,
                    "idempotency_key_reused",
                    "This Idempotency-Key was already used for a different request. Use a new key.",
                    idempotency_key=key,
                )
            return JSONResponse(
                stored.response,
                status_code=stored.status_code,
                headers={REPLAYED_HEADER: "true", IDEMPOTENCY_HEADER: key},
            )

        status, body = await handler()
        encoded = jsonable_encoder(body)
        record, inserted = await store.save_api_request(
            scope=scope, idempotency_key=key, fingerprint=body_fingerprint, status_code=status, response=encoded
        )
        if not inserted:
            # Two identical requests raced. The natural keys made the writes
            # safe; the first answer is the one both clients get.
            if record.fingerprint != body_fingerprint:
                raise ApiError(422, "idempotency_key_reused", "This Idempotency-Key was already used for a different request.")
            return JSONResponse(
                record.response, status_code=record.status_code, headers={REPLAYED_HEADER: "true", IDEMPOTENCY_HEADER: key}
            )
        return JSONResponse(encoded, status_code=status, headers={IDEMPOTENCY_HEADER: key})

    def body_fingerprint(request: Request, body: Any) -> str:
        return fingerprint(request.method, request.url.path, json.dumps(jsonable_encoder(body), sort_keys=True, default=str))

    def limit_of(limit: int) -> int:
        return max(1, min(int(limit), MAX_LIMIT))

    def zone() -> ZoneInfo:
        try:
            return ZoneInfo(settings.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def aware(moment: datetime | None) -> datetime | None:
        """A naive time is read in the campaign's timezone, as the agent reads a caller's."""
        if moment is None:
            return None
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=zone())

    async def resolve_campaign(reference: str | int | None) -> Any:
        if reference is None:
            return None
        store = store_of()
        text = str(reference).strip()
        if text.isdigit():
            found = await store.get_campaign(int(text))
            if found is not None:
                return found
        return await store.find_campaign_by_name(text)

    async def campaign_or_404(reference: str | int) -> Any:
        campaign = await resolve_campaign(reference)
        if campaign is None:
            raise ApiError(404, "campaign_not_found", f"No campaign called {reference!r}.", campaign=reference)
        return campaign

    async def prospect_or_404(prospect_id: int) -> Any:
        prospect = await store_of().get_prospect(prospect_id)
        if prospect is None:
            raise ApiError(404, "prospect_not_found", f"No prospect with id {prospect_id}.", prospect_id=prospect_id)
        return prospect

    async def prospect_by_reference(prospect_id: int | None, phone: str | None) -> Any:
        if prospect_id is not None:
            return await prospect_or_404(prospect_id)
        if phone:
            number = normalize_phone(phone, default_region=settings.default_region)
            if not number.e164:
                raise ApiError(422, "invalid_phone", f"{phone!r} could not be normalised: {number.reason}", phone=phone)
            found = await store_of().find_prospect_by_phone(number.e164)
            if found is None:
                raise ApiError(404, "prospect_not_found", f"No prospect with the number {number.e164}.", phone=number.e164)
            return found
        raise ApiError(422, "prospect_required", "Give `prospect_id` or `phone`.")

    # --- Routes ---------------------------------------------------------------------------

    @app.get(PING_PATH)
    async def ping(request: Request) -> dict[str, Any]:
        """Is the API up and can it reach the database? Unauthenticated: it says nothing else."""
        decision = anon_limiter.check(ip_of(request))
        if not decision.allowed:
            raise too_many(decision, what="unauthenticated requests")
        store = state["store"]
        if store is None:
            return {"ok": False, "detail": "starting"}
        try:
            await store.count_prospects()
        except CampaignStoreError as exc:
            return {"ok": False, "detail": (str(exc).splitlines() or [type(exc).__name__])[0]}
        return {"ok": True, "detail": "database reachable", "version": API_VERSION}

    router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_key)], route_class=_PiiRoute)

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        """What the API and the deliverer are doing, in one read."""
        store = store_of()
        deliverer: EventDeliverer | None = state["deliverer"]
        counts = await store.automation_event_counts()
        by_status: dict[str, int] = {}
        for found in await store.list_campaigns(limit=1000):
            by_status[found.status.value] = by_status.get(found.status.value, 0) + 1
        # Phase 21: the fleet and the queue, for a workflow that scales workers
        # or pages someone. None when the database predates the tables.
        scheduler: dict[str, Any] | None
        try:
            from ..campaigns.coordination import DEFAULT_STALE_SECS
            from ..campaigns.store import CampaignStoreError

            try:
                summary = await store.worker_summary(stale_after_secs=DEFAULT_STALE_SECS)
                depth = await store.queue_depth()
                scheduler = {
                    "workers": summary.to_dict(datetime.now(UTC), DEFAULT_STALE_SECS),
                    "queue": depth.to_dict(),
                }
            except CampaignStoreError as exc:
                if "does not exist" not in str(exc):
                    raise
                scheduler = None
        except AttributeError:
            scheduler = None  # A store double without the Phase 21 reads.
        return {
            "version": API_VERSION,
            "prospects": await store.count_prospects(),
            "campaigns": by_status,
            "events": counts,
            "scheduler": scheduler,
            "delivery": {
                "enabled": deliverer is not None,
                "targets": {kind: _host(url) for kind, url in automation.targets.items()},
                "signed": bool(automation.webhook_secret),
                "totals": deliverer.totals.snapshot() if deliverer is not None else None,
            },
            "dialled_by": _DIALLED_BY,
            # Phase 18: who is asking, so a workflow can tell which key it holds.
            "principal": {
                "name": principal_of(request).name,
                "role": principal_of(request).role.value,
                "permissions": sorted(p.value for p in principal_of(request).permissions),
            },
            "audit": {"written": audit.written, "failed": audit.failed, "strict": audit.strict},
        }

    # Prospects -----------------------------------------------------------------------

    @router.post("/prospects", status_code=201, dependencies=[Depends(need_write)])
    async def create_prospect(request: Request, body: ProspectIn) -> JSONResponse:
        """Create a prospect, or return the one that already has this number.

        Idempotent by phone number: the same person twice is one row and a
        200; a new number is a 201. A number that cannot be normalised is
        still stored — as `UNREACHABLE`, undialable — and the answer says so.
        """
        fp = body_fingerprint(request, body)

        async def handler() -> tuple[int, dict[str, Any]]:
            store, service = store_of(), service_of()
            number = normalize_phone(body.phone, default_region=settings.default_region)
            if number.e164:
                existing = await store.find_prospect_by_phone(number.e164)
                if existing is not None:
                    return 200, {"prospect": prospect_dict(existing), "created": False, "warnings": []}
            extras = {k: v for k, v in (body.model_extra or {}).items()}
            custom = {**extras, **body.custom_data}
            try:
                prospect = await service.create_prospect(
                    first_name=body.first_name,
                    last_name=body.last_name,
                    phone=body.phone,
                    email=body.email,
                    company=body.company,
                    job_title=body.job_title,
                    industry=body.industry,
                    location=body.location,
                    website=body.website,
                    custom_data=custom or None,
                )
            except DuplicateProspectError as exc:
                existing = await store.get_prospect(exc.existing_id)
                if existing is None:
                    raise
                return 200, {"prospect": prospect_dict(existing), "created": False, "warnings": []}
            except ValueError as exc:
                raise ApiError(422, "invalid_prospect", str(exc)) from exc
            warnings = []
            if not number.is_dialable:
                warnings.append(
                    f"phone {body.phone!r} could not be normalised ({number.reason}); "
                    f"stored as UNREACHABLE and will not be dialled until corrected"
                )
            logger.info(log_event("automation.prospect_created", prospect=prospect.id, outcome=prospect.status.value))
            await audited(request, "prospect.create", target=("prospect", prospect.id), status=prospect.status.value)
            return 201, {"prospect": prospect_dict(prospect), "created": True, "warnings": warnings}

        return await idempotent(request, "prospects.create", fp, handler)

    @router.post("/prospects/import", dependencies=[Depends(need_write)])
    async def import_prospects(
        request: Request,
        campaign: str | None = Query(default=None, description="campaign id or name to add every row to"),
        create_campaign: bool = Query(default=False, description="create the campaign if it does not exist"),
        dry_run: bool = Query(default=False, description="parse and report; write nothing"),
    ) -> JSONResponse:
        """Import many prospects: a JSON list of rows, or a CSV body.

        `Content-Type: text/csv` sends the file as-is; anything else is read
        as JSON `{"rows": [...], "campaign": ..., "create_campaign": ...,
        "dry_run": ...}`. Either way the rows go through the CSV import's
        header aliasing (`First Name`, `first_name`, `Mobile` …), phone
        normalisation and duplicate detection, and the answer is the same
        report `campaign.py import` prints: what was created, what was
        already known, what was rejected and why. Re-importing a list creates
        nothing and is safe.
        """
        raw = await request.body()
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type in ("text/csv", "application/csv", "text/plain"):
            text = raw.decode("utf-8-sig", errors="replace")
            reference: str | int | None = campaign
            make = create_campaign
            dry = dry_run
        else:
            try:
                data = ImportIn.model_validate(json.loads(raw or b"{}"))
            except (ValueError, TypeError) as exc:
                raise ApiError(422, "invalid_request", f"Expected JSON with `rows`: {exc}") from exc
            text = _rows_to_csv(data.rows)
            reference = data.campaign if data.campaign is not None else campaign
            make = data.create_campaign or create_campaign
            dry = data.dry_run or dry_run
        fp = fingerprint(request.method, request.url.path, raw, str(reference), str(make), str(dry))

        async def handler() -> tuple[int, dict[str, Any]]:
            service = service_of()
            target = None
            if reference is not None:
                target = await resolve_campaign(reference)
                if target is None:
                    if not make or str(reference).strip().isdigit():
                        raise ApiError(404, "campaign_not_found", f"No campaign called {reference!r}.", campaign=reference)
                    if dry:
                        target = None
                    else:
                        target = await service.create_campaign(str(reference).strip())
            outcome = await service.import_csv(text, campaign_id=target.id if target else None, dry_run=dry)
            report = outcome.report
            body: dict[str, Any] = {
                "dry_run": dry,
                "created": outcome.created,
                "duplicates": outcome.duplicates,
                "added_to_campaign": outcome.added_to_campaign,
                "rejected": [
                    {"line": row.line, "errors": list(row.errors), "values": {k: v for k, v in row.values.items()}}
                    for row in report.invalid_rows[:200]
                ],
                "rejected_count": len(report.invalid_rows),
                "prospect_ids": list(outcome.prospect_ids),
                "mapping": {
                    "columns": dict(report.mapping.columns),
                    "extras": list(report.mapping.extras),
                    "missing_required": list(report.mapping.missing_required),
                    "usable": report.mapping.is_usable,
                },
                "campaign": campaign_dict(target) if target else None,
                "summary": outcome.summary() if report.mapping.is_usable and not report.error else (report.error or report.summary()),
            }
            if report.error:
                raise ApiError(422, "unreadable_import", report.error)
            if not report.mapping.is_usable:
                raise ApiError(
                    422,
                    "unusable_columns",
                    f"No column supplies {', '.join(report.mapping.missing_required)}.",
                    mapping=body["mapping"],
                )
            logger.info(
                log_event(
                    "automation.import",
                    outcome=outcome.summary(),
                    campaign=target.id if target else None,
                )
            )
            if not dry:
                await audited(
                    request,
                    "prospect.import",
                    target=("campaign", target.id) if target else None,
                    created=outcome.created,
                    duplicates=outcome.duplicates,
                    rejected=len(report.invalid_rows),
                )
            return 200, body

        return await idempotent(request, "prospects.import", fp, handler)

    @router.get("/prospects")
    async def list_prospects(
        request: Request,
        phone: str | None = Query(default=None, max_length=64, description="look one prospect up by number"),
        status: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> dict[str, Any]:
        """Prospects, newest first; or one, by number (which needs `read_pii`)."""
        store = store_of()
        if phone:
            # Looking somebody up by number says whether that number is on
            # file, which is the fact a viewer may not have.
            await require_pii(request, what="a lookup by phone number")
            number = normalize_phone(phone, default_region=settings.default_region)
            if not number.e164:
                raise ApiError(422, "invalid_phone", f"{phone!r} could not be normalised: {number.reason}", phone=phone)
            found = await store.find_prospect_by_phone(number.e164)
            return {"prospects": [prospect_dict(found)] if found else [], "count": 1 if found else 0}
        wanted = _enum_or_422(ProspectStatus, status, "status")
        rows = await store.list_prospects(limit=limit_of(limit), offset=offset, status=wanted)
        return {"prospects": [prospect_dict(p) for p in rows], "count": len(rows), "limit": limit, "offset": offset}

    @router.get("/prospects/{prospect_id}")
    async def get_prospect(prospect_id: int = Path(ge=1)) -> dict[str, Any]:
        """One prospect, with their callbacks and recent calls."""
        store = store_of()
        prospect = await prospect_or_404(prospect_id)
        attempts = await store.list_attempts(prospect_id=prospect_id, limit=20)
        callbacks = await _quiet(store.list_callbacks(prospect_id=prospect_id, status=None, limit=20)) or []
        return {
            "prospect": prospect_dict(prospect),
            "calls": [attempt_dict(a) for a in attempts],
            "callbacks": [callback_dict(c) for c in callbacks],
        }

    @router.post("/prospects/{prospect_id}/do-not-call", dependencies=[Depends(need_write)])
    async def do_not_call(
        request: Request,
        prospect_id: int = Path(ge=1),
        reason: str | None = Query(default=None, max_length=500),
    ) -> dict[str, Any]:
        """Never call this person again, on any campaign. Idempotent.

        Phase 19: the number goes on the do-not-call list as well (source
        `api`, the key's label as the actor), so it is blocked whichever
        prospect row carries it.
        """
        prospect = await prospect_or_404(prospect_id)
        changed = prospect.status is not ProspectStatus.DO_NOT_CALL
        actor = principal_of(request).name
        await service_of().mark_do_not_call(
            prospect_id, source=DncSource.API, reason=reason, actor=actor
        )
        prospect = await prospect_or_404(prospect_id)
        if changed:
            await audited(request, "prospect.do_not_call", target=("prospect", prospect_id), reason=reason)
        entry = await service_of().is_listed(prospect.phone_normalized)
        return {"prospect": prospect_dict(prospect), "changed": changed, "dnc": entry.to_dict() if entry else None}

    # The do-not-call list (Phase 19) -------------------------------------------------

    @router.post("/dnc", status_code=201, dependencies=[Depends(need_write)])
    async def add_dnc(request: Request, body: DncIn) -> JSONResponse:
        """Put a number on the do-not-call list, prospect row or not. Every prospect with it is marked."""
        fp = body_fingerprint(request, body)

        async def handler() -> tuple[int, dict[str, Any]]:
            source = parse_source(body.source, DncSource.API)
            if source is DncSource.CLI:
                source = DncSource.API
            try:
                entry, inserted, marked = await service_of().add_do_not_call_number(
                    body.phone,
                    source=source,
                    reason=body.reason,
                    actor=principal_of(request).name,
                    note=body.note,
                    expires_at=aware(body.expires_at),
                )
            except ValueError as exc:
                raise ApiError(422, "invalid_phone", str(exc), phone=body.phone) from exc
            await audited(
                request,
                "compliance.dnc_added",
                target=("number", entry.phone_normalized),
                source=source.value,
                reason=body.reason,
                prospects_marked=marked,
                inserted=inserted,
            )
            return (201 if inserted else 200), {"dnc": entry.to_dict(), "created": inserted, "prospects_marked": marked}

        return await idempotent(request, "dnc.add", fp, handler)

    @router.get("/dnc", dependencies=[Depends(need_pii)])
    async def list_dnc(
        phone: str | None = Query(default=None, max_length=64),
        source: str | None = Query(default=None, max_length=20),
        include_revoked: bool = Query(default=False),
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        """The do-not-call list, newest first. Needs `read_pii`: it is a list of numbers."""
        store = store_of()
        wanted: str | None = None
        if phone:
            number = normalize_phone(phone, default_region=settings.default_region)
            if not number.e164:
                raise ApiError(422, "invalid_phone", f"{phone!r} could not be normalised: {number.reason}", phone=phone)
            wanted = number.e164
        rows = await store.list_dnc(
            phone_normalized=wanted, source=source.strip().lower() if source else None,
            include_revoked=include_revoked, before_id=before_id, limit=limit_of(limit),
        )
        return {
            "entries": [entry.to_dict() for entry in rows],
            "count": len(rows),
            "counts": await store.dnc_counts(),
            "next_before_id": rows[-1].id if len(rows) >= limit_of(limit) and rows[-1].id is not None else None,
        }

    @router.get("/dnc/check", dependencies=[Depends(need_read)])
    async def check_dnc(phone: str = Query(min_length=3, max_length=64)) -> dict[str, Any]:
        """Whether a number may be dialled: on the list, or marked on a prospect. Needs only `read`, and answers only yes or no."""
        number = normalize_phone(phone, default_region=settings.default_region)
        if not number.e164:
            raise ApiError(422, "invalid_phone", f"{phone!r} could not be normalised: {number.reason}", phone=phone)
        entry = await service_of().is_listed(number.e164)
        prospects = await store_of().prospects_with_number(number.e164)
        marked = any(p.status is ProspectStatus.DO_NOT_CALL for p in prospects)
        return {
            "blocked": entry is not None or marked,
            "on_list": entry is not None,
            "prospect_marked": marked,
            "source": entry.source.value if entry else None,
            "since": entry.created_at.isoformat() if entry and entry.created_at else None,
        }

    @router.delete("/dnc/{phone}", dependencies=[Depends(need_manage)])
    async def remove_dnc(request: Request, body: DncRemoveIn | None = None, phone: str = Path(min_length=3, max_length=64)) -> dict[str, Any]:
        """Take a number off the list. Needs `manage`. The row stays, stamped with who and why."""
        payload = body or DncRemoveIn()
        try:
            entry, reinstated = await service_of().remove_do_not_call_number(
                phone, actor=principal_of(request).name, reason=payload.reason, reinstate_prospects=payload.reinstate_prospects
            )
        except ValueError as exc:
            raise ApiError(422, "invalid_phone", str(exc), phone=phone) from exc
        if entry is None:
            raise ApiError(404, "not_listed", f"{phone} is not on the do-not-call list.", phone=phone)
        await audited(
            request, "compliance.dnc_removed", target=("number", entry.phone_normalized),
            reason=payload.reason, reinstated=reinstated,
        )
        return {"dnc": entry.to_dict(), "changed": True, "prospects_reinstated": reinstated}

    # Campaign compliance settings (Phase 19) ---------------------------------------------

    @router.get("/campaigns/{campaign}/compliance")
    async def get_compliance(campaign: str = Path(min_length=1, max_length=200)) -> dict[str, Any]:
        """A campaign's compliance settings and the effective policy they produce."""
        found = await campaign_or_404(campaign)
        overrides = found.configuration.get(COMPLIANCE_KEY) if isinstance(found.configuration, dict) else None
        policy = service_of().policy_for(found)
        resolver = settings.compliance
        return {
            "campaign": campaign_dict(found),
            "settings": dict(overrides or {}),
            "policy": policy.to_dict(),
            "environment": (resolver.base.to_dict() if resolver else None),
            "jurisdictions": (resolver.jurisdictions if resolver else {}),
            "note": "The software applies these settings; deciding them is the operator's. See COMPLIANCE.md.",
        }

    @router.put("/campaigns/{campaign}/configuration", dependencies=[Depends(need_write)])
    async def put_configuration(request: Request, body: ConfigurationIn, campaign: str = Path(min_length=1, max_length=200)) -> dict[str, Any]:
        """Set a campaign's agent profile and pacing. Phase 24.

        Writes one top-level `configuration` key per field given, the same
        way `campaign.py compliance --set` writes the compliance block; the
        bot reads them per call through the brief, so a running campaign's
        next call uses the new profile.
        """
        store = store_of()
        found = await resolve_campaign(campaign)
        if found is None:
            raise ApiError(404, "campaign_not_found", f"No campaign called {campaign!r}.", campaign=campaign)
        changes = body.changes()
        if not changes:
            raise ApiError(422, "nothing_to_change", "Give at least one field to set.")
        updated = found
        for key, value in changes.items():
            updated = await store.update_campaign_configuration(found.id, key, value) or updated
        await audited(request, "campaign.configure", target=("campaign", found.id), keys=sorted(changes))
        return {"campaign": campaign_dict(updated, counts=await store.campaign_counts(updated.id)), "changed": sorted(changes)}

    @router.put("/campaigns/{campaign}/compliance", dependencies=[Depends(need_write)])
    async def put_compliance(request: Request, body: ComplianceIn, campaign: str = Path(min_length=1, max_length=200)) -> dict[str, Any]:
        """Replace a campaign's compliance settings. Validated against the policy before anything is written."""
        found = await campaign_or_404(campaign)
        overrides = body.overrides()
        problems: list[str] = []
        base = settings.compliance.base if settings.compliance else service_of().policy_for(None)
        base.overlay(overrides, source=f"campaign:{found.id}", problems=problems)
        if problems:
            raise ApiError(422, "invalid_compliance_settings", "The settings did not validate.", problems=problems)
        updated = await store_of().update_campaign_configuration(found.id, COMPLIANCE_KEY, overrides or None)
        if updated is None:
            raise ApiError(404, "campaign_not_found", f"No campaign called {campaign!r}.", campaign=campaign)
        await audited(request, "campaign.compliance_updated", target=("campaign", found.id), settings=overrides)
        return {"campaign": campaign_dict(updated), "settings": overrides, "policy": service_of().policy_for(updated).to_dict()}

    # Campaigns -----------------------------------------------------------------------

    @router.post("/campaigns", status_code=201, dependencies=[Depends(need_write)])
    async def create_campaign(request: Request, body: CampaignIn) -> JSONResponse:
        """Create a campaign in DRAFT, or return the one that already has this name."""
        fp = body_fingerprint(request, body)

        async def handler() -> tuple[int, dict[str, Any]]:
            store, service = store_of(), service_of()
            existing = await store.find_campaign_by_name(body.name.strip())
            if existing is not None:
                return 200, {"campaign": campaign_dict(existing, counts=await store.campaign_counts(existing.id)), "created": False}
            try:
                created = await service.create_campaign(body.name.strip(), body.description)
            except CampaignStoreError as exc:
                if "already exists" not in str(exc):
                    raise
                existing = await store.find_campaign_by_name(body.name.strip())
                if existing is None:
                    raise
                return 200, {"campaign": campaign_dict(existing, counts=await store.campaign_counts(existing.id)), "created": False}
            await audited(request, "campaign.create", target=("campaign", created.id), name=created.name)
            return 201, {"campaign": campaign_dict(created, counts=await store.campaign_counts(created.id)), "created": True}

        return await idempotent(request, "campaigns.create", fp, handler)

    @router.get("/campaigns")
    async def list_campaigns(
        status: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=MAX_LIMIT)
    ) -> dict[str, Any]:
        """Campaigns, newest first, each with its membership counts."""
        store = store_of()
        wanted = _enum_or_422(CampaignStatus, status, "status")
        rows = await store.list_campaigns(status=wanted, limit=limit_of(limit))
        return {
            "campaigns": [campaign_dict(c, counts=await store.campaign_counts(c.id)) for c in rows],
            "count": len(rows),
        }

    @router.get("/campaigns/{campaign}")
    async def get_campaign(campaign: str = Path(min_length=1, max_length=200)) -> dict[str, Any]:
        """One campaign by id or name, with its counts and what its queue holds."""
        store = store_of()
        found = await campaign_or_404(campaign)
        counts = await store.campaign_counts(found.id)
        outlook = await _quiet(store.queue_outlook(found.id, max_attempts=settings.max_attempts))
        return {"campaign": campaign_dict(found, counts=counts, outlook=outlook)}

    @router.get("/campaigns/{campaign}/prospects")
    async def list_campaign_prospects(
        campaign: str = Path(min_length=1, max_length=200),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> dict[str, Any]:
        """Who is in a campaign, and where each of them stands."""
        found = await campaign_or_404(campaign)
        pairs = await store_of().list_campaign_prospects(found.id, limit=limit_of(limit), offset=offset)
        return {
            "campaign": campaign_dict(found),
            "members": [{"membership": membership_dict(m), "prospect": prospect_dict(p)} for m, p in pairs],
            "count": len(pairs),
            "limit": limit,
            "offset": offset,
        }

    @router.post("/campaigns/{campaign}/prospects", dependencies=[Depends(need_write)])
    async def add_campaign_prospects(
        request: Request, body: AddProspectsIn, campaign: str = Path(min_length=1, max_length=200)
    ) -> JSONResponse:
        """Add prospects to a campaign, by id, by number, or all of them. Adding twice adds nothing."""
        fp = body_fingerprint(request, body)

        async def handler() -> tuple[int, dict[str, Any]]:
            store, service = store_of(), service_of()
            found = await campaign_or_404(campaign)
            ids = list(dict.fromkeys(body.prospect_ids))
            unknown_phones: list[str] = []
            for phone in body.phones:
                number = normalize_phone(phone, default_region=settings.default_region)
                match = await store.find_prospect_by_phone(number.e164) if number.e164 else None
                if match is None:
                    unknown_phones.append(phone)
                elif match.id not in ids:
                    ids.append(match.id)
            if body.all:
                ids = [p.id for p in await store.list_prospects(limit=100_000)]
            if not ids and not unknown_phones:
                raise ApiError(422, "nobody_to_add", "Give `prospect_ids`, `phones`, or `all: true`.")
            added = await service.add_prospects(found.id, ids)
            counts = await store.campaign_counts(found.id)
            await audited(request, "campaign.add_prospects", target=("campaign", found.id), requested=len(ids), added=added, everybody=body.all)
            return 200, {
                "campaign": campaign_dict(found, counts=counts),
                "requested": len(ids),
                "added": added,
                "already_members": len(ids) - added,
                "unknown_phones": unknown_phones,
            }

        return await idempotent(request, f"campaigns.add:{campaign}", fp, handler)

    @router.post("/campaigns/{campaign}/{action}", dependencies=[Depends(need_write)])
    async def transition_campaign(
        request: Request, action: CampaignAction, campaign: str = Path(min_length=1, max_length=200)
    ) -> dict[str, Any]:
        """start / pause / resume / complete / cancel. Idempotent: the target status twice is `changed: false`.

        Phase 18: `complete` and `cancel` close a campaign for good, so they
        need `manage` (an admin key); the reversible verbs need `write`.
        """
        if action in (CampaignAction.COMPLETE, CampaignAction.CANCEL):
            await need_manage(request)
        store, service = store_of(), service_of()
        found = await campaign_or_404(campaign)
        target, allowed = _TRANSITIONS[action]
        if found.status is target:
            return {"campaign": campaign_dict(found, counts=await store.campaign_counts(found.id)), "changed": False}
        if found.status not in allowed:
            raise ApiError(
                409,
                "invalid_transition",
                f"Cannot {action.value} campaign {found.name!r}: it is {found.status.value}.",
                current_status=found.status.value,
                allowed_from=sorted(s.value for s in allowed),
            )
        updated = await service.set_status(found.id, target) or found
        logger.info(log_event("automation.campaign", campaign=found.id, outcome=f"{found.status.value} -> {updated.status.value}"))
        await audited(request, f"campaign.{action.value}", target=("campaign", found.id), outcome=f"{found.status.value} -> {updated.status.value}")
        return {"campaign": campaign_dict(updated, counts=await store.campaign_counts(updated.id)), "changed": True}

    @router.post("/campaigns/{campaign}/prospects/{prospect_id}/retry", dependencies=[Depends(need_write)])
    async def retry_prospect(
        request: Request, prospect_id: int = Path(ge=1), campaign: str = Path(min_length=1, max_length=200)
    ) -> JSONResponse:
        """Queue one contact of a campaign again, on purpose. Phase 25.

        The only way a contact the campaign is done with (reached, exhausted)
        is dialled again: the request becomes a due callback, which the
        scheduler places ahead of the queue and past the attempt ceiling,
        through the same reservation as every other call — so a contact
        already on a call is not dialled twice, a do-not-call number is
        refused here, and a paused campaign holds it until resumed.
        """
        found = await campaign_or_404(campaign)
        return await queue_call(
            request,
            CallRequestIn(prospect_id=prospect_id, campaign_id=found.id, note="explicit retry"),
            require_time=False,
        )

    # Calls and callbacks -------------------------------------------------------------

    async def queue_call(request: Request, body: CallRequestIn, *, require_time: bool) -> JSONResponse:
        fp = body_fingerprint(request, body)
        scope = "callbacks.schedule" if require_time else "calls.queue"

        async def handler() -> tuple[int, dict[str, Any]]:
            store, service = store_of(), service_of()
            prospect = await prospect_by_reference(body.prospect_id, body.phone)
            if body.campaign_id is None and not body.campaign:
                raise ApiError(422, "campaign_required", "Give `campaign_id` or `campaign`: a call is placed for a campaign.")
            found = await campaign_or_404(body.campaign_id if body.campaign_id is not None else body.campaign or "")
            if prospect.status is ProspectStatus.DO_NOT_CALL:
                raise ApiError(409, "do_not_call", f"{prospect.full_name} is marked DO_NOT_CALL and will not be dialled.", prospect_id=prospect.id)
            # Phase 19: the list, whichever row the number is on. The gate
            # would refuse the dial anyway; refusing the *request* tells the
            # workflow now rather than leaving a callback the scheduler blocks.
            listed = await service.is_listed(prospect.phone_normalized)
            if listed is not None:
                await audited(request, "compliance.blocked", target=("prospect", prospect.id), outcome="dnc_list", purpose="queue", source=listed.source.value)
                raise ApiError(
                    409, "do_not_call",
                    f"{prospect.full_name}'s number is on the do-not-call list ({listed.source.value}) and will not be dialled.",
                    prospect_id=prospect.id,
                )
            if not prospect.phone_normalized:
                raise ApiError(409, "not_dialable", f"{prospect.full_name} has no dialable number ({prospect.phone!r}).", prospect_id=prospect.id)

            moment = service.now()
            when = aware(body.scheduled_at)
            if require_time and when is None:
                raise ApiError(422, "scheduled_at_required", "Give `scheduled_at` (ISO 8601) for a callback; use POST /calls to call now.")
            if when is None or (not require_time and when < moment):
                when = moment
            if require_time and when < moment - timedelta(minutes=5):
                raise ApiError(422, "scheduled_in_past", f"`scheduled_at` {when.isoformat()} is in the past.", now=moment.isoformat())
            horizon = moment + timedelta(days=settings.callback_max_days_ahead)
            if when > horizon:
                raise ApiError(
                    422, "too_far_ahead", f"`scheduled_at` is more than {settings.callback_max_days_ahead} days away (CALLBACK_MAX_DAYS_AHEAD).", latest=horizon.isoformat()
                )

            membership = await store.find_membership(found.id, prospect.id)
            joined = False
            if membership is None:
                membership = await store.add_to_campaign(found.id, prospect.id) or await store.find_membership(found.id, prospect.id)
                joined = membership is not None
            if membership is None:
                raise CampaignStoreError("the membership could not be created")

            previous = await store.list_callbacks(prospect_id=prospect.id, status=CallbackStatus.PENDING, limit=1)
            note = body.note or ("requested through the automation API" if not require_time else "scheduled through the automation API")
            callback = await store.schedule_callback(
                prospect_id=prospect.id,
                scheduled_for=when,
                campaign_id=found.id,
                campaign_prospect_id=membership.id,
                note=note,
            )

            warnings: list[str] = []
            if not found.status.is_dialable:
                warnings.append(f"campaign {found.name!r} is {found.status.value}; the scheduler places callbacks only for ACTIVE campaigns — start it")
            if await store.has_live_attempt(prospect.id):
                warnings.append("the prospect is on a call right now; this will be placed after it ends")
            if membership.status.is_closed:
                warnings.append(f"this campaign is {membership.status.value} for them; the scheduler reopens the membership for the callback")
            if previous and previous[0].scheduled_for != when:
                warnings.append(f"a pending callback for {previous[0].scheduled_for.isoformat()} was moved to this time (one pending callback per prospect)")
            logger.info(
                log_event(
                    "automation.call_queued",
                    campaign=found.id,
                    prospect=prospect.id,
                    outcome=f"callback {callback.id} for {when.isoformat(timespec='minutes')}",
                )
            )
            await audited(
                request,
                "callback.schedule" if require_time else "call.queue",
                target=("callback", callback.id),
                prospect=prospect.id,
                campaign=found.id,
                scheduled_for=when.isoformat(timespec="minutes"),
                moved=bool(previous and previous[0].scheduled_for != when),
            )
            return 202, {
                "queued": True,
                "callback": callback_dict(callback),
                "prospect": prospect_dict(prospect),
                "campaign": campaign_dict(found),
                "membership": membership_dict(membership),
                "joined_campaign": joined,
                # The pending callback as it stood before this request, when
                # the request moved it: one pending callback per prospect
                # means the row keeps its id and changes its time.
                "replaced": callback_dict(previous[0]) if previous and previous[0].scheduled_for != when else None,
                "dialled_by": _DIALLED_BY,
                "warnings": warnings,
            }

        return await idempotent(request, scope, fp, handler)

    @router.post("/calls", status_code=202)
    async def trigger_call(request: Request, body: CallRequestIn) -> JSONResponse:
        """Ask for a call now (or at `scheduled_at`). Nothing dials here.

        Writes the same pending-callback row the agent writes when a prospect
        asks to be phoned back, due now; `uv run campaign.py run` places it
        on its next tick, ahead of the queue, under every calling rule. One
        pending callback per prospect, so asking twice moves the time rather
        than stacking a second call. 202 means "queued", never "ringing":
        watch `GET /calls?prospect_id=` or the `call.completed` event.
        """
        return await queue_call(request, body, require_time=False)

    @router.post("/callbacks", status_code=202)
    async def schedule_callback(request: Request, body: CallRequestIn) -> JSONResponse:
        """Schedule a callback at `scheduled_at`. The same row `POST /calls` writes, for later."""
        return await queue_call(request, body, require_time=True)

    @router.get("/calls")
    async def list_calls(
        campaign_id: int | None = Query(default=None),
        prospect_id: int | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        """Call attempts, newest first."""
        rows = await store_of().list_attempts(prospect_id=prospect_id, campaign_id=campaign_id, limit=limit_of(limit))
        return {"calls": [attempt_dict(a) for a in rows], "count": len(rows)}

    @router.get("/calls/{attempt_id}")
    async def get_call(
        request: Request,
        attempt_id: int = Path(ge=1),
        include: str | None = Query(default=None, max_length=100, description="`transcript` to include it (needs `read_pii`)"),
    ) -> dict[str, Any]:
        """One call: the attempt, its result once there is one, and any transfer."""
        store = store_of()
        transcript = _wants_transcript(include)
        if transcript:
            await require_pii(request, what="a transcript")
        attempt = await store.get_attempt(attempt_id)
        if attempt is None:
            raise ApiError(404, "call_not_found", f"No call attempt with id {attempt_id}.", attempt_id=attempt_id)
        result = await _quiet(store.get_call_result(attempt_id))
        transfers = await _quiet(store.list_transfers(call_attempt_id=attempt_id)) or []
        if transcript and result is not None:
            await audited(request, "pii.transcript_read", target=("call", attempt_id))
        return {
            "call": attempt_dict(attempt),
            "result": result_dict(result, include_transcript=transcript) if result else None,
            "transfers": [transfer_dict(t) for t in transfers],
        }

    @router.get("/callbacks")
    async def list_callbacks(
        status: str | None = Query(default="PENDING", description="PENDING (default), PLACED, CANCELLED, or `all`"),
        due: bool = Query(default=False, description="only callbacks whose time has come"),
        prospect_id: int | None = Query(default=None),
        campaign_id: int | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        """Scheduled callbacks, soonest first."""
        wanted = None if (status or "").lower() == "all" else _enum_or_422(CallbackStatus, status, "status")
        rows = await store_of().list_callbacks(
            prospect_id=prospect_id,
            status=wanted,
            due_before=service_of().now() if due else None,
            limit=limit_of(limit),
            campaign_id=campaign_id,
        )
        return {"callbacks": [callback_dict(c) for c in rows], "count": len(rows), "now": service_of().now().isoformat()}

    @router.get("/callbacks/{callback_id}")
    async def get_callback(callback_id: int) -> dict[str, Any]:
        """One callback."""
        callback = await store_of().get_callback(callback_id)
        if callback is None:
            raise ApiError(404, "callback_not_found", f"No callback with id {callback_id}.", callback_id=callback_id)
        return {"callback": callback_dict(callback)}

    @router.delete("/callbacks/{callback_id}", dependencies=[Depends(need_write)])
    async def cancel_callback(request: Request, callback_id: int = Path(ge=1)) -> dict[str, Any]:
        """Withdraw a pending callback. Already withdrawn or placed is `changed: false`."""
        store = store_of()
        changed = await store.cancel_callback(callback_id)
        callback = await store.get_callback(callback_id)
        if callback is None:
            raise ApiError(404, "callback_not_found", f"No callback with id {callback_id}.", callback_id=callback_id)
        if changed:
            await audited(request, "callback.cancel", target=("callback", callback_id), prospect=callback.prospect_id)
        return {"callback": callback_dict(callback), "changed": changed}

    # Results, meetings, events -------------------------------------------------------

    @router.get("/results")
    async def list_results(
        request: Request,
        campaign_id: int | None = Query(default=None, ge=1),
        prospect_id: int | None = Query(default=None, ge=1),
        disposition: str | None = Query(default=None, max_length=40),
        since: str | None = Query(default=None, max_length=64, description="ISO 8601: results updated at or after this moment"),
        before_id: int | None = Query(default=None, ge=1, description="page: results with a smaller id than this"),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
        include: str | None = Query(default=None, max_length=100, description="`transcript` to include it (needs `read_pii`)"),
    ) -> dict[str, Any]:
        """What finished calls produced, newest first. Page with `before_id`, poll with `since`."""
        if _wants_transcript(include):
            await require_pii(request, what="transcripts")
            await audited(request, "pii.transcript_read", target=("results", "list"), campaign=campaign_id, prospect=prospect_id)
        wanted = _enum_or_422(Disposition, disposition, "disposition")
        rows = await store_of().list_call_results(
            prospect_id=prospect_id,
            campaign_id=campaign_id,
            disposition=wanted,
            limit=limit_of(limit),
            since=aware(_moment_or_422(since, "since")),
            before_id=before_id,
        )
        transcript = _wants_transcript(include)
        return {
            "results": [result_dict(r, include_transcript=transcript) for r in rows],
            "count": len(rows),
            "next_before_id": rows[-1].id if len(rows) >= limit_of(limit) and rows[-1].id is not None else None,
        }

    @router.get("/results/{attempt_id}", dependencies=[Depends(need_pii)])
    async def get_result(request: Request, attempt_id: int = Path(ge=1)) -> dict[str, Any]:
        """One call's full result, transcript included, with the person and the campaign. Needs `read_pii`."""
        store = store_of()
        result = await store.get_call_result(attempt_id)
        if result is not None:
            await audited(request, "pii.transcript_read", target=("call", attempt_id))
        if result is None:
            attempt = await store.get_attempt(attempt_id)
            if attempt is None:
                raise ApiError(404, "call_not_found", f"No call attempt with id {attempt_id}.", attempt_id=attempt_id)
            raise ApiError(404, "result_not_ready", f"Call {attempt_id} is {attempt.status.value}; no result has been written yet.", attempt_id=attempt_id, current_status=attempt.status.value)
        prospect = await store.get_prospect(result.prospect_id)
        campaign = await store.get_campaign(result.campaign_id) if result.campaign_id else None
        transfers = await _quiet(store.list_transfers(call_attempt_id=attempt_id)) or []
        return {
            "result": result_dict(result, include_transcript=True),
            "prospect": prospect_dict(prospect) if prospect else None,
            "campaign": campaign_dict(campaign) if campaign else None,
            "transfers": [transfer_dict(t) for t in transfers],
        }

    @router.get("/meetings")
    async def list_meetings(
        prospect_id: int | None = Query(default=None),
        from_time: str | None = Query(default=None, alias="from", description="ISO 8601: meetings starting at or after this moment"),
        status: str | None = Query(default="BOOKED"),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        """Meetings the agent booked, soonest first."""
        wanted = None if (status or "").lower() == "all" else _enum_or_422(MeetingStatus, status, "status")
        rows = await store_of().list_meetings(prospect_id=prospect_id, from_time=aware(_moment_or_422(from_time, "from")), status=wanted, limit=limit_of(limit))
        return {"meetings": [meeting_dict(m) for m in rows], "count": len(rows)}

    @router.get("/events")
    async def list_events(
        request: Request,
        state_: str | None = Query(default=None, alias="state", max_length=40),
        kind: str | None = Query(default=None, max_length=40),
        prospect_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
        include: str | None = Query(default=None, max_length=100, description="`payload` to include what was sent (needs `read_pii`)"),
    ) -> dict[str, Any]:
        """The outbox: what has been sent to the automation platform, and what has not."""
        store = store_of()
        wanted = _enum_or_422(AutomationEventState, state_, "state")
        if kind is not None and kind.strip().lower() not in AUTOMATION_EVENT_KINDS:
            raise ApiError(422, "invalid_kind", f"kind {kind!r} is not one of: {', '.join(AUTOMATION_EVENT_KINDS)}.")
        with_payload = (include or "").strip().lower() == "payload"
        if with_payload:
            # A payload is the prospect, the result and (on request) the
            # transcript, exactly as the automation platform received them.
            await require_pii(request, what="event payloads")
        rows = await store.list_automation_events(
            state=wanted, kind=kind.strip().lower() if kind else None, prospect_id=prospect_id, limit=limit_of(limit)
        )
        return {
            "events": [event_dict(e, include_payload=with_payload) for e in rows],
            "count": len(rows),
            "counts": await store.automation_event_counts(),
        }

    @router.get("/events/{event_id}", dependencies=[Depends(need_pii)])
    async def get_event(event_id: int = Path(ge=1)) -> dict[str, Any]:
        """One outbox row, with the payload that was (or will be) sent. Needs `read_pii`."""
        found = await store_of().get_automation_event(event_id)
        if found is None:
            raise ApiError(404, "event_not_found", f"No event with id {event_id}.", event_id=event_id)
        return {"event": event_dict(found, include_payload=True)}

    @router.post("/events/{event_id}/retry", dependencies=[Depends(need_manage)])
    async def retry_event(request: Request, event_id: int = Path(ge=1)) -> dict[str, Any]:
        """Reopen one event for delivery. A row being delivered right now is left alone. Needs `manage`."""
        store = store_of()
        found = await store.get_automation_event(event_id)
        if found is None:
            raise ApiError(404, "event_not_found", f"No event with id {event_id}.", event_id=event_id)
        reopened = await store.retry_automation_events(event_id=event_id)
        found = await store.get_automation_event(event_id) or found
        if reopened:
            await audited(request, "event.retry", target=("event", event_id), kind=found.kind)
        return {"event": event_dict(found), "changed": reopened > 0}

    # The audit log (Phase 18) -------------------------------------------------------

    @router.get("/audit", dependencies=[Depends(need_manage)])
    async def list_audit(
        action: str | None = Query(default=None, max_length=80, description="an action or a prefix: `auth.`, `campaign.start`"),
        actor: str | None = Query(default=None, max_length=80),
        since: str | None = Query(default=None, max_length=64, description="ISO 8601"),
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        """Who did what, newest first. Needs `manage`."""
        store = store_of()
        moment = aware(_moment_or_422(since, "since"))
        if action is not None and (_CONTROL.search(action) or not action.strip()):
            raise ApiError(422, "invalid_action", "action must be printable text.")
        rows = await store.list_audit(
            action=action.strip() if action else None,
            actor=actor.strip() if actor else None,
            since=moment,
            before_id=before_id,
            limit=limit_of(limit),
        )
        return {
            "entries": [entry.to_dict() for entry in rows],
            "count": len(rows),
            "counts": await store.audit_counts(since=moment),
            "next_before_id": rows[-1].id if len(rows) >= limit_of(limit) and rows[-1].id is not None else None,
        }

    app.include_router(router)
    return app


# --- Helpers ----------------------------------------------------------------------------


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """JSON rows as the CSV text the importer already knows how to read.

    The header aliasing, the phone normalisation and the duplicate detection
    all live in `csv_import.py`; turning rows into a CSV is how they are
    reused rather than reimplemented. Nested values are kept as JSON text,
    which lands in `custom_data` like any unknown column.
    """
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(str(key))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in headers})
    return buffer.getvalue()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _enum_or_422(enum: type[Any], raw: str | None, name: str) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return enum(raw.strip().upper())
    except ValueError as exc:
        raise ApiError(
            422, f"invalid_{name}", f"{name} {raw!r} is not one of: {', '.join(m.value for m in enum)}."
        ) from exc


def _moment_or_422(raw: str | None, name: str) -> datetime | None:
    """An ISO 8601 query value, read leniently.

    A `+05:00` offset arrives as ` 05:00` when the client did not URL-encode
    the plus — n8n expressions rarely do — so a space before the offset is
    read as the plus it was. `Z` is accepted. Naive values are left naive for
    `aware()` to place in the campaign's timezone.
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    if " " in text and text.count(" ") == 1 and text.rsplit(" ", 1)[1][:1].isdigit() and ":" in text.rsplit(" ", 1)[1]:
        text = text.replace(" ", "+")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(422, f"invalid_{name}", f"{name} {raw!r} is not an ISO 8601 moment, e.g. 2026-09-07T10:00:00Z.") from exc


def _wants_transcript(include: str | None) -> bool:
    return "transcript" in {part.strip().lower() for part in (include or "").split(",")}


async def _quiet(operation: Awaitable[Any]) -> Any:
    """Await a read against a table a re-init adds; None when the table is missing."""
    try:
        return await operation
    except CampaignStoreError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc or url


__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "IDEMPOTENCY_HEADER",
    "MAX_LIMIT",
    "PING_PATH",
    "REPLAYED_HEADER",
    "ApiError",
    "ApiSettings",
    "CampaignAction",
    "create_automation_app",
]
