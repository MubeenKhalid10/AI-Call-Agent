"""The compliance policy a call is placed under, and where each rule came from. Phase 19.

A **policy** is the set of operator-configured rules that decide whether and
how a call goes out: the calling window, the attempt ceiling, the retry
delays, and what the agent must say first. It is assembled in three layers,
each overlaying the one before:

    environment (`COMPLIANCE_*`, `CALLING_*`, `CAMPAIGN_*`)
      ← campaign   (`campaigns.configuration["compliance"]`)
      ← jurisdiction (`COMPLIANCE_JURISDICTIONS[<region of the number>]`)

The jurisdiction layer is applied *last* so that a campaign's settings can
never loosen a rule the operator configured for a country: a campaign may
narrow its hours below the national ones, and the national ones then apply
on top of whatever it chose. The region comes from the phone number's
country code (libphonenumber) — the one thing a number *does* determine,
unlike a timezone, which this project has always refused to guess.

**Nothing here knows what any law says.** `COMPLIANCE_JURISDICTIONS` is a
JSON object the operator writes; the software applies it. `COMPLIANCE.md`
lists which controls exist and which policies must be configured, and says
in so many words that configuring them is the operator's responsibility.

Pure: no database, no clock of its own, no carrier. Every problem with an
overlay is reported as a sentence rather than raised, so a campaign row with
a bad `compliance` block is refused at the API and CLI where it is written,
and merely logged and ignored where it is read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from ..scheduling.hours import BusinessHours

if False:  # pragma: no cover - typing only; imported lazily below so config.py can import this module
    from ..reliability.guardrails import CallingWindow


def resolve_zone(name: str | None) -> Any:
    """Read an IANA timezone name, or raise `ValueError`. The guardrails' rule, restated here.

    Restated rather than imported so that `config.py`, which imports this
    module, does not pull `src.reliability` in at import time.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    cleaned = (name or "UTC").strip()
    if not cleaned or cleaned.upper() == "UTC":
        from datetime import UTC

        return UTC
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(
            f"{cleaned!r} is not a known timezone. Use an IANA name such as "
            f"Asia/Karachi, Europe/London or America/New_York."
        ) from exc

#: The key under `campaigns.configuration` that holds a campaign's overrides.
CONFIG_KEY = "compliance"

#: The keys an overlay may set, in the order `describe()` lists them.
OVERLAY_KEYS = (
    "jurisdiction",
    "calling_hours",
    "calling_days",
    "timezone",
    "enforce_calling_hours",
    "max_attempts",
    "retry_minutes",
    "retry_minutes_no_answer",
    "retry_minutes_busy",
    "retry_minutes_voicemail",
    "ai_disclosure",
    "ai_disclosure_required",
    "recording_enabled",
    "recording_disclosure",
    "recording_disclosure_required",
)

MAX_ATTEMPTS_CEILING = 20
MAX_RETRY_MINUTES = 7 * 24 * 60
MAX_DISCLOSURE_CHARS = 400

DEFAULT_AI_DISCLOSURE = "I'm an AI assistant"
DEFAULT_RECORDING_DISCLOSURE = "this call may be recorded"


@dataclass(frozen=True)
class Disclosure:
    """A sentence the agent must say in its opening, when it is required."""

    text: str = ""
    required: bool = False

    @property
    def sentence(self) -> str | None:
        """The sentence to require, or None when not required or empty."""
        cleaned = self.text.strip()
        return cleaned if self.required and cleaned else None


@dataclass(frozen=True)
class RetryDelays:
    """How long to wait before trying again, per unreached outcome. Minutes.

    `None` means "use the policy's general `retry_minutes`". A separate
    figure per outcome exists because a voicemail and a busy tone say
    different things about when to try again.
    """

    no_answer: float | None = None
    busy: float | None = None
    voicemail: float | None = None

    def for_status(self, status: str, default: float) -> float:
        """The delay for an attempt status value (`NO_ANSWER`, `BUSY`, `VOICEMAIL`)."""
        chosen = {
            "NO_ANSWER": self.no_answer,
            "BUSY": self.busy,
            "VOICEMAIL": self.voicemail,
        }.get(str(status).upper())
        return float(default if chosen is None else chosen)


