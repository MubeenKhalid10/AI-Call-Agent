"""Environment configuration, validated at startup.

The point of this module is that a missing or misspelled environment variable
fails immediately with a message naming the variable, rather than surfacing
thirty seconds into a call as an opaque authentication error from a vendor SDK.

Phase 2 adds the realtime tuning knobs — turn detection, VAD, TTS streaming
mode, silence and disconnect handling. Every one of them has a default that is
right for a normal conversation, so an empty `.env` still gets a good agent; the
knobs exist so that a tuning session is an edit to `.env` rather than a code
change.

Phase 3 adds the knowledge base. One of its settings is not like the others:
`KB_DATABASE_URL` has no useful default, because a database only exists once
somebody has made one. It is therefore required whenever `KB_ENABLED` is true,
and validated here so that a missing knowledge base is a one-second startup
failure naming the variable rather than an agent that takes a call and then
cannot answer anything.

Phase 4 adds telephony, and its settings break the rule the rest of this module
follows. Everything else is validated at startup and stops the process when it
is wrong; telephony is validated **at the point of use**. The reason is that
telephony is optional: the browser agent is how this is developed and tested,
and demanding a carrier account before the bot will boot would make every
developer without one unable to run anything. So a missing carrier credential is
not a startup failure — it is a one-second failure the first time you try to
dial, from `TelephonyConfig.require_outbound`, naming the variable. A bot with no
credentials can even *answer* a call; see `telephony/transport.py`.

Phase 6 adds `SalesConfig`, and it goes further in the same direction:
**nothing in it is required and nothing in it is defaulted to a placeholder.**
An unset company name does not stop the bot and does not become "Acme"; it
becomes an instruction telling the agent it has not been told which company it
is calling for and must not invent one. The startup log names the gaps so that
running without them is a decision rather than a discovery.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .compliance.policy import (
    DEFAULT_AI_DISCLOSURE,
    DEFAULT_RECORDING_DISCLOSURE,
    MAX_DISCLOSURE_CHARS,
    CompliancePolicy,
    Disclosure,
    PolicyResolver,
    RetryDelays,
    parse_jurisdictions,
)
from .embeddings import DEFAULT_EMBEDDING_MODEL
from .scheduling.hours import BusinessHours
from .security.http import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_TRUSTED_PROXIES,
    cors_problems,
    parse_networks,
)
from .security.roles import Role, UserDirectory
from .security.sessions import DEFAULT_TTL_SECS as DEFAULT_SESSION_TTL_SECS
from .security.sessions import MIN_SECRET_LENGTH

DEFAULT_CARTESIA_VOICE_ID = "41e97793-a58d-40b7-8430-83465e186f94"
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"
# Pipecat's own default for `DeepgramTTSService`. Deepgram names a voice and its
# model in one string (aura-2-<voice>-<language>); override with DEEPGRAM_TTS_VOICE.
DEFAULT_DEEPGRAM_TTS_VOICE = "aura-2-helena-en"

# Default model per LLM provider. Override with <PROVIDER>_MODEL in .env.
# Provider catalogues change; if one of these 404s, set the env var instead.
DEFAULT_MODELS = {
    # Measured on this account 2026-09-01: 324 ms time-to-first-token, the
    # fastest of Groq's current catalogue, and it needs no special settings.
    # Avoid openai/gpt-oss-* here: they are reasoning models that return EMPTY
    # content unless you also pass reasoning_effort="low", which is a silent
    # failure mid-call. Groq retired the Llama models entirely.
    "groq": "qwen/qwen3.8-27b",
    "gemini": "gemini-3.6-flash",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5-mini",
    "cerebras": "qwen-3.8-27b",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "mistral": "mistral-small-latest",
    "ollama": "llama3.2",
}

# Default model per STT provider.
DEFAULT_STT_MODELS = {
    # Flux is Deepgram's conversational model on /v2/listen: it transcribes and
    # decides end-of-turn server side, from the acoustics and the words rather
    # than from silence alone.
    "deepgram_flux": "flux-general-en",
    # Classic streaming transcription on /v1/listen. Turn detection is then done
    # locally by Silero VAD plus the Smart Turn v3 analyser.
    "deepgram": "nova-3",
}

# API key env var per LLM provider. Ollama runs locally and needs none.
_LLM_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "ollama": None,
}

_STT_KEY_ENV = {"deepgram_flux": "DEEPGRAM_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}
_TTS_KEY_ENV = {
    "cartesia": "CARTESIA_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}

_WHERE_TO_GET = {
    "DEEPGRAM_API_KEY": "https://console.deepgram.com ($200 free credit)",
    "CARTESIA_API_KEY": "https://play.cartesia.ai/keys (free tier)",
    "ELEVENLABS_API_KEY": "https://elevenlabs.io/app/settings/api-keys",
    "GROQ_API_KEY": "https://console.groq.com/keys (free, no card required)",
    "ANTHROPIC_API_KEY": "https://console.anthropic.com/settings/keys (paid credits required)",
    "OPENAI_API_KEY": "https://platform.openai.com/api-keys (paid credits required)",
    "CEREBRAS_API_KEY": "https://cloud.cerebras.ai (free tier)",
    "OPENROUTER_API_KEY": "https://openrouter.ai/keys (some models free)",
    "MISTRAL_API_KEY": "https://console.mistral.ai/api-keys (free tier)",
    "GEMINI_API_KEY": "https://aistudio.google.com/apikey",
}

SUPPORTED = {
    "stt": tuple(_STT_KEY_ENV),
    "llm": tuple(_LLM_KEY_ENV),
    "tts": tuple(_TTS_KEY_ENV),
}

# What `ECHO_SUPPRESSION` accepts, and what each value costs.
#
# The problem it solves: with no headphones, the agent's own voice comes out of
# the speakers, goes back in the microphone, and is transcribed as the caller
# talking. The agent then answers itself, and because every reply feeds the next
# one the conversation runs away on its own. It is unmistakable in a transcript
# — the "user" turn is the bot's previous sentence.
#
# Normally something upstream cancels that echo: headphones make it physically
# impossible, browsers run acoustic echo cancellation (the Pipecat client asks
# for it), and phone networks cancel echo on the line. This setting is for when
# none of that is enough, which in practice means a laptop on speaker.
ECHO_SUPPRESSION_MODES = ("off", "greeting", "always")

# What `BARGE_IN_TRIGGER` accepts on the Flux path. "vad" lets the local VAD cut
# the agent off while it is speaking (about 0.2 s after the caller's first
# sound); "flux" waits for Deepgram's StartOfTurn, which needs recognised words
# and was measured 0.8-1.1 s after the caller began. See `turns.py`.
BARGE_IN_TRIGGERS = ("vad", "flux")

# What `LLM_REASONING_FORMAT` accepts, for a Groq reasoning model. "hidden" asks
# the provider for the final answer only; "parsed" keeps the reasoning in its
# own field, which Pipecat ignores; "off" sends nothing and takes the
# provider's default — which, with tools advertised, streams the model's
# thinking into the answer. Observed spoken aloud on a real call, 2026-09-08.
REASONING_FORMATS = ("hidden", "parsed", "off")

# What `KB_RETRIEVAL_MODE` accepts. "always" is Phase 3's unconditional search;
# "auto" gates it on whether the turn could be an information request at all.
KB_RETRIEVAL_MODES = ("auto", "always")

# Carriers this project can place an outbound call through. Shorter than the
# list of carriers whose audio it can *receive* (`telephony.TELEPHONY_TRANSPORTS`),
# because receiving audio needs no code from us and placing a call does — see
# `src/telephony/base.py`.
SUPPORTED_TELEPHONY = ("twilio", "signalwire")

# Per-carrier settings, as {logical name: environment variable}. The logical
# names are what `telephony.make_provider` passes to the provider's constructor;
# the environment variable names are what appears in an error message, so the
# person reading it knows which line of `.env` to edit.
#
# Not all of these are secret — SignalWire's space URL is a hostname — but they
# are grouped together because they share the one property that matters here:
# the provider cannot be built without them, and none of them can be guessed.
_TELEPHONY_CREDENTIALS = {
    "twilio": {
        "account_sid": "TWILIO_ACCOUNT_SID",
        "auth_token": "TWILIO_AUTH_TOKEN",
    },
    "signalwire": {
        "project_id": "SIGNALWIRE_PROJECT_ID",
        "api_token": "SIGNALWIRE_API_TOKEN",
        "space_url": "SIGNALWIRE_SPACE_URL",
    },
}

# Phase 14: what a carrier keys its webhook signatures with, when that is not
# one of the credentials above. Twilio signs with the auth token it already
# has; SignalWire signs with a separate signing key from the dashboard's API
# credentials page. Optional — it is needed to *receive* events, not to dial —
# so a provider missing it dials as before and simply gets no callbacks.
_TELEPHONY_WEBHOOK_SIGNING = {
    "signalwire": "SIGNALWIRE_SIGNING_KEY",
}

# Phase 14: who serves the webhook route. `bot` mounts it on the bot's own web
# server, which is the only public address a single tunnel gives you; the
# handler does an HMAC and two short database writes and never touches a
# pipeline. `standalone` leaves the bot alone and expects `uv run webhooks.py`
# behind the same public address (a proxy routing the path to it).
WEBHOOK_RECEIVERS = ("bot", "standalone")

_WHERE_TO_GET_TELEPHONY = {
    "twilio": "https://console.twilio.com (the account SID and auth token are on the dashboard)",
    "signalwire": (
        "https://signalwire.com — sign up, then API in the sidebar: the Project ID, an API "
        "token you create there, and the space URL shown at the top of the dashboard"
    ),
}

# Phase 12: answering-machine detection by the carrier, requested when a call
# is placed. `off` sends nothing. `async` asks the carrier to detect in the
# background — the call connects at once, so a person hears no delay, and the
# verdict appears on the call resource a few seconds later, where the bot and
# the dialer poll for it. `sync` makes the carrier decide *before* connecting
# the call: the verdict is known when the bot answers, at the cost of two to
# four seconds of silence for every person who picks up. Both cost a small fee
# per call on Twilio; see `.env.example`.
MACHINE_DETECTION_MODES = ("off", "async", "sync")

# Phase 12: the bot's own answering-machine detection, from what the line
# sounds like. `heuristic` watches the first caller turns for recorded-greeting
# phrases and for a greeting that runs on without pausing; `off` leaves the
# agent to talk to the voicemail as it did before. Phone calls only — a browser
# or eval session is never judged.
VOICEMAIL_DETECTION_MODES = ("off", "heuristic")

# Phase 12: what to do when a machine answers. `hangup` ends the call at once
# (the cheap and default choice); `message` waits for the greeting to end, then
# speaks `VOICEMAIL_MESSAGE` and hangs up.
VOICEMAIL_ACTIONS = ("hangup", "message")

# Calendars the agent can book into (Phase 7). `none` disables booking: the
# agent records the intent and says a colleague will confirm. `local` is a real
# calendar in this system's own database — business hours minus what is booked —
# and the default, because it needs no account and makes the booking flow
# testable end to end. `calcom` books into a Cal.com event type.
CALENDAR_PROVIDERS = ("none", "local", "calcom")

# CRMs a finished call's result can be filed with (Phase 15). `none` is the
# default: the result row is written either way, and the sync is a separate
# process that a CRM has to be configured for. Adding one is a provider module
# in `src/crm/`, an entry here, and its credentials in `CrmConfig`.
CRM_PROVIDERS = ("none", "hubspot")

#: Phase 17. The events the automation outbox can emit to n8n (or anything
#: that takes a signed POST). Mirrored by `campaigns.store.AUTOMATION_EVENT_KINDS`.
AUTOMATION_EVENT_KINDS = (
    "call.completed",
    "call.updated",
    "lead.qualified",
    "meeting.booked",
    "callback.scheduled",
    "campaign.completed",
)

# E.164 shape, for the one phone number this module validates itself — the
# transfer destination. Prospect numbers go through libphonenumber in
# `campaigns/phone.py`; this only asks whether a *configured* value is dialable.
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


@dataclass(frozen=True)
class CalendarConfig:
    """Where meetings get booked, and when they may be offered. Phase 7.

    Grouped like `TelephonyConfig` and `SalesConfig`: one feature, one object.
    Validated at startup, unlike telephony, because a bad timezone or an hours
    string that reads backwards would offer somebody a meeting at four in the
    morning on the very first call — and because none of it needs an account
    except the Cal.com path, whose credentials are checked only when it is
    selected.

    Attributes:
        provider: One of `CALENDAR_PROVIDERS`.
        timezone: IANA zone the calendar keeps, the agent states the time in,
            and the tools expect times in. UTC unless set, and the startup log
            says so — a bot calling Karachi should not be left on UTC quietly.
        slot_minutes: Slot length, and for Cal.com the event type's length.
        business_hours / business_days: When the local calendar offers slots.
            Cal.com applies its own availability; these are ignored there.
        max_days_ahead: The furthest day the agent may check or book.
        min_notice_minutes: Nothing is offered sooner than this from now.
    """

    provider: str
    timezone: str
    slot_minutes: int
    business_hours: str
    business_days: str
    max_days_ahead: int
    min_notice_minutes: int
    calcom_api_key: str | None
    calcom_event_type_id: int | None
    calcom_api_base: str | None
    # Phase 16: ceiling on one Cal.com request. Fifteen seconds, because the
    # request is made inside a tool turn with a person waiting; a booking that
    # exceeds it is looked up rather than repeated.
    calcom_timeout_secs: float = 15.0

    @classmethod
    def from_env(cls, problems: list[str]) -> CalendarConfig:
        """Read the calendar settings, collecting every problem into `problems`."""
        provider = _choice("CALENDAR_PROVIDER", "local", CALENDAR_PROVIDERS, problems)
        timezone = _clean(os.getenv("CALENDAR_TIMEZONE")) or "UTC"
        if timezone.upper() != "UTC":
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                problems.append(
                    f"CALENDAR_TIMEZONE is {timezone!r}, which is not a known timezone. Use an "
                    f"IANA name such as Asia/Karachi, Europe/London or America/New_York."
                )
                timezone = "UTC"

        hours = _clean(os.getenv("CALENDAR_BUSINESS_HOURS")) or "09:00-17:00"
        days = _clean(os.getenv("CALENDAR_BUSINESS_DAYS")) or "mon-fri"
        try:
            BusinessHours.parse(hours, days)
        except ValueError as exc:
            problems.append(f"CALENDAR_BUSINESS_HOURS / CALENDAR_BUSINESS_DAYS: {exc}")
            hours, days = "09:00-17:00", "mon-fri"

        num = _Numbers(problems)
        event_type = num.optional_int("CALCOM_EVENT_TYPE_ID", 1, 10**12)
        api_key = _clean(os.getenv("CALCOM_API_KEY"))
        if provider == "calcom" and (not api_key or event_type is None):
            problems.append(
                "CALENDAR_PROVIDER is calcom, so CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID must both "
                "be set. Get them at https://app.cal.com/settings/developer/api-keys and from the "
                "event type's URL."
            )

        return cls(
            provider=provider,
            timezone=timezone,
            slot_minutes=num.integer("CALENDAR_SLOT_MINUTES", 30, 5, 240),
            business_hours=hours,
            business_days=days,
            max_days_ahead=num.integer("CALENDAR_MAX_DAYS_AHEAD", 30, 1, 365),
            min_notice_minutes=num.integer("CALENDAR_MIN_NOTICE_MINUTES", 60, 0, 10080),
            calcom_api_key=api_key,
            calcom_event_type_id=event_type,
            calcom_api_base=_clean(os.getenv("CALCOM_API_BASE")),
            calcom_timeout_secs=num.number("CALCOM_TIMEOUT_SECS", 15.0, 1.0, 120.0),
        )

    @property
    def enabled(self) -> bool:
        """Whether the agent can check and book a calendar at all."""
        return self.provider != "none"

    @property
    def hours(self) -> BusinessHours:
        """The parsed business hours. Valid, because `from_env` checked them."""
        return BusinessHours.parse(self.business_hours, self.business_days)

    def describe(self) -> str:
        """One line for the startup log. Never the API key."""
        if not self.enabled:
            return f"off ({self.timezone})"
        if self.provider == "calcom":
            return f"calcom event {self.calcom_event_type_id}, {self.slot_minutes}min, {self.timezone}"
        return f"local {self.business_hours} {self.business_days}, {self.slot_minutes}min, {self.timezone}"


@dataclass(frozen=True)
class CrmConfig:
    """Which CRM finished calls are filed with, and how the sync paces itself. Phase 15.

    Grouped like `CalendarConfig`: one feature, one object. The provider's
    credentials are demanded only when it is selected, so a `.env` with no
    CRM boots exactly as before and the sync command says what it needs.
    Nothing here is read by the bot: the sync is `campaign.py crm-sync`, in a
    process of its own.

    Attributes:
        provider: One of `CRM_PROVIDERS`.
        hubspot_access_token: A private-app access token. Never logged.
        hubspot_api_base: Overridable for a test double.
        custom_properties: Whether the provider files the structured facts in
            custom fields it creates (`ai_*` on HubSpot contacts), or keeps to
            standard fields only.
        sync_unanswered: Whether calls nobody answered are filed too.
        sync_poll_secs: How often the sync process looks for new results.
        sync_batch: Results claimed per pass.
        sync_max_attempts: Passes a result may fail transiently before it is
            closed as failed.
        sync_retry_secs / sync_max_retry_secs: The backoff after a transient
            failure — doubling from the first, capped at the second.
        sync_stale_secs: A row claimed this long ago by a syncer that never
            reported is claimed again.
    """

    provider: str
    hubspot_access_token: str | None
    hubspot_api_base: str | None
    custom_properties: bool
    sync_unanswered: bool
    sync_poll_secs: float
    sync_batch: int
    sync_max_attempts: int
    sync_retry_secs: float
    sync_max_retry_secs: float
    sync_stale_secs: float

    @classmethod
    def from_env(cls, problems: list[str]) -> CrmConfig:
        """Read the CRM settings, collecting every problem into `problems`."""
        provider = _choice("CRM_PROVIDER", "none", CRM_PROVIDERS, problems)
        token = _clean(os.getenv("HUBSPOT_ACCESS_TOKEN"))
        if provider == "hubspot" and not token:
            problems.append(
                "CRM_PROVIDER is hubspot, so HUBSPOT_ACCESS_TOKEN must be set. Create a private app "
                "at https://app.hubspot.com → Settings → Integrations → Private Apps, with the "
                "crm.objects.contacts.read/write and crm.objects.calls.read/write scopes "
                "(and crm.schemas.contacts.write for the custom properties)."
            )
        num = _Numbers(problems)
        retry = num.number("CRM_SYNC_RETRY_SECS", 60.0, 1.0, 86400.0)
        return cls(
            provider=provider,
            hubspot_access_token=token,
            hubspot_api_base=_clean(os.getenv("HUBSPOT_API_BASE")),
            custom_properties=_flag("CRM_CUSTOM_PROPERTIES", True, problems),
            sync_unanswered=_flag("CRM_SYNC_UNANSWERED", True, problems),
            sync_poll_secs=num.number("CRM_SYNC_POLL_SECS", 15.0, 1.0, 3600.0),
            sync_batch=num.integer("CRM_SYNC_BATCH", 20, 1, 200),
            sync_max_attempts=num.integer("CRM_SYNC_MAX_ATTEMPTS", 8, 1, 100),
            sync_retry_secs=retry,
            sync_max_retry_secs=max(retry, num.number("CRM_SYNC_MAX_RETRY_SECS", 3600.0, 1.0, 86400.0)),
            sync_stale_secs=num.number("CRM_SYNC_STALE_SECS", 900.0, 30.0, 86400.0),
        )

    @property
    def enabled(self) -> bool:
        """Whether a CRM is selected at all."""
        return self.provider != "none"

    def require_credentials(self) -> None:
        """Raise unless the selected CRM's credentials are present."""
        if self.provider == "hubspot" and not self.hubspot_access_token:
            raise ConfigError("HUBSPOT_ACCESS_TOKEN is not set, so nothing can be filed with HubSpot.")

    def describe(self) -> str:
        """One line for the startup log. Never the token."""
        if not self.enabled:
            return "off (CRM_PROVIDER=none)"
        answered = "every call" if self.sync_unanswered else "answered calls only"
        return (
            f"{self.provider}, {answered}, poll {self.sync_poll_secs:g}s, batch {self.sync_batch}, "
            f"{self.sync_max_attempts} attempts from {self.sync_retry_secs:g}s"
        )


