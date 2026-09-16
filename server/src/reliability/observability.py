"""Structured logs that can be followed across a call, with secrets scrubbed.

Phase 9. Two problems, one module.

**Following one call.** A call touches four processes' worth of concerns — the
dialer that placed it, the bot that held it, the sink that recorded it, the
recovery pass that reconciled it — and until now each logged its own ids in its
own words. `call_context()` binds them once, and every log line inside the
block carries `campaign`, `prospect`, `attempt`, `call` and `provider` whether
or not the code that wrote the line thought to mention them. With
`LOG_FORMAT=json` the same fields come out as machine-readable keys, so a run
can be grepped by attempt id rather than by hope.

The field names are fixed in `CALL_FIELDS` and used everywhere, because a log
you have to guess the field names of is not much better than prose.

**Never logging a secret.** `install_scrubber()` registers a loguru patcher
that replaces every configured credential's *value* with `***` in every record,
including exception text. That is the belt to `describe()`'s braces: this
project is careful to log key tails rather than keys, but the failure mode
being guarded against is not carelessness — it is a vendor SDK putting the
Authorization header in an exception message, which no amount of care at the
call site prevents. The scrubber reads the environment for the variable names
in `SECRET_ENV`, so a key that is not in the environment cannot be scrubbed and
must not be passed around in the first place.

Nothing here changes what the existing log lines say. The prefixes this project
already uses (`CALL |`, `RESULT |`, `TOOL |`) stay exactly as they are; this
adds the context around them and the safety underneath them.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

#: The fields a call-related log line may carry. Fixed so that a query for one
#: of them finds every line that has it, and so a typo shows up as a missing
#: field rather than as a new one.
CALL_FIELDS = (
    # Phase 22: the correlation id born at the dialer and carried through
    # every process — see `monitoring/tracing.py`. First, because it is the
    # one field a reader greps for across the fleet's logs.
    "trace",
    "campaign",
    "prospect",
    "attempt",
    "call",
    "provider",
    # Phase 22: which scheduler process wrote the line, and which HTTP
    # request an API line belongs to.
    "worker",
    "request",
    "event",
    "outcome",
    "latency_ms",
    "retries",
    "error",
)

#: Environment variables whose values must never appear in a log. Names, not
#: values: the scrubber reads them at install time. Anything absent is skipped.
SECRET_ENV = (
    "DEEPGRAM_API_KEY",
    "GROQ_API_KEY",
    "CARTESIA_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CEREBRAS_API_KEY",
    "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "SIGNALWIRE_API_TOKEN",
    "SIGNALWIRE_PROJECT_ID",
    "SIGNALWIRE_SIGNING_KEY",
    "CALCOM_API_KEY",
    "HUBSPOT_ACCESS_TOKEN",
    "DATABASE_URL",
    "KB_DATABASE_URL",
    # Phase 17: the automation API's keys and the outbound webhook's secrets.
    "AUTOMATION_API_KEYS",
    "AUTOMATION_API_KEY",
    "AUTOMATION_WEBHOOK_SECRET",
    "AUTOMATION_WEBHOOK_AUTH_TOKEN",
    # Phase 18: the role-scoped API keys, the session secret, and the user
    # directory (its hashes are not passwords, but a hash in a log is a hash
    # somebody can run a cracker against).
    "AUTOMATION_OPERATOR_API_KEYS",
    "AUTOMATION_VIEWER_API_KEYS",
    "DASHBOARD_SESSION_SECRET",
    "DASHBOARD_USERS",
    # Phase 22: the bearer token in front of /metrics and /readyz.
    "MONITORING_TOKEN",
)

#: Secrets that hold several values separated by commas (a key list that
#: allows rotation). Each part is scrubbed on its own, since a log line would
#: carry one key, never the whole list.
_COMMA_SEPARATED_SECRETS = frozenset(
    {"AUTOMATION_API_KEYS", "AUTOMATION_OPERATOR_API_KEYS", "AUTOMATION_VIEWER_API_KEYS", "DASHBOARD_USERS"}
)

#: Shapes that are secrets wherever they appear, whatever the environment holds.
#: Cheap insurance for a credential this project never sees — one pasted into a
#: campaign note, or returned in a vendor's error body.
_SECRET_PATTERNS = (
    # A named credential takes everything after the separator to the end of the
    # field, not just the next token: `Authorization: Bearer abc…` has the value
    # in the *second* token, and a pattern that stopped at the first would leave
    # the secret in the log and the word "Bearer" redacted.
    re.compile(r"(?i)\b(authorization|x-api-key|api[-_]?key|auth[-_]?token)\s*[:=]\s*[^,;\n]+"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{12,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{12,}"),
    re.compile(r"(?i)://[^\s/:@]+:[^\s/@]+@"),  # user:password@host in any URL
    # Phase 18: a password wherever one is named, a session cookie, a
    # password hash (crackable offline, so not for a log either).
    re.compile(r"(?i)\b(password|passwd|pwd|session[-_]?secret)\s*[:=]\s*[^,;\s]+"),
    re.compile(r"\baiva_session=[^;\s]+"),
    re.compile(r"\bscrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9_\-]+\$[A-Za-z0-9_\-]+"),
)

_REDACTED = "***"

# Values found in the environment at install time, longest first so that a
# secret that contains another one is replaced whole.
_secrets: list[str] = []
_installed = False


def redact(text: str) -> str:
    """Replace anything credential-shaped in `text` with `***`.

    Safe to call on any string, including one that contains no secret. Applied
    automatically to every log record once `install_scrubber` has run; exposed
    directly for the places that build a string for somewhere other than the log
    — an error message shown to a user, a field written to the database.
    """
    if not text:
        return text
    scrubbed = text
    for secret in _secrets:
        if secret in scrubbed:
            scrubbed = scrubbed.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub(_mask, scrubbed)
    return scrubbed


def _mask(match: re.Match[str]) -> str:
    """Keep the name of a credential-shaped match and drop its value."""
    text = match.group(0)
    for separator in (": ", ":", "= ", "=", " "):
        if separator in text:
            head, _, _tail = text.partition(separator)
            return f"{head}{separator}{_REDACTED}"
    return _REDACTED


def load_secrets(extra_secrets: tuple[str, ...] = ()) -> int:
    """Read the values that must never be logged, from the environment.

    Reads `SECRET_ENV` *now*, so it must run after `load_dotenv`. Separated
    from installing the patcher because `configure_logging` installs its own
    (which scrubs *and* renders the call context), and two calls to
    `logger.configure(patcher=...)` do not compose — the second replaces the
    first.

    Returns:
        How many distinct secret values will be scrubbed.
    """
    values = {
        value
        for name in SECRET_ENV
        # A short value is not a credential and would scrub half the log — a
        # DATABASE_URL of "x" or an empty key must not turn every "x" into ***.
        if (value := (os.getenv(name) or "").strip()) and len(value) >= 8
    }
    for name in _COMMA_SEPARATED_SECRETS:
        parts = [part.strip() for part in (os.getenv(name) or "").split(",")]
        values.update(part for part in parts if len(part) >= 8)
    values.update(value for value in extra_secrets if value and len(value) >= 8)
    _secrets[:] = sorted(values, key=len, reverse=True)
    return len(_secrets)


def install_scrubber(extra_secrets: tuple[str, ...] = ()) -> int:
    """Load the secrets and make every log record pass through `redact`.

    For a caller that has not run `configure_logging` — a test, a scratch
    script — and wants the safety without the formatting. Idempotent, and it
    will not replace a patcher `configure_logging` has already installed, since
    that one already scrubs.

    Returns:
        How many distinct secret values are being scrubbed.
    """
    global _installed
    count = load_secrets(extra_secrets)
    if not _installed:
        logger.configure(patcher=_patch_record)
        _installed = True
    return count


def _patch_record(record: dict[str, Any]) -> None:
    """Scrub a record's message and any exception text, in place."""
    if record.get("message"):
        record["message"] = redact(record["message"])
    exception = record.get("exception")
    if exception is not None and getattr(exception, "value", None) is not None:
        # The formatted traceback is rendered from the exception object, so the
        # message on the exception itself is what has to be clean.
        try:
            exception.value.args = tuple(
                redact(arg) if isinstance(arg, str) else arg for arg in exception.value.args
            )
        except Exception:  # noqa: BLE001 - a read-only exception must not break logging
            pass