@dataclass(frozen=True)
class CompliancePolicy:
    """The effective rules for one call, or one campaign, or the whole deployment.

    Attributes:
        jurisdiction: A label the operator chose (`US`, `GB`, `PK`, or anything
            else) naming which rule set applied. Informational and audited.
        calling_hours / calling_days / timezone: The window, in the syntax
            `BusinessHours` reads (`09:00-18:00`, `mon-fri`). The timezone is
            the fallback when the prospect's own record gives none.
        enforce_calling_hours: False allows any hour (a test call to your own
            phone at night). Never the production setting.
        max_attempts: Dials per membership before it is exhausted.
        retry_minutes: The general wait after an unreached call.
        retry: Per-outcome waits, overriding `retry_minutes` where set.
        ai_disclosure: What the agent says about being an AI, and whether it
            must say it in its first breath rather than only when asked.
        recording_enabled: Whether the operator records calls. This software
            does not record audio itself; the flag exists so a disclosure can
            be required when the operator's carrier or proxy does.
        recording_disclosure: What the agent says about recording, and whether
            it must.
        sources: Which layers built this policy, for the audit trail.
    """

    jurisdiction: str | None = None
    calling_hours: str = "09:00-18:00"
    calling_days: str = "mon-fri"
    timezone: str = "UTC"
    enforce_calling_hours: bool = True
    max_attempts: int = 3
    retry_minutes: float = 60.0
    retry: RetryDelays = field(default_factory=RetryDelays)
    ai_disclosure: Disclosure = field(default_factory=lambda: Disclosure(DEFAULT_AI_DISCLOSURE, False))
    recording_enabled: bool = False
    recording_disclosure: Disclosure = field(default_factory=lambda: Disclosure(DEFAULT_RECORDING_DISCLOSURE, False))
    sources: tuple[str, ...] = ("defaults",)

    #: Do-not-call is honoured under every policy. Not a setting, on purpose:
    #: there is no configuration in which phoning somebody who asked not to
    #: be phoned is correct.
    honor_dnc: bool = field(default=True, init=False, repr=False, compare=False)

    def window(self, *, clock: Callable[[], datetime] | None = None) -> CallingWindow:
        """The calling window this policy defines."""
        from ..reliability.guardrails import CallingWindow

        return CallingWindow.parse(
            self.calling_hours,
            self.calling_days,
            self.timezone,
            enabled=self.enforce_calling_hours,
            clock=clock,
        )

    def retry_minutes_for(self, status: Any) -> float:
        """The wait before an attempt with this status is retried."""
        return self.retry.for_status(getattr(status, "value", status), self.retry_minutes)

    def disclosures(self) -> list[str]:
        """The sentences the agent's opening must contain, in order."""
        found = []
        for disclosure in (self.ai_disclosure, self.recording_disclosure):
            sentence = disclosure.sentence
            if sentence:
                found.append(sentence)
        return found

    def overlay(
        self, overrides: Mapping[str, Any] | None, *, source: str, problems: list[str] | None = None
    ) -> CompliancePolicy:
        """This policy with `overrides` applied. Every problem is reported, not raised.

        Args:
            overrides: The keys in `OVERLAY_KEYS`, in the shapes `describe`
                shows. A key that is absent or None leaves the value alone.
            source: Where the overrides came from, for `sources`.
            problems: Where to append what could not be applied. None raises
                `ValueError` on the first problem instead.
        """
        if not overrides:
            return self
        if not isinstance(overrides, Mapping):
            return _problem(problems, f"{source}: the compliance settings must be an object, not {type(overrides).__name__}", self)
        collected: list[str] = [] if problems is None else problems
        values: dict[str, Any] = {}
        retry = self.retry
        ai = self.ai_disclosure
        rec = self.recording_disclosure

        for key, raw in overrides.items():
            name = str(key)
            if name not in OVERLAY_KEYS:
                collected.append(f"{source}: unknown compliance setting {name!r}; use one of {', '.join(OVERLAY_KEYS)}")
                continue
            if raw is None:
                continue
            try:
                if name == "jurisdiction":
                    values[name] = _text(raw, name, 40) or None
                elif name in ("calling_hours", "calling_days"):
                    values[name] = _text(raw, name, 200)
                elif name == "timezone":
                    text = _text(raw, name, 80)
                    resolve_zone(text)
                    values[name] = text
                elif name in ("enforce_calling_hours", "ai_disclosure_required", "recording_enabled", "recording_disclosure_required"):
                    flag = _flag(raw, name)
                    if name == "enforce_calling_hours":
                        values[name] = flag
                    elif name == "ai_disclosure_required":
                        ai = replace(ai, required=flag)
                    elif name == "recording_enabled":
                        values[name] = flag
                    else:
                        rec = replace(rec, required=flag)
                elif name == "max_attempts":
                    values[name] = _integer(raw, name, 1, MAX_ATTEMPTS_CEILING)
                elif name == "retry_minutes":
                    values[name] = _number(raw, name, 0.0, MAX_RETRY_MINUTES)
                elif name == "retry_minutes_no_answer":
                    retry = replace(retry, no_answer=_number(raw, name, 0.0, MAX_RETRY_MINUTES))
                elif name == "retry_minutes_busy":
                    retry = replace(retry, busy=_number(raw, name, 0.0, MAX_RETRY_MINUTES))
                elif name == "retry_minutes_voicemail":
                    retry = replace(retry, voicemail=_number(raw, name, 0.0, MAX_RETRY_MINUTES))
                elif name == "ai_disclosure":
                    ai = replace(ai, text=_text(raw, name, MAX_DISCLOSURE_CHARS))
                elif name == "recording_disclosure":
                    rec = replace(rec, text=_text(raw, name, MAX_DISCLOSURE_CHARS))
            except ValueError as exc:
                collected.append(f"{source}: {exc}")

        hours = values.get("calling_hours", self.calling_hours)
        days = values.get("calling_days", self.calling_days)
        if "calling_hours" in values or "calling_days" in values:
            try:
                BusinessHours.parse(hours, days)
            except ValueError as exc:
                collected.append(f"{source}: calling window {hours!r} / {days!r}: {exc}")
                values.pop("calling_hours", None)
                values.pop("calling_days", None)

        if problems is None and collected:
            raise ValueError("; ".join(collected))
        return replace(
            self,
            **values,
            retry=retry,
            ai_disclosure=ai,
            recording_disclosure=rec,
            sources=(*self.sources, source),
        )

    def to_dict(self) -> dict[str, Any]:
        """The effective policy as JSON, for the API and the audit trail."""
        return {
            "jurisdiction": self.jurisdiction,
            "calling_hours": self.calling_hours,
            "calling_days": self.calling_days,
            "timezone": self.timezone,
            "enforce_calling_hours": self.enforce_calling_hours,
            "max_attempts": self.max_attempts,
            "retry_minutes": self.retry_minutes,
            "retry_minutes_no_answer": self.retry.no_answer,
            "retry_minutes_busy": self.retry.busy,
            "retry_minutes_voicemail": self.retry.voicemail,
            "ai_disclosure": self.ai_disclosure.text,
            "ai_disclosure_required": self.ai_disclosure.required,
            "recording_enabled": self.recording_enabled,
            "recording_disclosure": self.recording_disclosure.text,
            "recording_disclosure_required": self.recording_disclosure.required,
            "honor_dnc": True,
            "disclosures": self.disclosures(),
            "sources": list(self.sources),
        }

    def describe(self) -> str:
        """One line for a log or a CLI. Never long."""
        window = (
            f"{self.calling_hours} {self.calling_days} {self.timezone}"
            if self.enforce_calling_hours
            else "any hour (calling hours not enforced)"
        )
        retries = f"retry {self.retry_minutes:g}m"
        extras = [
            f"{name} {value:g}m"
            for name, value in (("no-answer", self.retry.no_answer), ("busy", self.retry.busy), ("voicemail", self.retry.voicemail))
            if value is not None
        ]
        if extras:
            retries += f" ({', '.join(extras)})"
        disclosures = ", ".join(
            name
            for name, disclosure in (("AI", self.ai_disclosure), ("recording", self.recording_disclosure))
            if disclosure.sentence
        )
        return (
            f"{self.jurisdiction or 'default'}: window {window}; max {self.max_attempts} attempt(s); {retries}; "
            f"disclosures {disclosures or 'none required'}; DNC honoured; from {' < '.join(self.sources)}"
        )