@dataclass(frozen=True)
class AutomationConfig:
    """The automation integration: the API n8n calls, and the events it is sent. Phase 17.

    Two halves, both optional, both off until configured, and neither read
    by the bot:

    * **The API** (`uv run automation.py`) — a small authenticated HTTP surface
      over the campaign rows: prospects, campaigns, calls, callbacks, results.
      On while `AUTOMATION_API_KEYS` holds at least one key; it refuses to
      serve without one, because every write it takes can make a phone ring.
    * **The events** — the outbox deliverer, which POSTs `call.completed`,
      `lead.qualified`, `meeting.booked` and the rest to a webhook URL,
      signed, once each. On while `AUTOMATION_WEBHOOK_URL` (or a per-kind
      `AUTOMATION_WEBHOOK_URL_<KIND>`) is set.

    Attributes:
        api_keys: Accepted bearer keys. More than one so a key can be rotated
            without a gap. Never logged.
        host / port: Where the API binds. Loopback by default.
        webhook_url: Where every enabled event kind is sent, unless
            `webhook_urls` names another address for that kind.
        webhook_urls: Per-kind overrides, keyed by kind.
        webhook_secret: Signs every delivery (`X-Aiva-Signature`, HMAC-SHA256
            over the timestamp and the body). Optional; recommended.
        webhook_auth_header / webhook_auth_token: A static header the
            receiver checks — n8n's Webhook node "Header Auth" credential.
        events: Which kinds are created and delivered.
        events_since: Only facts recorded at or after this moment become
            events. Bounds a first run on a database with history.
        settle_secs: How long a call result must have been unchanged before
            its events are created — the carrier's thin result and the
            conversation's rich one land seconds apart.
        poll_secs / batch: How often the deliverer looks, and how many
            events it takes per pass.
        max_attempts / retry_secs / max_retry_secs: The backoff after a
            transient failure — doubling from the first, capped at the second,
            for up to `max_attempts` passes.
        stale_secs: A row claimed this long ago by a deliverer that never
            reported is claimed again.
        timeout_secs: Per delivery request.
        idempotency_ttl_secs: How long a stored `Idempotency-Key` answer is
            replayed.
        include_transcript: Whether `call.*` payloads carry the transcript.
            Off by default: a transcript is the largest thing in a result and
            most workflows read the summary; `GET /api/v1/results/{id}` has
            it on demand.
    """

    api_keys: tuple[str, ...]
    host: str
    port: int
    webhook_url: str | None
    webhook_urls: dict[str, str]
    webhook_secret: str | None
    webhook_auth_header: str | None
    webhook_auth_token: str | None
    events: tuple[str, ...]
    events_since: datetime | None
    settle_secs: float
    poll_secs: float
    batch: int
    max_attempts: int
    retry_secs: float
    max_retry_secs: float
    stale_secs: float
    timeout_secs: float
    idempotency_ttl_secs: float
    include_transcript: bool
    # Phase 18: keys with less than full access. `api_keys` are the admin
    # keys (everything, as before); operator keys may read the people and
    # make phones ring but not close a campaign, retry the outbox or read
    # the audit log; viewer keys read totals and outcomes with the phone
    # numbers masked and the transcripts withheld. See `security/roles.py`.
    operator_api_keys: tuple[str, ...] = ()
    viewer_api_keys: tuple[str, ...] = ()
    # Whether `/api/v1/docs` and the OpenAPI schema are served. On by default;
    # a production deployment may turn them off to publish less of its surface.
    docs_enabled: bool = True

    @classmethod
    def from_env(cls, problems: list[str]) -> AutomationConfig:
        """Read the automation settings, collecting every problem into `problems`."""
        raw_keys = _clean(os.getenv("AUTOMATION_API_KEYS")) or _clean(os.getenv("AUTOMATION_API_KEY"))
        api_keys = _keys(raw_keys, "AUTOMATION_API_KEYS", problems)
        operator_keys = _keys(_clean(os.getenv("AUTOMATION_OPERATOR_API_KEYS")), "AUTOMATION_OPERATOR_API_KEYS", problems)
        viewer_keys = _keys(_clean(os.getenv("AUTOMATION_VIEWER_API_KEYS")), "AUTOMATION_VIEWER_API_KEYS", problems)
        shared = set(api_keys) & set(operator_keys) | set(api_keys) & set(viewer_keys) | set(operator_keys) & set(viewer_keys)
        if shared:
            problems.append(
                "A key appears in more than one of AUTOMATION_API_KEYS, AUTOMATION_OPERATOR_API_KEYS "
                "and AUTOMATION_VIEWER_API_KEYS; a key must hold exactly one role."
            )

        num = _Numbers(problems)
        url = _url("AUTOMATION_WEBHOOK_URL", problems)
        per_kind: dict[str, str] = {}
        known_names = set()
        for kind in AUTOMATION_EVENT_KINDS:
            name = "AUTOMATION_WEBHOOK_URL_" + kind.upper().replace(".", "_")
            known_names.add(name)
            value = _url(name, problems)
            if value:
                per_kind[kind] = value
        for name in sorted(os.environ):
            if name.startswith("AUTOMATION_WEBHOOK_URL_") and name not in known_names:
                problems.append(
                    f"{name} names no event kind. Per-kind URLs are AUTOMATION_WEBHOOK_URL_<KIND> "
                    f"for one of: {', '.join(AUTOMATION_EVENT_KINDS)}."
                )

        token = _clean(os.getenv("AUTOMATION_WEBHOOK_AUTH_TOKEN"))
        header = _clean(os.getenv("AUTOMATION_WEBHOOK_AUTH_HEADER"))
        if header and not token:
            problems.append(
                "AUTOMATION_WEBHOOK_AUTH_HEADER is set but AUTOMATION_WEBHOOK_AUTH_TOKEN is not; "
                "the header needs a value to carry."
            )
        if token and not header:
            header = "X-Aiva-Key"

        chosen = tuple(
            part.strip().lower()
            for part in (_clean(os.getenv("AUTOMATION_EVENTS")) or "").split(",")
            if part.strip()
        )
        unknown = [kind for kind in chosen if kind not in AUTOMATION_EVENT_KINDS]
        if unknown:
            problems.append(
                f"AUTOMATION_EVENTS names unknown kind(s) {', '.join(unknown)}; "
                f"use any of: {', '.join(AUTOMATION_EVENT_KINDS)}."
            )
        events = tuple(kind for kind in AUTOMATION_EVENT_KINDS if not chosen or kind in chosen)

        retry = num.number("AUTOMATION_RETRY_SECS", 30.0, 1.0, 86400.0)
        return cls(
            api_keys=api_keys,
            host=_clean(os.getenv("AUTOMATION_HOST")) or "127.0.0.1",
            port=num.integer("AUTOMATION_PORT", 7890, 1, 65535),
            webhook_url=url,
            webhook_urls=per_kind,
            webhook_secret=_clean(os.getenv("AUTOMATION_WEBHOOK_SECRET")),
            webhook_auth_header=header,
            webhook_auth_token=token,
            events=events,
            events_since=_moment("AUTOMATION_EVENTS_SINCE", problems),
            settle_secs=num.number("AUTOMATION_SETTLE_SECS", 30.0, 0.0, 3600.0),
            poll_secs=num.number("AUTOMATION_POLL_SECS", 10.0, 1.0, 3600.0),
            batch=num.integer("AUTOMATION_BATCH", 20, 1, 500),
            max_attempts=num.integer("AUTOMATION_MAX_ATTEMPTS", 12, 1, 100),
            retry_secs=retry,
            max_retry_secs=max(retry, num.number("AUTOMATION_MAX_RETRY_SECS", 1800.0, 1.0, 86400.0)),
            stale_secs=num.number("AUTOMATION_STALE_SECS", 600.0, 30.0, 86400.0),
            timeout_secs=num.number("AUTOMATION_TIMEOUT_SECS", 15.0, 1.0, 120.0),
            idempotency_ttl_secs=num.number(
                "AUTOMATION_IDEMPOTENCY_TTL_SECS", 86400.0, 60.0, 30 * 86400.0
            ),
            include_transcript=_flag("AUTOMATION_INCLUDE_TRANSCRIPT", False, problems),
            operator_api_keys=operator_keys,
            viewer_api_keys=viewer_keys,
            docs_enabled=_flag("AUTOMATION_DOCS_ENABLED", True, problems),
        )

    @property
    def api_enabled(self) -> bool:
        """Whether the API may serve: at least one key, of any role, is configured."""
        return bool(self.api_keys or self.operator_api_keys or self.viewer_api_keys)

    @property
    def key_count(self) -> int:
        """How many keys are configured, across every role."""
        return len(self.api_keys) + len(self.operator_api_keys) + len(self.viewer_api_keys)

    def role_for_key(self, presented: str | None) -> tuple[Role, str] | None:
        """The role a presented key holds and the key's label, or None. Phase 18.

        Every configured key of every role is compared, in constant time,
        whether or not an earlier one matched — so the time taken says
        nothing about which key (if any) was right. The label
        (`operator-key#2`) is what the audit log records; the key never is.
        """
        if not presented:
            return None
        import hmac

        wanted = presented.encode("utf-8")
        found: tuple[Role, str] | None = None
        for role, keys in (
            (Role.ADMIN, self.api_keys),
            (Role.OPERATOR, self.operator_api_keys),
            (Role.VIEWER, self.viewer_api_keys),
        ):
            for index, key in enumerate(keys, start=1):
                if hmac.compare_digest(wanted, key.encode("utf-8")) and found is None:
                    found = (role, f"{role.value}-key#{index}")
        return found

    @property
    def targets(self) -> dict[str, str]:
        """Where each enabled event kind is sent. A kind with no URL is absent."""
        found: dict[str, str] = {}
        for kind in self.events:
            url = self.webhook_urls.get(kind) or self.webhook_url
            if url:
                found[kind] = url
        return found

    @property
    def delivery_enabled(self) -> bool:
        """Whether any event kind has somewhere to go."""
        return bool(self.targets)

    def target_for(self, kind: str) -> str | None:
        """The URL one kind is sent to, or None."""
        return self.targets.get(kind)

    def require_api_keys(self) -> None:
        """Raise unless the API has a key to check requests against."""
        if not self.api_enabled:
            raise ConfigError(
                "AUTOMATION_API_KEYS is not set, so the automation API will not serve: every "
                "write it takes can make a phone ring. Generate a key with "
                "`uv run security.py make-key` (or `python -c \"import secrets; "
                "print(secrets.token_urlsafe(32))\"`) and set it; operator and viewer keys go in "
                "AUTOMATION_OPERATOR_API_KEYS / AUTOMATION_VIEWER_API_KEYS."
            )

    def describe(self) -> str:
        """One line for the startup log. Never a key, a token or a secret."""
        api = (
            f"API on {self.host}:{self.port}, {len(self.api_keys)} admin / "
            f"{len(self.operator_api_keys)} operator / {len(self.viewer_api_keys)} viewer key(s)"
            if self.api_enabled
            else "API off (no AUTOMATION_API_KEYS)"
        )
        targets = self.targets
        if not targets:
            delivery = "delivery off (no AUTOMATION_WEBHOOK_URL)"
        else:
            hosts = sorted({_host_of(url) for url in targets.values()})
            delivery = (
                f"{len(targets)} event kind(s) to {', '.join(hosts)}, "
                f"{'signed' if self.webhook_secret else 'unsigned'}, "
                f"{'header auth' if self.webhook_auth_token else 'no header auth'}, "
                f"settle {self.settle_secs:g}s, retry from {self.retry_secs:g}s"
                + (f", since {self.events_since.isoformat(timespec='minutes')}" if self.events_since else "")
            )
        return f"{api}; {delivery}"