@dataclass(frozen=True)
class CallContext:
    """The ids that identify one call attempt, for every log line about it.

    Every field is optional because the same context object describes a
    campaign call (which has all of them), a browser session (which has none)
    and a recovery pass (which has the attempt but no live call).
    """

    campaign_id: int | None = None
    prospect_id: int | None = None
    attempt_id: int | None = None
    call_id: str | None = None
    provider: str | None = None
    #: Phase 22: the correlation id that is the same in every process that
    #: touches this call. `monitoring/tracing.py` says where it is born.
    trace_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def fields(self) -> dict[str, Any]:
        """The context as loguru `extra` fields, omitting what is not known."""
        values = {
            "trace": self.trace_id,
            "campaign": self.campaign_id,
            "prospect": self.prospect_id,
            "attempt": self.attempt_id,
            "call": self.call_id,
            "provider": self.provider,
        }
        present = {name: value for name, value in values.items() if value is not None}
        present.update({k: v for k, v in self.extra.items() if v is not None})
        return present

    def describe(self) -> str:
        """The context as a compact `k=v` fragment, for a human-readable line."""
        return " ".join(f"{name}={value}" for name, value in self.fields().items()) or "no call"


@contextmanager
def call_context(context: CallContext | None = None, **fields: Any) -> Iterator[None]:
    """Bind call ids onto every log record written inside the block.

    Nests: an inner block adds to the outer one rather than replacing it, which
    is what lets the dialer bind the campaign and prospect and the placement
    step add the carrier's call id when it learns it.
    """
    bound = dict(context.fields()) if context else {}
    bound.update({name: value for name, value in fields.items() if value is not None})
    # Phase 22: a keyword spelled the `CallContext` way (`trace_id=`,
    # `call_id=`) lands on the record under the same short field name the
    # `CallContext` form uses, so both spellings grep alike.
    for long_name, short_name in _FIELD_ALIASES.items():
        if long_name in bound:
            bound[short_name] = bound.pop(long_name)
    with logger.contextualize(**bound):
        yield