class PolicyResolver:
    """Builds the effective policy for a campaign or a call from the three layers."""

    def __init__(
        self,
        base: CompliancePolicy,
        *,
        jurisdictions: Mapping[str, Mapping[str, Any]] | None = None,
        default_region: str | None = None,
    ) -> None:
        """Create the resolver.

        Args:
            base: The environment's policy.
            jurisdictions: Region code (`US`, `GB`, …) → overrides, from
                `COMPLIANCE_JURISDICTIONS`. Keys are compared upper-cased.
            default_region: The region assumed for a number that carries no
                country code — the same `DEFAULT_PHONE_REGION` the importer
                uses. Only for numbers that could not be normalised, which are
                not dialled anyway.
        """
        self._base = base
        self._jurisdictions = {str(k).upper(): dict(v) for k, v in (jurisdictions or {}).items()}
        self._default_region = (default_region or "").upper() or None

    @property
    def base(self) -> CompliancePolicy:
        return self._base

    @property
    def jurisdictions(self) -> dict[str, dict[str, Any]]:
        return dict(self._jurisdictions)

    @staticmethod
    def region_of(phone_normalized: str | None) -> str | None:
        """The country code a normalised number belongs to, or None."""
        if not phone_normalized:
            return None
        try:
            import phonenumbers

            return phonenumbers.region_code_for_number(phonenumbers.parse(phone_normalized, None)) or None
        except Exception:  # noqa: BLE001 - not a number is not a crash
            return None

    def for_campaign(
        self, configuration: Mapping[str, Any] | None, *, campaign_id: int | None = None, problems: list[str] | None = None
    ) -> CompliancePolicy:
        """The environment's policy with one campaign's overrides."""
        overrides = configuration.get(CONFIG_KEY) if isinstance(configuration, Mapping) else None
        if not overrides:
            return self._base
        collected: list[str] = []
        policy = self._base.overlay(overrides, source=f"campaign:{campaign_id if campaign_id is not None else '?'}", problems=collected)
        if collected:
            if problems is not None:
                problems.extend(collected)
            else:
                from loguru import logger

                logger.warning(f"COMPLIANCE | campaign {campaign_id} has settings that were ignored: {'; '.join(collected)}")
        return policy

    def for_call(
        self,
        configuration: Mapping[str, Any] | None,
        phone_normalized: str | None,
        *,
        campaign_id: int | None = None,
        region: str | None = None,
    ) -> tuple[CompliancePolicy, str | None]:
        """The policy for one call: campaign overrides, then the number's jurisdiction.

        Returns:
            The policy and the region it was resolved for (None when the
            number gave none and no default region is configured).
        """
        policy = self.for_campaign(configuration, campaign_id=campaign_id)
        found = (region or self.region_of(phone_normalized) or self._default_region or "").upper() or None
        overrides = self._jurisdictions.get(found or "")
        if overrides:
            collected: list[str] = []
            policy = policy.overlay(overrides, source=f"jurisdiction:{found}", problems=collected)
            if collected:
                from loguru import logger

                logger.warning(f"COMPLIANCE | COMPLIANCE_JURISDICTIONS[{found}] has settings that were ignored: {'; '.join(collected)}")
            if policy.jurisdiction is None or policy.jurisdiction == self._base.jurisdiction:
                policy = replace(policy, jurisdiction=found)
        return policy, found