def _host_of(url: str) -> str:
    """The host of a URL, for a log line that must not carry a webhook path."""
    from urllib.parse import urlsplit

    return urlsplit(url).netloc or url


def _keys(raw: str | None, name: str, problems: list[str]) -> tuple[str, ...]:
    """A comma-separated key list, de-duplicated, each key long enough to be one."""
    keys = tuple(dict.fromkeys(k.strip() for k in (raw or "").split(",") if k.strip()))
    if any(len(key) < 16 for key in keys):
        problems.append(
            f"{name}: every key must be at least 16 characters. Generate one with "
            f"`uv run security.py make-key`."
        )
    return keys


@dataclass(frozen=True)
class SecurityConfig:
    """Authentication, authorisation and hardening for the servers. Phase 18.

    Read by the dashboard, the automation API and the webhook receiver;
    never by the bot. Every default is the safe end of its trade, and the
    two that are not safe by themselves — `dashboard_auth_disabled` and a
    session secret generated at boot — are printed at startup.

    Attributes:
        users: The dashboard's users, from `DASHBOARD_USERS`
            (`name:role:hash,…`; hashes from `uv run security.py hash-password`).
        dashboard_auth_disabled: `DASHBOARD_AUTH_DISABLED=true` serves the
            dashboard without a login, as Phase 10 did. `dashboard.py`
            refuses to bind anything but loopback in that mode.
        session_secret: Signs the session cookie. Unset: generated at boot,
            so sessions do not survive a restart and two processes do not
            share them; the startup line says so.
        session_ttl_secs: How long a login lasts.
        require_https: Refuse plain-HTTP requests (redirecting a dashboard
            page, refusing an API call) and send HSTS. For production,
            behind a proxy that terminates TLS.
        trusted_proxies: Networks whose `X-Forwarded-For` and
            `X-Forwarded-Proto` are believed. Loopback by default.
        cors_origins: Origins allowed to call from a browser. None by
            default; never a wildcard.
        api_rate_limit: Requests per minute per API key.
        anon_rate_limit: Requests per minute per address for requests that
            carry no valid credential — the budget for guessing.
        login_rate_limit: Login attempts per minute per address.
        max_body_bytes: The largest request body accepted.
        audit_enabled / audit_strict: Whether sensitive actions are written
            to `audit_log`, and whether a write that cannot be recorded is
            refused rather than logged.
        registration_enabled: Phase 27. Whether the application's Register
            page accepts sign-ups (`DASHBOARD_REGISTRATION_ENABLED`, on by
            default). Off: the page says so and the route refuses. A sign-up
            is an active viewer at once; one that asks for operator or admin
            is pending until an admin approves it on the Settings page.
    """

    users: UserDirectory
    dashboard_auth_disabled: bool
    session_secret: str | None
    session_ttl_secs: float
    require_https: bool
    trusted_proxies: tuple[str, ...]
    cors_origins: tuple[str, ...]
    api_rate_limit: int
    anon_rate_limit: int
    login_rate_limit: int
    max_body_bytes: int
    audit_enabled: bool
    audit_strict: bool
    registration_enabled: bool = True

    @classmethod
    def defaults(cls) -> SecurityConfig:
        """The settings with nothing configured: no users, everything else at its default."""
        return cls(
            users=UserDirectory(),
            dashboard_auth_disabled=False,
            session_secret=None,
            session_ttl_secs=float(DEFAULT_SESSION_TTL_SECS),
            require_https=False,
            trusted_proxies=DEFAULT_TRUSTED_PROXIES,
            cors_origins=(),
            api_rate_limit=300,
            anon_rate_limit=30,
            login_rate_limit=5,
            max_body_bytes=DEFAULT_MAX_BODY_BYTES,
            audit_enabled=True,
            audit_strict=False,
        )

    @classmethod
    def from_env(cls, problems: list[str]) -> SecurityConfig:
        """Read the security settings, collecting every problem into `problems`."""
        num = _Numbers(problems)
        users = UserDirectory.parse(os.getenv("DASHBOARD_USERS"), problems)
        secret = _clean(os.getenv("DASHBOARD_SESSION_SECRET"))
        if secret is not None and len(secret) < MIN_SECRET_LENGTH:
            problems.append(
                f"DASHBOARD_SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters; "
                f"make one with `uv run security.py make-secret`."
            )
        # Comma-separated, as `.env.example` and the README say (`0.0.0.0/0,::/0`
        # behind Vercel): neither a CIDR nor an origin contains a comma, and
        # `_list`'s pipe would have made the documented value one bad entry.
        proxies = tuple(_comma_list("SECURITY_TRUSTED_PROXIES")) or DEFAULT_TRUSTED_PROXIES
        parse_networks(proxies, problems)
        origins = tuple(_comma_list("SECURITY_CORS_ORIGINS"))
        problems.extend(cors_problems(origins))
        return cls(
            users=users,
            dashboard_auth_disabled=_flag("DASHBOARD_AUTH_DISABLED", False, problems),
            session_secret=secret,
            session_ttl_secs=num.number("DASHBOARD_SESSION_TTL_SECS", float(DEFAULT_SESSION_TTL_SECS), 60.0, 30 * 86400.0),
            require_https=_flag("SECURITY_REQUIRE_HTTPS", False, problems),
            trusted_proxies=proxies,
            cors_origins=origins,
            api_rate_limit=num.integer("SECURITY_API_RATE_LIMIT", 300, 0, 100_000),
            anon_rate_limit=num.integer("SECURITY_ANON_RATE_LIMIT", 30, 0, 100_000),
            login_rate_limit=num.integer("SECURITY_LOGIN_RATE_LIMIT", 5, 0, 10_000),
            max_body_bytes=num.integer("SECURITY_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, 1024, 512 * 1024 * 1024),
            audit_enabled=_flag("SECURITY_AUDIT_ENABLED", True, problems),
            audit_strict=_flag("SECURITY_AUDIT_STRICT", False, problems),
            registration_enabled=_flag("DASHBOARD_REGISTRATION_ENABLED", True, problems),
        )

    @property
    def dashboard_auth_required(self) -> bool:
        """Whether the dashboard demands a login."""
        return not self.dashboard_auth_disabled

    def describe(self) -> str:
        """One line for the startup log. Never a secret, a hash or a key."""
        login = "login off (DASHBOARD_AUTH_DISABLED)" if self.dashboard_auth_disabled else f"login on ({self.users.describe()})"
        return (
            f"{login}; sessions {'signed with DASHBOARD_SESSION_SECRET' if self.session_secret else 'signed with a boot-time secret'}"
            f", {self.session_ttl_secs / 3600:g}h; "
            f"{'HTTPS required' if self.require_https else 'HTTP allowed (set SECURITY_REQUIRE_HTTPS in production)'}; "
            f"CORS {', '.join(self.cors_origins) if self.cors_origins else 'off'}; "
            f"rate limits {self.api_rate_limit}/min per key, {self.anon_rate_limit}/min anonymous, "
            f"{self.login_rate_limit}/min logins; body up to {self.max_body_bytes // 1024} KiB; "
            f"audit {'strict' if self.audit_strict else 'on' if self.audit_enabled else 'OFF'}; "
            f"sign-up {'on (viewer at once; operator and admin on approval)' if self.registration_enabled else 'off'}"
        )


@dataclass(frozen=True)
class ComplianceConfig:
    """Outbound calling compliance controls. Phase 19.

    Everything here is a *policy the operator configures*; the software
    applies it and records that it did. Nothing here encodes what any law
    requires — `COMPLIANCE.md` says which controls exist and which decisions
    are the operator's. The calling window and the general attempt ceiling
    and retry wait come from Phase 9's `CALLING_*` and Phase 5's
    `CAMPAIGN_*` settings, which stand; this adds what was missing.

    Attributes:
        ai_disclosure / ai_disclosure_required: What the agent says about
            being an AI, and whether it must say it in its opening sentence
            rather than only when asked (Phase 6 already answers honestly
            when asked, under every setting).
        recording_enabled / recording_disclosure / _required: Whether the
            operator records calls (this software does not record audio
            itself), and the sentence the agent must open with when they do.
        retry: Per-outcome retry waits, overriding `CAMPAIGN_RETRY_MINUTES`
            where set.
        jurisdictions: Region code → overrides, from `COMPLIANCE_JURISDICTIONS`
            (JSON). Applied on top of the campaign's settings for a number in
            that region, so a campaign cannot loosen a country's rule.
        default_jurisdiction: The label for numbers whose region is unknown.
        audit_allowed: Write an audit row for every call the gate *allowed*,
            not only the refusals. On by default: "why was this person
            phoned" deserves the same answer as "why not".
    """

    ai_disclosure: str
    ai_disclosure_required: bool
    recording_enabled: bool
    recording_disclosure: str
    recording_disclosure_required: bool
    retry: RetryDelays
    jurisdictions: dict[str, dict[str, Any]]
    default_jurisdiction: str | None
    audit_allowed: bool

    @classmethod
    def from_env(cls, problems: list[str]) -> ComplianceConfig:
        """Read the compliance settings, collecting every problem into `problems`."""
        num = _Numbers(problems)
        ai_text = _clean(os.getenv("COMPLIANCE_AI_DISCLOSURE")) or DEFAULT_AI_DISCLOSURE
        rec_text = _clean(os.getenv("COMPLIANCE_RECORDING_DISCLOSURE")) or DEFAULT_RECORDING_DISCLOSURE
        for name, text in (("COMPLIANCE_AI_DISCLOSURE", ai_text), ("COMPLIANCE_RECORDING_DISCLOSURE", rec_text)):
            if len(text) > MAX_DISCLOSURE_CHARS:
                problems.append(f"{name} may be at most {MAX_DISCLOSURE_CHARS} characters: it is spoken in the opening sentence.")
        recording = _flag("COMPLIANCE_RECORDING_ENABLED", False, problems)
        raw_default = os.getenv("COMPLIANCE_DEFAULT_JURISDICTION")
        default_jurisdiction = _clean(raw_default)
        if default_jurisdiction and len(default_jurisdiction) > 40:
            problems.append("COMPLIANCE_DEFAULT_JURISDICTION is a short label (US, GB, PK), not a description.")
        return cls(
            ai_disclosure=ai_text,
            ai_disclosure_required=_flag("COMPLIANCE_AI_DISCLOSURE_REQUIRED", False, problems),
            recording_enabled=recording,
            recording_disclosure=rec_text,
            recording_disclosure_required=_flag("COMPLIANCE_RECORDING_DISCLOSURE_REQUIRED", recording, problems),
            retry=RetryDelays(
                no_answer=num.optional_float("COMPLIANCE_RETRY_MINUTES_NO_ANSWER", 0.0, 10080.0),
                busy=num.optional_float("COMPLIANCE_RETRY_MINUTES_BUSY", 0.0, 10080.0),
                voicemail=num.optional_float("COMPLIANCE_RETRY_MINUTES_VOICEMAIL", 0.0, 10080.0),
            ),
            jurisdictions=parse_jurisdictions(os.getenv("COMPLIANCE_JURISDICTIONS"), problems),
            default_jurisdiction=default_jurisdiction,
            audit_allowed=_flag("COMPLIANCE_AUDIT_ALLOWED", True, problems),
        )

    def policy(
        self,
        reliability: ReliabilityConfig,
        *,
        max_attempts: int,
        retry_minutes: float,
    ) -> CompliancePolicy:
        """The environment's policy: Phase 9's window and Phase 5's limits plus this."""
        return CompliancePolicy(
            jurisdiction=self.default_jurisdiction,
            calling_hours=reliability.calling_hours,
            calling_days=reliability.calling_days,
            timezone=reliability.calling_timezone,
            enforce_calling_hours=reliability.enforce_calling_hours,
            max_attempts=max_attempts,
            retry_minutes=retry_minutes,
            retry=self.retry,
            ai_disclosure=Disclosure(self.ai_disclosure, self.ai_disclosure_required),
            recording_enabled=self.recording_enabled,
            recording_disclosure=Disclosure(self.recording_disclosure, self.recording_disclosure_required),
            sources=("env",),
        )

    def describe(self) -> str:
        """One line for the startup log."""
        disclosures = ", ".join(
            name for name, required in (("AI", self.ai_disclosure_required), ("recording", self.recording_disclosure_required)) if required
        )
        return (
            f"disclosures required: {disclosures or 'none'}; recording {'on' if self.recording_enabled else 'off'}; "
            f"jurisdictions {', '.join(sorted(self.jurisdictions)) if self.jurisdictions else 'none configured'}"
            f"{f' (default {self.default_jurisdiction})' if self.default_jurisdiction else ''}; "
            f"DNC list enforced; audit {'every decision' if self.audit_allowed else 'refusals only'}"
        )


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. Message is intended for the user."""


@dataclass(frozen=True)
class ReliabilityConfig:
    """The limits that keep a campaign safe to point at real numbers. Phase 9.

    Grouped like `TelephonyConfig` and `CalendarConfig`: one concern, one
    object. Validated at startup, because every one of these has a working
    default and a wrong value is a typo rather than a missing account — and
    because the calling window in particular must never silently fall back to
    "any hour".

    **The defaults are the safe end of every trade.** Calling hours enforced,
    one call at a time, a ten-minute ceiling on a call. A campaign that wants
    more throughput says so explicitly; nothing here becomes permissive by
    being left alone.

    Attributes:
        calling_hours / calling_days / calling_timezone: When a prospect may be
            phoned, in their timezone where their record gives one and this one
            otherwise. Same syntax as the calendar's business hours.
        enforce_calling_hours: False allows any hour, for calling your own phone
            in the evening while testing. Reported at startup either way.
        max_concurrent_calls: Live calls allowed at once, across all campaigns.
        pacing_secs: Minimum seconds between placements. 0 is unpaced.
        max_call_secs: Hard ceiling on one call, enforced by the bot.
        llm_stall_secs: How long an inference may run without finishing before
            the call is treated as stalled.
        max_service_failures: Consecutive failures from one pipeline stage
            before the call is ended.
        carrier_timeout_secs: Ceiling on one HTTP request to the carrier.
        recovery_min_age_secs: How stale a live attempt must be before recovery
            will touch it — a healthy call in progress must never be reconciled.
        health_timeout_secs: Ceiling on one health check.
    """

    calling_hours: str
    calling_days: str
    calling_timezone: str
    enforce_calling_hours: bool
    max_concurrent_calls: int
    pacing_secs: float
    max_call_secs: float
    llm_stall_secs: float
    max_service_failures: int
    carrier_timeout_secs: float
    recovery_min_age_secs: float
    health_timeout_secs: float

    @classmethod
    def from_env(cls, problems: list[str], *, default_timezone: str = "UTC") -> ReliabilityConfig:
        """Read the reliability settings, collecting every problem into `problems`.

        Args:
            problems: The shared startup problem list.
            default_timezone: What `CALLING_TIMEZONE` falls back to — the
                calendar's zone, so a bot configured for Karachi meetings calls
                during Karachi hours without being told twice.
        """
        num = _Numbers(problems)
        hours = _clean(os.getenv("CALLING_HOURS")) or "09:00-18:00"
        days = _clean(os.getenv("CALLING_DAYS")) or "mon-fri"
        timezone = _clean(os.getenv("CALLING_TIMEZONE")) or default_timezone

        try:
            BusinessHours.parse(hours, days)
        except ValueError as exc:
            problems.append(f"CALLING_HOURS / CALLING_DAYS: {exc}")
            hours, days = "09:00-18:00", "mon-fri"
        if timezone.upper() != "UTC":
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                problems.append(
                    f"CALLING_TIMEZONE is {timezone!r}, which is not a known timezone. Use an "
                    f"IANA name such as Asia/Karachi."
                )
                timezone = "UTC"

        return cls(
            calling_hours=hours,
            calling_days=days,
            calling_timezone=timezone,
            enforce_calling_hours=_flag("ENFORCE_CALLING_HOURS", True, problems),
            max_concurrent_calls=num.integer("MAX_CONCURRENT_CALLS", 1, 1, 100),
            pacing_secs=num.number("CALL_PACING_SECS", 0.0, 0.0, 3600.0),
            # Ten minutes: a cold call that has run that long has either become
            # a real conversation somebody should be handling, or has gone
            # wrong. Either way it is worth a look.
            max_call_secs=num.number("MAX_CALL_SECS", 600.0, 0.0, 7200.0),
            # Generous, because a throttled Groq turn legitimately takes tens of
            # seconds on the free tier (see the handoff) and cutting those off
            # would break working calls.
            llm_stall_secs=num.number("LLM_STALL_SECS", 60.0, 0.0, 600.0),
            max_service_failures=num.integer("MAX_SERVICE_FAILURES", 3, 0, 50),
            carrier_timeout_secs=num.number("CARRIER_TIMEOUT_SECS", 20.0, 1.0, 300.0),
            recovery_min_age_secs=num.number("RECOVERY_MIN_AGE_SECS", 120.0, 0.0, 86400.0),
            health_timeout_secs=num.number("HEALTH_TIMEOUT_SECS", 6.0, 1.0, 120.0),
        )

    def describe(self) -> str:
        """One line for the startup log."""
        hours = (
            f"{self.calling_hours} {self.calling_days} {self.calling_timezone}"
            if self.enforce_calling_hours
            else "any hour (ENFORCE_CALLING_HOURS=false)"
        )
        pacing = f", one call every {self.pacing_secs:g}s" if self.pacing_secs else ""
        return (
            f"calling {hours}, max {self.max_concurrent_calls} concurrent{pacing}, "
            f"call ceiling {self.max_call_secs:g}s"
        )


@dataclass(frozen=True)
class WorkerConfig:
    """How the scheduler paces itself. Phase 13.

    None of these decide *whether* a call may be placed — that is
    `ReliabilityConfig` and the campaign's own rules, applied unchanged. These
    decide how often the worker looks: at the calls it is following, at the
    queue, at the callbacks that have fallen due.

    Attributes:
        poll_secs: How often a call in progress is checked with the carrier,
            and the shortest the loop sleeps when it has work.
        idle_secs: The longest the loop sleeps. It wakes at least this often
            to notice a campaign that was activated, a callback that fell due,
            or a pause.
        recovery_interval_secs: How often the recovery pass runs while the
            worker is up, for attempts it is not itself following.
        drain_secs: After a stop request, how long to keep following the calls
            already in progress before giving up on them.
        report_secs: How often the `worker.metrics` line is logged.
        auto_complete: Mark a campaign `COMPLETED` when it has nothing left it
            could ever dial.
        webhook_poll_secs: Phase 14. Once the carrier has pushed at least one
            event for a call, how often the worker still asks the carrier
            about it — the safety net under the webhooks, not the source of
            truth. Until the first event arrives a call is polled every
            `poll_secs` as before, so a receiver that is down costs nothing
            but the old request rate. 0 keeps polling at `poll_secs`
            regardless of webhooks.
    """

    poll_secs: float
    idle_secs: float
    recovery_interval_secs: float
    drain_secs: float
    report_secs: float
    auto_complete: bool
    webhook_poll_secs: float = 30.0
    # Phase 21: the fleet. How often a worker says it is alive, how long a
    # silence means it is dead, how often a live worker looks for the calls
    # and reservations a dead one left, a name for this process, and whether
    # a placement that failed for the system's reasons is tried again.
    heartbeat_secs: float = 10.0
    stale_secs: float = 60.0
    adopt_secs: float = 30.0
    worker_id: str | None = None
    retry_transient_failures: bool = True
    transient_retry_minutes: float | None = None
    # Phase 25: the unified application runs the scheduler inside itself
    # (`WORKER_EMBEDDED`, default on), and gives calls in progress this long
    # to end when the application is asked to stop before handing them over
    # (`WORKER_SHUTDOWN_SECS`; the CLI's `drain_secs` is for a process whose
    # only job is the calls).
    embedded: bool = True
    shutdown_secs: float = 30.0

    @classmethod
    def from_env(cls, problems: list[str]) -> WorkerConfig:
        """Read the worker settings, collecting every problem into `problems`."""
        num = _Numbers(problems)
        heartbeat = num.number("WORKER_HEARTBEAT_SECS", 10.0, 1.0, 300.0)
        stale = num.number("WORKER_STALE_SECS", 60.0, 5.0, 3600.0)
        if stale <= heartbeat * 2:
            problems.append(
                f"WORKER_STALE_SECS ({stale:g}) must be more than twice WORKER_HEARTBEAT_SECS "
                f"({heartbeat:g}), or one slow beat would make a live worker look dead."
            )
        return cls(
            heartbeat_secs=heartbeat,
            stale_secs=stale,
            adopt_secs=num.number("WORKER_ADOPT_SECS", 30.0, 1.0, 3600.0),
            worker_id=_clean(os.getenv("WORKER_ID")),
            retry_transient_failures=_flag("WORKER_RETRY_TRANSIENT_FAILURES", True, problems),
            transient_retry_minutes=num.optional_float("WORKER_TRANSIENT_RETRY_MINUTES", 0.0, 10080.0),
            poll_secs=num.number("WORKER_POLL_SECS", 2.0, 0.5, 60.0),
            idle_secs=num.number("WORKER_IDLE_SECS", 30.0, 1.0, 3600.0),
            recovery_interval_secs=num.number("WORKER_RECOVERY_INTERVAL_SECS", 300.0, 0.0, 86400.0),
            # Fifteen minutes: a call has a ten-minute ceiling by default, plus
            # ringing, plus a margin. Enough for the call in progress to end
            # on its own; short enough that a stop is not an open-ended wait.
            drain_secs=num.number("WORKER_DRAIN_SECS", 900.0, 0.0, 7200.0),
            report_secs=num.number("WORKER_REPORT_SECS", 60.0, 5.0, 3600.0),
            auto_complete=_flag("WORKER_AUTO_COMPLETE", True, problems),
            webhook_poll_secs=num.number("WORKER_WEBHOOK_POLL_SECS", 30.0, 0.0, 600.0),
            embedded=_flag("WORKER_EMBEDDED", True, problems),
            shutdown_secs=num.number("WORKER_SHUTDOWN_SECS", 30.0, 0.0, 600.0),
        )

    def describe(self) -> str:
        """One line for the startup log."""
        webhooks = (
            f"poll every {self.webhook_poll_secs:g}s once events are arriving"
            if self.webhook_poll_secs > 0
            else "webhooks do not slow the poll"
        )
        return (
            f"poll every {self.poll_secs:g}s, idle up to {self.idle_secs:g}s, "
            f"recovery every {self.recovery_interval_secs:g}s, drain {self.drain_secs:g}s, "
            f"auto-complete {'on' if self.auto_complete else 'off'}, {webhooks}; "
            f"heartbeat every {self.heartbeat_secs:g}s, stale after {self.stale_secs:g}s, "
            f"adopt abandoned work every {self.adopt_secs:g}s, "
            f"transient failures {'retried' if self.retry_transient_failures else 'not retried'}; "
            f"{'embedded in the application' if self.embedded else 'not embedded (campaign.py run)'}, "
            f"shutdown waits {self.shutdown_secs:g}s for calls in progress"
        )


@dataclass(frozen=True)
class CostConfig:
    """What this account pays per unit. Phase 11. Every rate optional.

    Unset is the normal state and produces *no* cost estimate rather than a
    guessed one: a price is a commercial arrangement this code cannot know, and
    the project's default stack is three free tiers where the honest per-call
    cost is "nothing until the tier runs out". Usage is measured either way.

    See `reliability/usage.py` for what is counted.
    """

    llm_input_per_mtok: float | None
    llm_output_per_mtok: float | None
    tts_per_mchar: float | None
    stt_per_minute: float | None
    telephony_per_minute: float | None

    @classmethod
    def from_env(cls, problems: list[str]) -> CostConfig:
        """Read the rates, collecting every problem into `problems`."""
        num = _Numbers(problems)
        return cls(
            llm_input_per_mtok=num.optional_float("COST_LLM_INPUT_PER_MTOK", 0.0, 10_000.0),
            llm_output_per_mtok=num.optional_float("COST_LLM_OUTPUT_PER_MTOK", 0.0, 10_000.0),
            tts_per_mchar=num.optional_float("COST_TTS_PER_MCHAR", 0.0, 10_000.0),
            stt_per_minute=num.optional_float("COST_STT_PER_MINUTE", 0.0, 1_000.0),
            telephony_per_minute=num.optional_float("COST_TELEPHONY_PER_MINUTE", 0.0, 1_000.0),
        )


@dataclass(frozen=True)
class MonitoringConfig:
    """The operational surface every process serves. Phase 22.

    `/healthz`, `/readyz` and `/metrics` are mounted on every server this
    project has — the bot's runner, the dashboard, the automation API, the
    webhook receiver — and served from a small server of their own for the
    scheduler, which otherwise has none. Nothing here changes what a call
    does; it decides where the numbers can be read and who may read them.

    Attributes:
        enabled: Serve the routes at all. Off leaves every process exactly
            as it was before the phase, counting nothing anyone can scrape.
        host / port: Where `campaign.py run` serves its own three routes.
            Port 0 turns that server off (the worker still records its
            numbers; they are simply not served). A second worker on the
            same machine finds the port taken and carries on without one.
        token: A bearer that `/metrics` and `/metrics.json` demand when set.
            The numbers name campaign ids, error rates and costs — not
            people — so the default is open on a loopback deployment and a
            token is expected once a port is reachable from a network.
        refresh_secs: How often a server process re-reads queue depth,
            worker health and throughput from PostgreSQL into its gauges.
        throughput_window_secs: The window `aiva_throughput_calls` counts
            over. An hour: the figure a campaign owner asks for.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7895
    token: str | None = None
    refresh_secs: float = 30.0
    throughput_window_secs: float = 3600.0

    @classmethod
    def from_env(cls, problems: list[str]) -> MonitoringConfig:
        """Read the monitoring settings, collecting every problem into `problems`."""
        num = _Numbers(problems)
        token = _clean(os.getenv("MONITORING_TOKEN"))
        if token is not None and len(token) < 16:
            problems.append("MONITORING_TOKEN must be at least 16 characters (or unset, to leave /metrics open).")
        return cls(
            enabled=_flag("MONITORING_ENABLED", True, problems),
            host=_clean(os.getenv("MONITORING_HOST")) or "127.0.0.1",
            port=num.integer("MONITORING_PORT", 7895, 0, 65535),
            token=token,
            refresh_secs=num.number("MONITORING_REFRESH_SECS", 30.0, 5.0, 3600.0),
            throughput_window_secs=num.number("MONITORING_THROUGHPUT_WINDOW_SECS", 3600.0, 60.0, 604800.0),
        )

    @property
    def serves_worker(self) -> bool:
        """Whether the scheduler should open its own port."""
        return self.enabled and self.port > 0

    def describe(self) -> str:
        """One line for the startup log."""
        if not self.enabled:
            return "monitoring off (MONITORING_ENABLED=false)"
        return (
            f"/healthz /readyz /metrics on every server"
            f"{f', scheduler on {self.host}:{self.port}' if self.port else ', scheduler serves none (MONITORING_PORT=0)'}; "
            f"metrics {'behind MONITORING_TOKEN' if self.token else 'open (set MONITORING_TOKEN once a port is reachable from a network)'}; "
            f"fleet gauges refreshed every {self.refresh_secs:g}s over a {self.throughput_window_secs / 60:g} min throughput window"
        )


@dataclass(frozen=True)
class TelephonyConfig:
    """How to place a phone call, when somebody asks for one.

    Unlike the rest of this module, nothing here is required at startup — see
    the module docstring. `is_configured` answers "could this bot dial out right
    now", which is what the startup banner reports, and `require_outbound`
    turns a "no" into a message naming what is missing.
    """

    provider: str
    from_number: str | None
    # The public address of *this* bot's runner, which is where the carrier
    # sends the call's audio. In development that is a tunnel (`ngrok http
    # 7860`); there is no default because it changes every time the tunnel
    # restarts.
    public_url: str | None
    # The runner's telephony websocket route. Pipecat's dev runner serves `/ws`;
    # this exists for a deployment that mounts it somewhere else.
    stream_path: str
    answer_timeout_secs: int
    # How long a dropped *phone* call is held open before the session is torn
    # down. Zero by default, and deliberately not the same knob as
    # `DISCONNECT_GRACE_SECS`, because the two drops are different events: a
    # browser's WebRTC connection blips and recovers into the same session,
    # while a phone call that drops is over — the person redials and gets a new
    # call. Waiting only keeps a dead pipeline and a live STT websocket around.
    disconnect_grace_secs: float
    # Phase 7: where `transfer_to_human` sends a live call. Unset means the
    # agent cannot transfer and is told so, offering a callback instead. Not
    # required and not validated as reachable — only as shaped like a number.
    transfer_number: str | None = None
    # Present only for credentials that are actually set, so a value read out of
    # here is a `str` and not a `str | None`. `require_credentials` is what
    # guarantees the ones you need are there.
    credentials: dict[str, str] = field(default_factory=dict)
    # {logical name: environment variable} for this provider, for error messages.
    credential_env: dict[str, str] = field(default_factory=dict)
    # Phase 12: carrier-side answering-machine detection. One of
    # `MACHINE_DETECTION_MODES`; `off` unless asked for, because it costs money
    # per call and, in `sync` mode, adds silence for every person who answers.
    machine_detection: str = "off"
    # How the bot reads the carrier's verdict: it polls the call resource for
    # `answered_by` every `amd_poll_secs` for `amd_window_secs` after the audio
    # connects. A few cheap reads instead of a webhook endpoint.
    amd_poll_secs: float = 2.0
    amd_window_secs: float = 20.0
    # Phase 14: carrier status webhooks. On by default, because they cost
    # nothing when the receiver is not reachable — the carrier's callback
    # fails quietly and polling carries on exactly as before — and because a
    # call whose outcome is pushed is written back within a second instead of
    # within `WORKER_POLL_SECS`. `webhook_url()` is what is sent to the
    # carrier and what the receiver verifies signatures against; it is None
    # whenever events could not be received or checked.
    webhooks_enabled: bool = True
    webhook_receiver: str = "bot"
    webhook_path: str = "/webhooks/telephony"
    # The carrier's signing secret when it is not a dialling credential, and
    # the environment variable it came from, for messages. Twilio has neither:
    # it signs with the auth token.
    webhook_signing_key: str | None = None
    webhook_signing_env: str | None = None
    # Phase 16: how long a transfer rings the colleague before the caller is
    # told nobody is available. The outcome of the ring reaches the webhook
    # receiver when webhooks are on, and is recorded on `call_transfers`.
    transfer_timeout_secs: int = 30

    @classmethod
    def from_env(cls, problems: list[str] | None = None) -> TelephonyConfig:
        """Read the telephony settings from the environment.

        Args:
            problems: A list to append problems to, when this is being built as
                part of the whole `Config` and every problem should be reported
                in one pass. Omit it — as `call.py` does — to have this raise on
                its own, which is what makes placing a call possible without a
                Deepgram, Groq, Cartesia or PostgreSQL configuration.

        Raises:
            ConfigError: A setting is invalid, and no `problems` list was given
                to collect it into.
        """
        collected: list[str] = [] if problems is None else problems

        # The provider *name* is validated here even though its credentials are
        # not: a typo is a mistake in `.env` rather than a missing account, and
        # left alone it would only surface as a confusing "no provider
        # implemented" the first time somebody tried to dial.
        provider = os.getenv("TELEPHONY_PROVIDER", "twilio").strip().lower()
        if provider not in SUPPORTED_TELEPHONY:
            collected.append(
                f"TELEPHONY_PROVIDER is {provider!r}, which cannot place calls. "
                f"Use one of: {', '.join(SUPPORTED_TELEPHONY)}."
            )

        credential_env = dict(_TELEPHONY_CREDENTIALS.get(provider, {}))
        credentials = {
            name: value
            for name, env_name in credential_env.items()
            if (value := _clean(os.getenv(env_name))) is not None
        }

        transfer_number = _clean(os.getenv("TELEPHONY_TRANSFER_NUMBER"))
        if transfer_number is not None and not _E164.match(transfer_number):
            collected.append(
                f"TELEPHONY_TRANSFER_NUMBER is {transfer_number!r}; it must be an E.164 number "
                f"such as +923001234567 (country code, no spaces, no leading zero)."
            )
            transfer_number = None

        # Phase 14: the route has to be a path. A bare word or a full URL here
        # would be sent to the carrier as-is and fail as a callback nobody
        # could trace back to one line of `.env`.
        webhook_path = _clean(os.getenv("TELEPHONY_WEBHOOK_PATH")) or "/webhooks/telephony"
        if not webhook_path.startswith("/") or "//" in webhook_path or " " in webhook_path:
            collected.append(
                f"TELEPHONY_WEBHOOK_PATH is {webhook_path!r}; it must be a route such as "
                f"/webhooks/telephony (starting with a slash, no scheme or host)."
            )
            webhook_path = "/webhooks/telephony"
        signing_env = _TELEPHONY_WEBHOOK_SIGNING.get(provider)

        num = _Numbers(collected)
        config = cls(
            provider=provider,
            from_number=_clean(os.getenv("TELEPHONY_FROM_NUMBER")),
            public_url=_clean(os.getenv("TELEPHONY_PUBLIC_URL")),
            stream_path=_clean(os.getenv("TELEPHONY_STREAM_PATH")) or "/ws",
            webhooks_enabled=_flag("TELEPHONY_WEBHOOKS", True, collected),
            webhook_receiver=_choice(
                "TELEPHONY_WEBHOOK_RECEIVER", "bot", WEBHOOK_RECEIVERS, collected
            ),
            webhook_path=webhook_path,
            webhook_signing_key=_clean(os.getenv(signing_env)) if signing_env else None,
            webhook_signing_env=signing_env,
            transfer_timeout_secs=num.integer("TELEPHONY_TRANSFER_TIMEOUT_SECS", 30, 5, 120),
            # Twilio's own default is 60s, long enough for most numbers to reach
            # voicemail. 30s is the working default here: on a cold call a
            # no-answer is a result to record and move on from.
            answer_timeout_secs=num.integer("TELEPHONY_ANSWER_TIMEOUT_SECS", 30, 5, 600),
            disconnect_grace_secs=num.number("TELEPHONY_DISCONNECT_GRACE_SECS", 0.0, 0.0, 120.0),
            transfer_number=transfer_number,
            credentials=credentials,
            credential_env=credential_env,
            machine_detection=_choice(
                "TELEPHONY_MACHINE_DETECTION", "off", MACHINE_DETECTION_MODES, collected
            ),
            amd_poll_secs=num.number("TELEPHONY_AMD_POLL_SECS", 2.0, 0.5, 30.0),
            amd_window_secs=num.number("TELEPHONY_AMD_WINDOW_SECS", 20.0, 1.0, 120.0),
        )

        if problems is None and collected:
            raise ConfigError(
                "Telephony configuration problems found:\n"
                + "\n".join(f"  - {p}" for p in collected)
            )
        return config

    @property
    def is_configured(self) -> bool:
        """Whether an outbound call could be placed with what is in the environment."""
        return not self._missing()

    @property
    def has_credentials(self) -> bool:
        """Whether the carrier's own settings are present.

        Weaker than `is_configured`, which also wants a caller ID and a public
        URL. This is the question "can a provider be built at all", which is
        what *receiving* a call needs: the serializer has to authenticate to
        hang the call up, but nothing about answering a call needs to know which
        number we would have dialled from.
        """
        return not self._missing_credentials()

    def require_credentials(self) -> None:
        """Raise unless the carrier's credentials are present.

        Separate from `require_outbound` because a provider can be built — to
        hang up a call, to look one up — without knowing a caller ID or having
        a public URL to stream audio to.
        """
        missing = self._missing_credentials()
        if missing:
            raise ConfigError(_telephony_problem(self.provider, missing))

    def require_outbound(self) -> None:
        """Raise unless everything needed to place an outbound call is present."""
        missing = self._missing()
        if missing:
            raise ConfigError(_telephony_problem(self.provider, missing))

    @property
    def can_transfer(self) -> bool:
        """Whether a live call could be handed to a person: credentials plus a destination."""
        return self.has_credentials and bool(self.transfer_number)

    @property
    def wants_machine_detection(self) -> bool:
        """Whether calls are placed with the carrier's answering-machine detection on."""
        return self.machine_detection != "off"

    # --- Webhooks (Phase 14) -------------------------------------------------

    @property
    def can_verify_webhooks(self) -> bool:
        """Whether a delivery from the carrier could be checked with what is set.

        Twilio signs with the auth token, so credentials are enough. A carrier
        with a separate signing key needs it set; without it every delivery
        would be refused, so none is asked for — see `webhook_url`.
        """
        if not self.has_credentials:
            return False
        if self.webhook_signing_env is None:
            return True
        return bool(self.webhook_signing_key)

    def webhook_url(self) -> str | None:
        """Where the carrier should POST call events, or None to ask for none.

        None whenever an event could not be received or could not be trusted:
        webhooks switched off, no public URL to reach this machine, or a
        carrier whose signing key is not set. The same value is what the
        receiver verifies signatures against, so the two cannot drift.
        """
        if not self.webhooks_enabled or not self.public_url or not self.can_verify_webhooks:
            return None
        from .telephony.base import TelephonyError, webhook_url

        try:
            return webhook_url(self.public_url, self.webhook_path, stream_path=self.stream_path)
        except TelephonyError:
            return None

    def describe_webhooks(self) -> str:
        """One clause for a startup line: where events go, or why they do not."""
        if not self.webhooks_enabled:
            return "webhooks off (TELEPHONY_WEBHOOKS=false)"
        if not self.has_credentials:
            return "webhooks off (no carrier credentials)"
        if self.webhook_signing_env and not self.webhook_signing_key:
            return (
                f"webhooks off — {self.webhook_signing_env} is not set, so {self.provider} "
                f"events could not be verified; call status is polled"
            )
        if not self.public_url:
            return "webhooks off (no TELEPHONY_PUBLIC_URL)"
        url = self.webhook_url()
        if url is None:
            return "webhooks off (TELEPHONY_PUBLIC_URL is unusable)"
        served = "served by the bot" if self.webhook_receiver == "bot" else "served by webhooks.py"
        return f"webhooks at {url} ({served})"

    def describe(self) -> str:
        """One line for the startup log. Never includes a credential."""
        if not self.is_configured:
            return f"{self.provider} (not configured — browser calls only)"
        transfer = (
            f", transfers to {self.transfer_number} ({self.transfer_timeout_secs}s ring)"
            if self.transfer_number
            else ", no transfer number"
        )
        amd = f", carrier AMD {self.machine_detection}" if self.wants_machine_detection else ""
        return (
            f"{self.provider} from {self.from_number} via {self.public_url}{self.stream_path}"
            f"{transfer}{amd}, {self.describe_webhooks()}"
        )

    def _missing_credentials(self) -> list[str]:
        """The carrier's own settings that are not set."""
        return [env for name, env in self.credential_env.items() if name not in self.credentials]

    def _missing(self) -> list[str]:
        """Environment variables needed for an outbound call that are not set."""
        missing = self._missing_credentials()
        if not self.from_number:
            missing.append("TELEPHONY_FROM_NUMBER")
        if not self.public_url:
            missing.append("TELEPHONY_PUBLIC_URL")
        return missing


@dataclass(frozen=True)
class VoiceQualityConfig:
    """How a call is judged while it happens, and what is done about a machine. Phase 12.

    Grouped like the other feature objects. Validated at startup, because every
    setting has a working default and a wrong value is a typo. None of it
    changes the audio pipeline: these settings decide what is *measured* and
    *logged*, whether a spurious interruption is recovered from, and how an
    answering machine is handled.

    Attributes:
        turn_response_timeout_secs: A caller turn with words in it that has
            produced no bot audio after this long is logged as a failed turn.
            0 disables the check. Generous by default because a throttled
            free-tier LLM turn legitimately takes tens of seconds.
        noise_resume: Whether the agent is asked to continue when it was cut
            off by an interruption that carried no words.
        noise_resume_max: How many times per call it may be asked.
        call_report_dir: Where each phone call's report is written, as JSON
            named by the carrier's call id. `off` disables it.
        voicemail_detection: One of `VOICEMAIL_DETECTION_MODES`.
        voicemail_action: One of `VOICEMAIL_ACTIONS`.
        voicemail_message: What to say on a machine. Empty means a short
            default composed from the agent's and company's names.
        voicemail_max_greeting_secs: A first caller turn longer than this,
            without a pause, is a recording. 0 disables the length rule.
        voicemail_window_secs: Only this much of the start of the call is judged.
        voicemail_message_delay_secs: How long after the greeting's turn ends
            to wait before speaking the message. The beep is not detected, so
            this is what clears it.
        voicemail_phrases: What only a recording says, `|`-separated in the
            environment. Empty means the built-in list.
    """

    turn_response_timeout_secs: float
    noise_resume: bool
    noise_resume_max: int
    call_report_dir: str | None
    voicemail_detection: str
    voicemail_action: str
    voicemail_message: str
    voicemail_max_greeting_secs: float
    voicemail_window_secs: float
    voicemail_message_delay_secs: float
    voicemail_phrases: tuple[str, ...]

    @classmethod
    def from_env(cls, problems: list[str]) -> VoiceQualityConfig:
        """Read the settings, collecting every problem into `problems`."""
        num = _Numbers(problems)
        report_dir = _clean(os.getenv("CALL_REPORT_DIR"))
        if report_dir is None:
            report_dir = "call-reports"
        elif report_dir.lower() in ("off", "none", "false", "0"):
            report_dir = None
        return cls(
            turn_response_timeout_secs=num.number("TURN_RESPONSE_TIMEOUT_SECS", 10.0, 0.0, 300.0),
            noise_resume=_flag("NOISE_RESUME", True, problems),
            noise_resume_max=num.integer("NOISE_RESUME_MAX", 3, 0, 50),
            call_report_dir=report_dir,
            voicemail_detection=_choice(
                "VOICEMAIL_DETECTION", "heuristic", VOICEMAIL_DETECTION_MODES, problems
            ),
            voicemail_action=_choice("VOICEMAIL_ACTION", "hangup", VOICEMAIL_ACTIONS, problems),
            voicemail_message=_clean(os.getenv("VOICEMAIL_MESSAGE")) or "",
            voicemail_max_greeting_secs=num.number("VOICEMAIL_MAX_GREETING_SECS", 8.0, 0.0, 120.0),
            voicemail_window_secs=num.number("VOICEMAIL_WINDOW_SECS", 30.0, 1.0, 600.0),
            voicemail_message_delay_secs=num.number("VOICEMAIL_MESSAGE_DELAY_SECS", 2.0, 0.0, 30.0),
            voicemail_phrases=_list("VOICEMAIL_PHRASES"),
        )

    @property
    def voicemail_enabled(self) -> bool:
        """Whether the bot judges a phone call for an answering machine."""
        return self.voicemail_detection != "off"

    def describe(self) -> str:
        """One line for the startup log."""
        failed = (
            f"failed turn after {self.turn_response_timeout_secs:g}s"
            if self.turn_response_timeout_secs
            else "failed-turn check off"
        )
        resume = f"noise resume up to {self.noise_resume_max}x" if self.noise_resume else "no noise resume"
        report = f"reports in {self.call_report_dir}" if self.call_report_dir else "no call reports"
        if self.voicemail_enabled:
            voicemail = (
                f"voicemail {self.voicemail_detection} -> {self.voicemail_action}, "
                f"greeting limit {self.voicemail_max_greeting_secs:g}s"
            )
        else:
            voicemail = "voicemail detection off"
        return f"{failed}, {resume}, {report}, {voicemail}"


def _telephony_problem(provider: str, missing: list[str]) -> str:
    """The message for a telephony setting that is needed and not there."""
    where = _WHERE_TO_GET_TELEPHONY.get(provider)
    lines = [
        f"Cannot place a phone call: TELEPHONY_PROVIDER is {provider!r} and these are not set:",
        *(f"  - {name}" for name in missing),
    ]
    if where:
        lines.append(f"\nCredentials: {where}")
    if "TELEPHONY_PUBLIC_URL" in missing:
        lines.append(
            "\nTELEPHONY_PUBLIC_URL is the address the carrier streams the call's audio to, so it "
            "has to reach this machine from the public internet. In development that is a tunnel:"
            "\n    ngrok http 7860"
            "\nthen set TELEPHONY_PUBLIC_URL to the https URL it prints."
        )
    lines.append("\nSee the TELEPHONY section of server/.env.example.")
    return "\n".join(lines)


@dataclass(frozen=True)
class SalesConfig:
    """Who the agent says it is, what it is selling, and who it is calling.

    Phase 6. Grouped into its own object for the same reason `TelephonyConfig`
    is: these settings belong to one feature, they are meaningless without each
    other, and a flat `Config` with fourteen more fields would bury the
    pipeline's own tuning knobs among them.

    **None of it is required and none of it is guessed.** An unset company name
    is not a startup failure and is not filled in with a placeholder; it becomes
    an instruction telling the agent that it has not been told which company it
    is calling for and must not invent one. Every empty value here works the
    same way, because the failure this whole phase is arranged to prevent is an
    agent that sounds confident about something nobody told it.

    Attributes:
        enabled: `SALES_MODE`. False runs the Phase 3 assistant — the general
            knowledge-base agent, no tools, no state machine — which is the A/B
            baseline and what the two Phase 2 eval scenarios were written
            against.
        value_points: The only claims the agent may make about the product. Set
            from `SALES_VALUE_POINTS` as a `|`-separated list, or per campaign
            from the campaign's `configuration` JSON, which overlays these.
        prospect: A prospect described in the environment, used only when a call
            carries no prospect id — a browser session or an eval run. It is how
            personalisation is tested without a database, and it is deliberately
            *not* a default applied to real calls: a campaign call that cannot
            find its prospect must stay anonymous rather than be told it is
            speaking to whoever `.env` last described.
    """

    enabled: bool
    agent_name: str
    company_name: str
    offer: str
    value_points: tuple[str, ...]
    qualification_criteria: tuple[str, ...]
    meeting_ask: str
    notes: tuple[str, ...]
    # Phase 28: the always-available campaign context — what the company is
    # and does, and the services it offers — from `SALES_COMPANY_DESCRIPTION`
    # and `SALES_SERVICES`. Optional: without them the agent has the company
    # name and the offer, as before.
    company_description: str = ""
    services: tuple[str, ...] = ()
    prospect: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, problems: list[str]) -> SalesConfig:
        """Read the sales settings from the environment. Nothing here can fail.

        The only thing that could be invalid is `SALES_MODE`, and `_flag`
        already collects an unrecognisable boolean into `problems`. Everything
        else is free text whose absence is handled in the prompt.
        """
        prospect = {
            name: value
            for name, value in (
                ("first_name", _clean(os.getenv("DEV_PROSPECT_FIRST_NAME")) or ""),
                ("last_name", _clean(os.getenv("DEV_PROSPECT_LAST_NAME")) or ""),
                ("company", _clean(os.getenv("DEV_PROSPECT_COMPANY")) or ""),
                ("job_title", _clean(os.getenv("DEV_PROSPECT_JOB_TITLE")) or ""),
                ("industry", _clean(os.getenv("DEV_PROSPECT_INDUSTRY")) or ""),
                ("location", _clean(os.getenv("DEV_PROSPECT_LOCATION")) or ""),
                ("email", _clean(os.getenv("DEV_PROSPECT_EMAIL")) or ""),
                ("notes", _clean(os.getenv("DEV_PROSPECT_NOTES")) or ""),
            )
            if value
        }
        return cls(
            enabled=_flag("SALES_MODE", True, problems),
            agent_name=_clean(os.getenv("SALES_AGENT_NAME")) or "",
            company_name=_clean(os.getenv("SALES_COMPANY_NAME")) or "",
            offer=_clean(os.getenv("SALES_OFFER")) or "",
            value_points=_list("SALES_VALUE_POINTS"),
            qualification_criteria=_list("SALES_QUALIFICATION_CRITERIA"),
            meeting_ask=_clean(os.getenv("SALES_MEETING_ASK")) or "",
            notes=_list("SALES_CAMPAIGN_NOTES"),
            company_description=_clean(os.getenv("SALES_COMPANY_DESCRIPTION")) or "",
            services=_list("SALES_SERVICES"),
            prospect=prospect,
        )

    @property
    def gaps(self) -> list[str]:
        """Settings whose absence changes what the agent is able to say.

        Reported as a warning at startup rather than as an error, because a bot
        with none of them still runs and still holds a conversation — it just
        introduces itself by name only and refuses every question of detail. The
        warning exists so that is a choice somebody made rather than a surprise
        heard on a live call.
        """
        missing = []
        if not self.agent_name:
            missing.append("SALES_AGENT_NAME")
        if not self.company_name:
            missing.append("SALES_COMPANY_NAME")
        if not self.offer:
            missing.append("SALES_OFFER")
        if not self.value_points:
            missing.append("SALES_VALUE_POINTS")
        if not self.meeting_ask:
            missing.append("SALES_MEETING_ASK")
        return missing

    def describe(self) -> str:
        """One line for the startup log."""
        if not self.enabled:
            return "off (SALES_MODE=false — general assistant)"
        who = self.agent_name or "unnamed agent"
        for_whom = f" for {self.company_name}" if self.company_name else " (no company set)"
        gaps = f", {len(self.gaps)} setting(s) unset" if self.gaps else ""
        return f"{who}{for_whom}, {len(self.value_points)} claim(s){gaps}"