#: The `CallContext` attribute names, and the field each one is logged under.
_FIELD_ALIASES = {
    "trace_id": "trace",
    "campaign_id": "campaign",
    "prospect_id": "prospect",
    "attempt_id": "attempt",
    "call_id": "call",
}


def current_trace_id() -> str | None:
    """The trace bound on the current task's log context, or None. Phase 22.

    Read from loguru's own context rather than a second variable, so the
    two cannot disagree: whatever `call_context` bound is what this returns.
    """
    try:
        from loguru import _logger as _loguru_internals

        context = _loguru_internals.context.get()
    except Exception:  # noqa: BLE001 - a private name; None is the honest fallback
        return None
    value = context.get("trace") if isinstance(context, dict) else None
    return str(value) if value else None


@dataclass
class Timer:
    """Measures one operation, for the `latency_ms` field.

    A class rather than a bare `time.monotonic()` because the value is wanted
    in two places — the log line and the stored record — and rounding it the
    same way in both is one fewer thing to get inconsistent.
    """

    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed_secs(self) -> float:
        """Seconds since the timer was created."""
        return time.monotonic() - self.started

    @property
    def elapsed_ms(self) -> int:
        """Milliseconds since the timer was created, rounded."""
        return int(round(self.elapsed_secs * 1000))