def parse_jurisdictions(raw: str | None, problems: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Read `COMPLIANCE_JURISDICTIONS`: a JSON object of region code → overrides.

    Every entry is validated against the default policy at startup, so a
    typo in a country's hours is a startup problem rather than a call placed
    at the wrong hour.
    """
    collected: list[str] = [] if problems is None else problems
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        collected.append(f"COMPLIANCE_JURISDICTIONS is not valid JSON: {exc}")
        return {}
    if not isinstance(data, dict):
        collected.append("COMPLIANCE_JURISDICTIONS must be a JSON object of region code -> settings.")
        return {}
    found: dict[str, dict[str, Any]] = {}
    for region, overrides in data.items():
        code = str(region).strip().upper()
        if not (2 <= len(code) <= 3) or not code.isalpha():
            collected.append(f"COMPLIANCE_JURISDICTIONS: {region!r} is not a region code (two letters, e.g. US, GB, PK).")
            continue
        if not isinstance(overrides, dict):
            collected.append(f"COMPLIANCE_JURISDICTIONS[{code}] must be an object of settings.")
            continue
        before = len(collected)
        CompliancePolicy().overlay(overrides, source=f"COMPLIANCE_JURISDICTIONS[{code}]", problems=collected)
        if len(collected) == before:
            found[code] = dict(overrides)
    if problems is None and collected:
        raise ValueError("; ".join(collected))
    return found


def _problem(problems: list[str] | None, message: str, fallback: CompliancePolicy) -> CompliancePolicy:
    if problems is None:
        raise ValueError(message)
    problems.append(message)
    return fallback


def _text(raw: Any, name: str, limit: int) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be text")
    cleaned = raw.strip()
    if len(cleaned) > limit:
        raise ValueError(f"{name} may be at most {limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in cleaned):
        raise ValueError(f"{name} contains control characters")
    return cleaned


def _flag(raw: Any, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(raw, str) and raw.strip().lower() in ("false", "no", "off", "0"):
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(raw: Any, name: str, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if isinstance(raw, bool) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _number(raw: Any, name: str, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if isinstance(raw, bool) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return value


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_AI_DISCLOSURE",
    "DEFAULT_RECORDING_DISCLOSURE",
    "MAX_ATTEMPTS_CEILING",
    "OVERLAY_KEYS",
    "CompliancePolicy",
    "Disclosure",
    "PolicyResolver",
    "RetryDelays",
    "parse_jurisdictions",
]