@dataclass(frozen=True)
class Config:
    """Resolved, validated configuration for one bot process."""

    # --- Providers ---------------------------------------------------------
    stt_provider: str
    llm_provider: str
    tts_provider: str

    stt_api_key: str | None
    llm_api_key: str | None
    tts_api_key: str | None

    stt_model: str
    llm_model: str
    cartesia_voice_id: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    deepgram_tts_voice: str
    # Phase 30. A second TTS provider on standby for the call: when the primary
    # fails before it has produced audio for what it was asked to say (HTTP 402
    # quota, a connection or timeout failure, a provider error), the rest of the
    # call is spoken by this one — once per call, never back. Off by default;
    # `services.make_tts` wraps the two services in `tts_fallback.TTSFallbackSwitcher`.
    tts_fallback_enabled: bool
    tts_fallback_provider: str
    tts_fallback_api_key: str | None
    # The most tokens the LLM may generate per turn. A spoken reply is one to
    # three sentences and a tool call is a few dozen tokens, so a few hundred is
    # generous — and sending the cap matters beyond tidiness: Groq's free tier
    # enforces an output-tokens-per-minute limit against the request's *expected*
    # output, which it estimates generously when no cap is given. Observed on
    # 2026-09-04: with twelve tools advertised and no cap, every request was
    # refused as "too large" before the model said a word.
    llm_max_output_tokens: int
    # How a reasoning model's thinking is kept out of the answer channel; one of
    # `REASONING_FORMATS`. Applied only where the provider takes the parameter
    # (`services.reasoning_extra`); `src/spoken_text.py` is the second line.
    llm_reasoning_format: str

    # --- Turn taking -------------------------------------------------------
    # Flux end-of-turn tuning. None means "use Deepgram's own default" rather
    # than a number we invented.
    flux_eot_threshold: float | None
    flux_eot_timeout_ms: int | None
    flux_eager_eot_threshold: float | None
    # Minimum words before a barge-in counts as an interruption. Only applies on
    # the non-Flux path, where we own the turn-start strategy; 0 disables the
    # guard so any speech interrupts.
    interrupt_min_words: int
    # Flux path only: what cuts the agent off mid-sentence. See `BARGE_IN_TRIGGERS`.
    barge_in_trigger: str
    # What to do about the agent hearing itself through the caller's speakers.
    # "off" (the default) keeps full barge-in and assumes something upstream is
    # cancelling the echo — headphones, the browser's AEC, or a phone network.
    # See `ECHO_SUPPRESSION` in `.env.example` for the whole trade.
    echo_suppression: str

    # --- VAD ---------------------------------------------------------------
    vad_confidence: float
    vad_start_secs: float
    vad_stop_secs: float
    vad_min_volume: float

    # --- Latency -----------------------------------------------------------
    # Stream tokens straight to the TTS websocket instead of buffering whole
    # sentences. Saves most of a sentence of latency on the first response, at
    # some cost in prosody, so it is off by default and easy to A/B.
    tts_stream_tokens: bool
    log_metrics: bool

    # --- Robustness --------------------------------------------------------
    user_idle_timeout_secs: float
    max_idle_prompts: int
    disconnect_grace_secs: float
    session_idle_timeout_secs: float
    # How long a browser peer connection may look dead before we believe it went
    # away. Covers the caller who closes the laptop or loses wifi rather than
    # pressing disconnect — nothing in WebRTC reports that, so it has to be
    # noticed by watching. See `resilience.PeerWatchdog`. 0 disables.
    peer_timeout_secs: float

    # --- Knowledge base ----------------------------------------------------
    # False runs the Phase 2 agent: no retrieval, no database, answers from the
    # model's own knowledge. Useful as the A/B baseline for what retrieval is
    # worth, and for running the pipeline with no PostgreSQL at all.
    kb_enabled: bool
    kb_database_url: str | None
    embedding_model: str
    # Passages handed to the LLM per turn. More gives the model a better chance
    # of holding the answer somewhere; it also lengthens the prompt, which costs
    # time to first token on every single turn, and buries the best match among
    # weaker ones.
    kb_top_k: int
    # Similarity floor. Its job is narrower than it looks, and it is worth being
    # precise about, because the obvious reading of it is wrong.
    #
    # Measured on this project's sample knowledge base (19 chunks, bge-small):
    # questions the document answers score 0.578-0.809; questions on the same
    # topic that it does *not* answer ("can I pay in yen?", against a document
    # full of prices) score 0.519-0.642; unrelated remarks and greetings score
    # 0.456-0.566. The middle band overlaps the first. No threshold separates
    # them, because a passage about pricing genuinely is the nearest thing to a
    # question about pricing — it just does not contain the answer.
    #
    # So this is not the setting that decides whether the agent admits it does
    # not know; the LLM decides that, from the passages and the instruction that
    # travels with them (`prompts.KNOWLEDGE_BLOCK_FOOTER`). This floor only
    # strips obvious noise, which is why the default is set *below* the worst
    # answerable question rather than between the two bands: a passage wrongly
    # let through costs a few tokens, and one wrongly dropped costs an answer
    # the knowledge base actually had.
    kb_min_score: float
    # When to search at all. "always" is Phase 3: every turn, unconditionally.
    # "auto" (the default from Phase 6) skips turns that cannot be an
    # information request — "yeah", "we do it by hand", "next quarter" — because
    # a sales conversation is mostly not questions and searching each of those
    # attaches confident, on-topic, irrelevant passages to a turn nobody asked
    # anything in. See `retrieval.looks_like_information_request`.
    kb_retrieval_mode: str
    # A caller message shorter than this is embedded together with their
    # previous one, so that "how much is it?" still retrieves the right thing.
    kb_short_query_words: int
    # Ingest-time chunking. Changing either means re-ingesting: the stored
    # chunks were cut with the old values.
    kb_chunk_words: int
    kb_chunk_overlap_words: int
    # Phase 37: the turn path never waits on the database for knowledge. The
    # bot holds the knowledge base in memory (`knowledge_index.py`), re-reads
    # it every `kb_refresh_secs` when it changed, and a retrieval that still
    # takes longer than `kb_timeout_secs` is abandoned for that turn.
    kb_timeout_secs: float
    kb_refresh_secs: float
    kb_index_max_chunks: int

    # --- Campaigns (Phase 5) ------------------------------------------------
    # Where prospects, campaigns and call history live. Defaults to
    # `KB_DATABASE_URL` so an existing setup needs no new configuration and both
    # sets of tables share one database — while leaving them separable later,
    # which matters because the knowledge base can be switched off entirely and
    # the campaign tables cannot.
    database_url: str | None
    # Country assumed for phone numbers written without one. Unset means such
    # numbers are refused rather than guessed at; see `campaigns/phone.py`.
    default_phone_region: str | None
    # Dials per prospect per campaign before that membership is exhausted.
    campaign_max_attempts: int
    # Wait before a no-answer or busy is offered again.
    campaign_retry_minutes: float

    # --- The sales conversation (Phase 6) -----------------------------------
    # Who the agent is, what it is selling, and — for development only — who it
    # should pretend to be calling when no campaign supplied a prospect.
    sales: SalesConfig

    # --- Telephony ---------------------------------------------------------
    # Optional, and validated at the point of use rather than at startup: see
    # the module docstring.
    telephony: TelephonyConfig

    # --- Actions (Phase 7) ---------------------------------------------------
    # Where the agent books meetings, and how far ahead a callback may be.
    calendar: CalendarConfig
    callback_max_days_ahead: int

    # --- Reliability (Phase 9) -----------------------------------------------
    # Calling hours, concurrency, pacing, and the limits the bot enforces on
    # itself. Every default is the safe end of its trade; see `ReliabilityConfig`.
    reliability: ReliabilityConfig

    # --- Cost (Phase 11) -----------------------------------------------------
    # What a unit costs on this account. All optional; unset means usage is
    # measured and cost is not estimated.
    cost: CostConfig

    # --- Voice quality (Phase 12) --------------------------------------------
    # Failed-turn detection, recovery from a spurious interruption, the per-call
    # report, and answering-machine handling. See `VoiceQualityConfig`.
    voice_quality: VoiceQualityConfig
    # Phase 13: the scheduler's own pacing. `uv run campaign.py run`.
    worker: WorkerConfig
    # Phase 15: which CRM finished calls are filed with. `campaign.py crm-sync`.
    crm: CrmConfig
    # Phase 17: the automation API and the outbound events. `automation.py`
    # and `campaign.py events`; never the bot.
    automation: AutomationConfig
    # Phase 18: who may reach the dashboard and the API, and how the servers
    # are hardened. `dashboard.py`, `automation.py`, `webhooks.py`; never the bot.
    security: SecurityConfig
    # Phase 19: the compliance controls — disclosures, per-outcome retries,
    # jurisdiction rules. The bot reads only the disclosures, as words on the
    # brief; the dialer and the API apply the rest through `ComplianceGate`.
    compliance: ComplianceConfig
    # Phase 22: where each process serves /healthz, /readyz and /metrics,
    # and who may read them. Every process; nothing on the call path
    # changes for it.
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    @property
    def compliance_policy(self) -> CompliancePolicy:
        """The environment's effective policy: the base every campaign and jurisdiction overlays."""
        return self.compliance.policy(
            self.reliability,
            max_attempts=self.campaign_max_attempts,
            retry_minutes=self.campaign_retry_minutes,
        )

    def policy_resolver(self) -> PolicyResolver:
        """The resolver the dialer, the API and the briefing share."""
        return PolicyResolver(
            self.compliance_policy,
            jurisdictions=self.compliance.jurisdictions,
            default_region=self.default_phone_region,
        )

    @classmethod
    def from_env(cls) -> Config:
        """Build config from the environment, or raise ConfigError listing every problem.

        Collects all problems before raising so you fix them in one pass instead
        of rerunning once per missing key.
        """
        problems: list[str] = []

        stt = os.getenv("STT_PROVIDER", "deepgram_flux").strip().lower()
        llm = os.getenv("LLM_PROVIDER", "cerebras").strip().lower()
        tts = os.getenv("TTS_PROVIDER", "cartesia").strip().lower()

        for kind, chosen in (("stt", stt), ("llm", llm), ("tts", tts)):
            if chosen not in SUPPORTED[kind]:
                problems.append(
                    f"{kind.upper()}_PROVIDER is {chosen!r}, which is not supported. "
                    f"Use one of: {', '.join(SUPPORTED[kind])}."
                )

        # Only demand keys for providers that are selected and valid.
        keys: dict[str, str | None] = {}
        for kind, chosen, table in (
            ("stt", stt, _STT_KEY_ENV),
            ("llm", llm, _LLM_KEY_ENV),
            ("tts", tts, _TTS_KEY_ENV),
        ):
            env_name = table.get(chosen)
            if env_name is None:
                keys[kind] = None  # Provider is local or unknown; nothing to check.
                continue
            value = _clean(os.getenv(env_name))
            if value is None:
                source = _WHERE_TO_GET.get(env_name, "")
                suffix = f"  Get one at {source}" if source else ""
                message = f"{env_name} is not set.{suffix}"
                if message not in problems:
                    problems.append(message)
            keys[kind] = value

        # Phase 30: the TTS fallback. Its key is only demanded when it is on,
        # and it has to be a different provider — the same one would fail for
        # the same reason.
        tts_fallback_enabled = _flag("TTS_FALLBACK_ENABLED", False, problems)
        tts_fallback_provider = _choice(
            "TTS_FALLBACK_PROVIDER", "elevenlabs", SUPPORTED["tts"], problems
        )
        tts_fallback_api_key: str | None = None
        if tts_fallback_enabled:
            if tts_fallback_provider == tts:
                problems.append(
                    f"TTS_FALLBACK_PROVIDER is {tts_fallback_provider!r}, the same as TTS_PROVIDER. "
                    "A fallback has to be a different provider; pick another or set "
                    "TTS_FALLBACK_ENABLED=false."
                )
            env_name = _TTS_KEY_ENV.get(tts_fallback_provider)
            if env_name is not None:
                tts_fallback_api_key = _clean(os.getenv(env_name))
                if tts_fallback_api_key is None:
                    source = _WHERE_TO_GET.get(env_name, "")
                    suffix = f"  Get one at {source}" if source else ""
                    message = f"{env_name} is not set (needed by TTS_FALLBACK_PROVIDER).{suffix}"
                    if message not in problems:
                        problems.append(message)

        kb_enabled = _flag("KB_ENABLED", True, problems)
        kb_database_url = _clean(os.getenv("KB_DATABASE_URL"))
        if kb_enabled and kb_database_url is None:
            problems.append(
                "KB_DATABASE_URL is not set, and KB_ENABLED is on.  Point it at a PostgreSQL "
                "database with the pgvector extension, e.g. "
                "postgresql://postgres:PASSWORD@localhost:5432/voice_agent_kb  "
                "(set KB_ENABLED=false to run without a knowledge base)."
            )

        num = _Numbers(problems)

        config = cls(
            stt_provider=stt,
            llm_provider=llm,
            tts_provider=tts,
            stt_api_key=keys["stt"],
            llm_api_key=keys["llm"],
            tts_api_key=keys["tts"],
            stt_model=os.getenv("STT_MODEL", "").strip() or DEFAULT_STT_MODELS.get(stt, ""),
            llm_model=os.getenv(f"{llm.upper()}_MODEL", "").strip() or DEFAULT_MODELS.get(llm, ""),
            cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID", "").strip()
            or DEFAULT_CARTESIA_VOICE_ID,
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
            elevenlabs_model=os.getenv("ELEVENLABS_MODEL", "").strip()
            or DEFAULT_ELEVENLABS_MODEL,
            deepgram_tts_voice=os.getenv("DEEPGRAM_TTS_VOICE", "").strip()
            or DEFAULT_DEEPGRAM_TTS_VOICE,
            tts_fallback_enabled=tts_fallback_enabled,
            tts_fallback_provider=tts_fallback_provider,
            tts_fallback_api_key=tts_fallback_api_key,
            llm_max_output_tokens=num.integer("LLM_MAX_OUTPUT_TOKENS", 400, 64, 8192),
            llm_reasoning_format=_choice("LLM_REASONING_FORMAT", "hidden", REASONING_FORMATS, problems),
            flux_eot_threshold=num.optional_float("FLUX_EOT_THRESHOLD", 0.0, 1.0),
            flux_eot_timeout_ms=num.optional_int("FLUX_EOT_TIMEOUT_MS", 500, 30_000),
            flux_eager_eot_threshold=num.optional_float("FLUX_EAGER_EOT_THRESHOLD", 0.0, 1.0),
            interrupt_min_words=num.integer("INTERRUPT_MIN_WORDS", 0, 0, 10),
            barge_in_trigger=_choice("BARGE_IN_TRIGGER", "vad", BARGE_IN_TRIGGERS, problems),
            echo_suppression=_choice("ECHO_SUPPRESSION", "off", ECHO_SUPPRESSION_MODES, problems),
            vad_confidence=num.number("VAD_CONFIDENCE", 0.7, 0.0, 1.0),
            vad_start_secs=num.number("VAD_START_SECS", 0.2, 0.0, 2.0),
            vad_stop_secs=num.number("VAD_STOP_SECS", 0.2, 0.0, 5.0),
            vad_min_volume=num.number("VAD_MIN_VOLUME", 0.6, 0.0, 1.0),
            tts_stream_tokens=_flag("TTS_STREAM_TOKENS", False, problems),
            log_metrics=_flag("LOG_METRICS", True, problems),
            user_idle_timeout_secs=num.number("USER_IDLE_TIMEOUT_SECS", 12.0, 0.0, 300.0),
            max_idle_prompts=num.integer("MAX_IDLE_PROMPTS", 2, 0, 10),
            disconnect_grace_secs=num.number("DISCONNECT_GRACE_SECS", 5.0, 0.0, 120.0),
            session_idle_timeout_secs=num.number("SESSION_IDLE_TIMEOUT_SECS", 300.0, 0.0, 3600.0),
            peer_timeout_secs=num.number("PEER_TIMEOUT_SECS", 5.0, 0.0, 120.0),
            kb_enabled=kb_enabled,
            kb_database_url=kb_database_url,
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip() or DEFAULT_EMBEDDING_MODEL,
            kb_top_k=num.integer("KB_TOP_K", 4, 1, 20),
            kb_min_score=num.number("KB_MIN_SCORE", 0.5, 0.0, 1.0),
            kb_retrieval_mode=_choice(
                "KB_RETRIEVAL_MODE", "auto", KB_RETRIEVAL_MODES, problems
            ),
            kb_short_query_words=num.integer("KB_SHORT_QUERY_WORDS", 6, 0, 30),
            kb_chunk_words=num.integer("KB_CHUNK_WORDS", 60, 20, 2000),
            kb_chunk_overlap_words=num.integer("KB_CHUNK_OVERLAP_WORDS", 15, 0, 500),
            kb_timeout_secs=num.number("KB_TIMEOUT_SECS", 2.0, 0.0, 30.0),
            kb_refresh_secs=num.number("KB_REFRESH_SECS", 60.0, 0.0, 3600.0),
            kb_index_max_chunks=num.integer("KB_INDEX_MAX_CHUNKS", 5000, 0, 200000),
            database_url=_clean(os.getenv("DATABASE_URL")) or kb_database_url,
            default_phone_region=_region("DEFAULT_PHONE_REGION", problems),
            campaign_max_attempts=num.integer("CAMPAIGN_MAX_ATTEMPTS", 3, 1, 20),
            campaign_retry_minutes=num.number("CAMPAIGN_RETRY_MINUTES", 60.0, 0.0, 10080.0),
            sales=SalesConfig.from_env(problems),
            # Collected into the same problem list, so a bad telephony setting
            # is reported alongside every other one rather than on a rerun.
            telephony=TelephonyConfig.from_env(problems),
            calendar=(calendar := CalendarConfig.from_env(problems)),
            callback_max_days_ahead=num.integer("CALLBACK_MAX_DAYS_AHEAD", 60, 1, 365),
            # The calling window defaults to the calendar's zone: a bot booking
            # meetings in Karachi should be calling during Karachi hours, and
            # making somebody say so twice is how the two end up disagreeing.
            reliability=ReliabilityConfig.from_env(problems, default_timezone=calendar.timezone),
            cost=CostConfig.from_env(problems),
            voice_quality=VoiceQualityConfig.from_env(problems),
            worker=WorkerConfig.from_env(problems),
            crm=CrmConfig.from_env(problems),
            automation=AutomationConfig.from_env(problems),
            security=SecurityConfig.from_env(problems),
            compliance=ComplianceConfig.from_env(problems),
            monitoring=MonitoringConfig.from_env(problems),
        )

        if config.kb_chunk_overlap_words >= config.kb_chunk_words:
            problems.append(
                f"KB_CHUNK_OVERLAP_WORDS ({config.kb_chunk_overlap_words}) must be smaller than "
                f"KB_CHUNK_WORDS ({config.kb_chunk_words}); an overlap at least as large as the "
                f"chunk would never advance through the document."
            )

        if problems:
            raise ConfigError(
                "Configuration problems found:\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\n\nSet these in server/.env (copy server/.env.example if you have not yet)."
            )

        return config

    def describe(self) -> str:
        """One-line summary of the active stack, for the startup log."""
        return (
            f"STT={self.stt_provider}:{self.stt_model} | "
            f"LLM={self.llm_provider}:{self.llm_model} | "
            f"TTS={self.tts_provider}"
            + (f"->{self.tts_fallback_provider}" if self.tts_fallback_enabled else "")
            + " | "
            f"KB={self.describe_knowledge()} | "
            f"SALES={self.sales.describe()} | "
            f"PHONE={self.telephony.describe()} | "
            f"CAL={self.calendar.describe()} | "
            f"CRM={self.crm.describe()}"
        )

    @property
    def shares_database(self) -> bool:
        """Whether the knowledge base and the campaign tables are one database.

        Phase 11. When they are — which is the default, since `DATABASE_URL`
        falls back to `KB_DATABASE_URL` — a bot session can open **one**
        connection pool instead of two. At six connections per call against
        PostgreSQL's default hundred, that difference is the ceiling on how many
        calls can run at once; see `bot.py`.
        """
        return bool(
            self.kb_enabled
            and self.kb_database_url
            and self.database_url
            and self.kb_database_url == self.database_url
        )

    def describe_safety(self) -> str:
        """One line for the startup log: the limits a campaign runs under. Phase 9.

        Its own line rather than a fragment of `describe()` because these are
        the settings that decide whether a stranger's phone rings at four in the
        morning, and somebody reading a log to check that should not have to
        find them inside a hundred-character stack summary.
        """
        return f"Safety: {self.reliability.describe()}"

    def describe_voice_quality(self) -> str:
        """One line for the startup log: what is measured on a call, and what a machine gets. Phase 12."""
        return f"Voice quality: {self.voice_quality.describe()}"

    def describe_knowledge(self) -> str:
        """One-line summary of the knowledge base setup, for the startup log."""
        if not self.kb_enabled:
            return "off"
        return (
            f"pgvector({self.embedding_model.rsplit('/', 1)[-1]}, "
            f"top{self.kb_top_k}@{self.kb_min_score:g}, {self.kb_retrieval_mode})"
        )

    def describe_echo(self) -> str:
        """One-line summary of the echo setting, for the startup log.

        Worth its own line rather than a flag buried in the stack summary: when
        it is on, barge-in is off, and somebody debugging "why can't I interrupt
        it" should find the reason in the first few lines of the log.
        """
        if self.echo_suppression == "always":
            return (
                "Echo suppression: on — the caller is ignored while the agent speaks (no barge-in)"
            )
        if self.echo_suppression == "greeting":
            return "Echo suppression: greeting only — no barge-in during the opening turn"
        return "Echo suppression: off — full barge-in (use headphones, or set ECHO_SUPPRESSION)"

    def describe_turn_taking(self) -> str:
        """One-line summary of how turns are detected, for the startup log."""
        if self.stt_provider == "deepgram_flux":
            eot = self.flux_eot_threshold if self.flux_eot_threshold is not None else "default"
            eager = (
                self.flux_eager_eot_threshold
                if self.flux_eager_eot_threshold is not None
                else "off"
            )
            return (
                f"Turn-taking: Flux semantic end-of-turn (eot={eot}, eager={eager}), "
                f"barge-in on {self.barge_in_trigger}"
            )
        guard = (
            f", barge-in needs {self.interrupt_min_words} words" if self.interrupt_min_words else ""
        )
        return f"Turn-taking: Silero VAD + Smart Turn v3{guard}"