def configure_logging(
    *, level: str | None = None, json_logs: bool | None = None, component: str | None = None
) -> None:
    """Set up logging for a process: one sink, scrubbed, optionally JSON.

    Reads `LOG_LEVEL` (default `INFO`) and `LOG_FORMAT` (`text` or `json`,
    default `text`). JSON is one object per line carrying the timestamp, level,
    module, message and every bound context field — the format a log shipper or
    `jq` wants, and the reason `call_context` exists.

    Phase 22: `component` names the process (`bot`, `scheduler`, `dashboard`,
    `api`, `webhooks`, `crm`, …) on every JSON line as `component`, beside
    the `pid`, so five processes' logs shipped to one place can be told
    apart without a filename. The text format leaves it out — a terminal
    already knows which process it is watching.

    Safe to call more than once; the last call wins. Call it *after*
    `load_dotenv`, or the environment it reads will be the shell's.
    """
    global _installed
    level = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    if json_logs is None:
        json_logs = (os.getenv("LOG_FORMAT") or "text").strip().lower() == "json"
    component = (component or os.getenv("LOG_COMPONENT") or "").strip() or None

    # Before the sink: the patcher this installs scrubs every record, and a
    # record written between adding the sink and loading the secrets would not
    # be scrubbed.
    load_secrets()
    logger.remove()
    # The process-wide fields. Underscored so `_json_sink` and the text
    # fragment can tell them from the per-call ones bound by `call_context`.
    extra: dict[str, Any] = {"_context": "", "_component": component, "_pid": os.getpid()}
    if json_logs:
        # `_json_sink` renders the whole record itself, so it needs only the
        # scrubbing patcher.
        logger.configure(patcher=_patch_record, extra=extra)
        logger.add(_json_sink, level=level, format="{message}")
    else:
        # One patcher does both jobs: two calls to `logger.configure(patcher=)`
        # do not compose — the second silently replaces the first, which is how
        # an earlier version of this ended up formatting records that had no
        # `_context` key and raising inside the logger.
        #
        # `extra` supplies a default for that key regardless, so a caller that
        # reconfigures the patcher later degrades to "no context on the line"
        # rather than to a KeyError on every log record.
        logger.configure(patcher=_patch_text_record, extra=extra)
        logger.add(
            sys.stderr,
            level=level,
            # The default format plus whichever call fields are bound. `{extra}`
            # renders as a dict, which is noisy; `_context_fragment` renders only
            # the fields that are set, in a fixed order.
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>{extra[_context]}"
            ),
        )
    _installed = True


def _patch_text_record(record: dict[str, Any]) -> None:
    """Scrub, then render the bound call fields for the text format."""
    _patch_record(record)
    record["extra"]["_context"] = _context_fragment(record["extra"])


def _context_fragment(extra: dict[str, Any]) -> str:
    """The bound call fields as ` | attempt=11 prospect=7`, or empty."""
    parts = [
        f"{name}={extra[name]}"
        for name in CALL_FIELDS
        if name in extra and extra[name] is not None
    ]
    return f"  <dim>[{' '.join(parts)}]</dim>" if parts else ""


def _json_sink(message: Any) -> None:
    """Write one log record as a JSON object on its own line."""
    record = message.record
    extra = record["extra"]
    payload: dict[str, Any] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": f"{record['name']}:{record['function']}:{record['line']}",
        "message": record["message"],
    }
    # Phase 22: which process wrote the line. Only when configured, so a
    # scratch script's JSON is not stamped with a component it does not have.
    if extra.get("_component"):
        payload["component"] = extra["_component"]
    if extra.get("_pid"):
        payload["pid"] = extra["_pid"]
    for name, value in extra.items():
        if name.startswith("_"):
            continue
        payload[name] = value
    if record["exception"] is not None:
        payload["exception"] = redact(
            f"{record['exception'].type.__name__}: {record['exception'].value}"
        )
    sys.stderr.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")


def event(name: str, **fields: Any) -> str:
    """Render a structured event as a log message.

    `event("call.placed", outcome="queued", latency_ms=412)` becomes
    `call.placed | outcome=queued latency_ms=412`. The name is dotted so that
    events sort into families, and the fields are `k=v` so that the text format
    stays greppable without needing JSON.

    Values are scrubbed and truncated: a log line is not the place for a
    paragraph, and the field it would come from is usually a vendor error.
    """
    rendered = " ".join(
        f"{key}={_render(value)}" for key, value in fields.items() if value is not None
    )
    return f"{name} | {rendered}" if rendered else name


def _render(value: Any) -> str:
    text = redact(str(value)).replace("\n", " ")
    return text if len(text) <= 160 else text[:157] + "..."


__all__ = [
    "CALL_FIELDS",
    "SECRET_ENV",
    "CallContext",
    "Timer",
    "call_context",
    "configure_logging",
    "current_trace_id",
    "event",
    "install_scrubber",
    "load_secrets",
    "redact",
]