class _Numbers:
    """Parses numeric env vars, collecting problems instead of raising one at a time."""

    def __init__(self, problems: list[str]) -> None:
        self._problems = problems

    def number(self, name: str, default: float, low: float, high: float) -> float:
        """Read a float, falling back to `default` when unset."""
        value = self.optional_float(name, low, high)
        return default if value is None else value

    def integer(self, name: str, default: int, low: int, high: int) -> int:
        """Read an int, falling back to `default` when unset."""
        value = self.optional_int(name, low, high)
        return default if value is None else value

    def optional_float(self, name: str, low: float, high: float) -> float | None:
        """Read a float, or None if unset. Unparseable or out of range is a config problem."""
        raw = _clean(os.getenv(name))
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            self._problems.append(f"{name} is {raw!r}, which is not a number.")
            return None
        if not low <= value <= high:
            self._problems.append(f"{name} is {value}; it must be between {low} and {high}.")
            return None
        return value

    def optional_int(self, name: str, low: int, high: int) -> int | None:
        """Read an int, or None if unset. Unparseable or out of range is a config problem."""
        raw = _clean(os.getenv(name))
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            self._problems.append(f"{name} is {raw!r}, which is not a whole number.")
            return None
        if not low <= value <= high:
            self._problems.append(f"{name} is {value}; it must be between {low} and {high}.")
            return None
        return value


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(name: str, default: bool, problems: list[str]) -> bool:
    """Read a boolean env var. Anything unrecognised is a config problem, not a silent False."""
    raw = _clean(os.getenv(name))
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    problems.append(f"{name} is {raw!r}; use one of: {', '.join(sorted(_TRUE | _FALSE))}.")
    return default


def _region(name: str, problems: list[str]) -> str | None:
    """Read a two-letter country code, or None.

    Validated for shape rather than against a list of real countries: a typo
    like `PKK` is worth catching here, and whether `ZZ` is a country is
    libphonenumber's question, which it answers by refusing to normalise
    anything — visibly, in the import report.
    """
    raw = _clean(os.getenv(name))
    if raw is None:
        return None
    code = raw.upper()
    if len(code) != 2 or not code.isalpha():
        problems.append(f"{name} is {raw!r}; use a two-letter country code such as PK, US or GB.")
        return None
    return code


def _choice(name: str, default: str, allowed: tuple[str, ...], problems: list[str]) -> str:
    """Read an env var restricted to a fixed set of values.

    An unrecognised value is a config problem rather than a silent fall back to
    the default, because the two are indistinguishable at runtime: a typo in
    `ECHO_SUPPRESSION` would otherwise look exactly like leaving it off, which
    is the failure it was set to fix.
    """
    raw = _clean(os.getenv(name))
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in allowed:
        return lowered
    problems.append(f"{name} is {raw!r}; use one of: {', '.join(allowed)}.")
    return default


def _url(name: str, problems: list[str]) -> str | None:
    """Read an http(s) URL, or None. Anything else is a config problem. Phase 17."""
    raw = _clean(os.getenv(name))
    if raw is None:
        return None
    lowered = raw.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")) or len(raw) < 12:
        problems.append(f"{name} is {raw!r}; it must be a full http(s):// URL.")
        return None
    return raw


def _moment(name: str, problems: list[str]) -> datetime | None:
    """Read an ISO 8601 timestamp, or None. Naive values are taken as UTC. Phase 17."""
    raw = _clean(os.getenv(name))
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        problems.append(f"{name} is {raw!r}; use ISO 8601, e.g. 2026-09-07T00:00:00Z.")
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _list(name: str) -> tuple[str, ...]:
    """Read a `|`-separated env var into a tuple, dropping empty entries.

    A pipe rather than a comma because every value this is used for is a
    sentence — a claim about a product, a qualification criterion — and English
    sentences contain commas.
    """
    raw = _clean(os.getenv(name))
    if raw is None:
        return ()
    return tuple(part.strip() for part in raw.split("|") if part.strip())


def _comma_list(name: str) -> tuple[str, ...]:
    """Read a comma-separated env var (a pipe is accepted too) into a tuple, dropping empty entries.

    For values that are never sentences — networks, origins — where a comma is
    the separator everybody expects and `.env.example` documents.
    """
    raw = _clean(os.getenv(name))
    if raw is None:
        return ()
    return tuple(part.strip() for part in raw.replace("|", ",").split(",") if part.strip())


def _clean(value: str | None) -> str | None:
    """Strip whitespace and treat an empty string as absent.

    A trailing space copied out of a dashboard is a common and very confusing
    cause of authentication failures.
    """
    if value is None:
        return None
    return value.strip() or None
