"""The audit log: who did what, to which row, from where. Phase 18.

Every sensitive action — a login and a failed one, a key refused, a
permission denied, a prospect created or imported, a campaign moved, a
call queued, a callback withdrawn, a do-not-call, a transcript read, an
outbox row reopened — is one `AuditEntry`, written to the `audit_log`
table through the store *and* emitted as an `audit.<action>` log line, so
a deployment that ships its logs has the trail even if the table is not
being read, and a deployment that only has the database has it too.

**What an entry never contains.** A password, an API key, a session token,
or a phone number in the clear. The actor is a user name or a key's label
(`api-key#2`); the detail is scrubbed through the same `redact` every log
line passes through, and phone-shaped values are masked. An audit log is
read by more people than a database is, and a leak *from the audit log*
is the kind of irony this project would rather not explain.

**When the table cannot be written.** By default the action still goes
ahead and the failure is logged loudly (`audit.unavailable`), once per
process rather than per request; the log line above is still emitted. With
`SECURITY_AUDIT_STRICT=true` the failure is raised instead and the action
is refused, for a deployment where "not recorded" must mean "did not
happen". The store's `record_audit` is duck-typed rather than imported:
this module sits under `config.py` in the import graph and must not pull
the store in.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from .pii import PHONE_KEYS, mask_phone
from .roles import Principal, Role

#: How much of one detail value is kept. An audit entry is a sentence about
#: an action, not a copy of its request body.
MAX_DETAIL_CHARS = 400
MAX_DETAIL_KEYS = 24
#: Detail fields whose *name* says they hold a credential. Their value is
#: never stored, whatever it turns out to be.
_SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[-_]?key|authorization|cookie|session)")


class AuditUnavailable(RuntimeError):
    """The audit table could not be written and strict mode is on."""


@dataclass(frozen=True)
class AuditEntry:
    """One recorded action."""

    action: str
    actor: str
    role: str
    via: str
    outcome: str = "ok"
    target_kind: str | None = None
    target_id: str | None = None
    ip: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """The entry as JSON, for the API and the CLI."""
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "action": self.action,
            "actor": self.actor,
            "role": self.role,
            "via": self.via,
            "outcome": self.outcome,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "ip": self.ip,
            "detail": dict(self.detail),
        }


#: A principal for entries with nobody behind them: a refused key, a login
#: that failed, a request from the anonymous dashboard.
NOBODY = Principal(name="-", role=Role.VIEWER, via="none")


class AuditLog:
    """Writes entries through a store, and always to the log."""

    def __init__(
        self,
        store_of: Callable[[], Any] | None = None,
        *,
        enabled: bool = True,
        strict: bool = False,
        clock: Callable[[], datetime] | None = None,
        keep_recent: int = 200,
    ) -> None:
        """Create the writer.

        Args:
            store_of: Returns the store to write through, or raises when there
                is none yet. None: log lines only (a CLI, a check).
            enabled: Whether entries are written to the store at all.
            strict: Raise `AuditUnavailable` when the store cannot take the
                entry, instead of logging and carrying on.
            clock: Where "now" comes from; the checks inject one.
            keep_recent: How many entries are kept in memory, for a process
                without a table (and for the checks).
        """
        self._store_of = store_of
        self._enabled = enabled
        self._strict = strict
        self._clock = clock or (lambda: datetime.now(UTC))
        self.recent: deque[AuditEntry] = deque(maxlen=keep_recent)
        self.written = 0
        self.failed = 0
        self._last_warning = 0.0

    @property
    def strict(self) -> bool:
        return self._strict

    async def record(
        self,
        action: str,
        *,
        principal: Principal | None = None,
        outcome: str = "ok",
        target: tuple[str, Any] | None = None,
        ip: str | None = None,
        **detail: Any,
    ) -> AuditEntry:
        """Record one action. Returns the entry; raises only in strict mode."""
        who = principal or NOBODY
        entry = AuditEntry(
            action=action,
            actor=who.name,
            role=who.role.value,
            via=who.via,
            outcome=outcome,
            target_kind=str(target[0]) if target else None,
            target_id=str(target[1]) if target and target[1] is not None else None,
            ip=ip,
            detail=_clean_detail(detail),
            created_at=self._clock(),
        )
        self.recent.append(entry)
        logger.info(_line(entry))
        if not self._enabled or self._store_of is None:
            return entry
        try:
            store = self._store_of()
            record = store.record_audit
        except Exception as exc:  # noqa: BLE001 - no store yet, or a store without the method
            return self._unavailable(entry, exc)
        try:
            saved = await record(
                action=entry.action,
                actor=entry.actor,
                role=entry.role,
                via=entry.via,
                outcome=entry.outcome,
                target_kind=entry.target_kind,
                target_id=entry.target_id,
                ip=entry.ip,
                detail=entry.detail,
                created_at=entry.created_at,
            )
        except Exception as exc:  # noqa: BLE001 - the database went away; say so, once
            return self._unavailable(entry, exc)
        self.written += 1
        return saved if isinstance(saved, AuditEntry) else entry

    def _unavailable(self, entry: AuditEntry, exc: Exception) -> AuditEntry:
        self.failed += 1
        reason = (str(exc).splitlines() or [type(exc).__name__])[0] if str(exc) else exc.__class__.__name__
        now = time.monotonic()
        if now - self._last_warning > 60.0:
            self._last_warning = now
            logger.warning(
                f"audit.unavailable | action={entry.action} error={_short(reason)} "
                f"outcome={'refused (SECURITY_AUDIT_STRICT)' if self._strict else 'the action went ahead; the log line above is the record'}"
            )
        if self._strict:
            raise AuditUnavailable(f"the audit log could not be written: {reason}") from exc
        return entry


def _line(entry: AuditEntry) -> str:
    """The log line. Rendered through `event` when it is available."""
    fields: dict[str, Any] = {
        "actor": entry.actor,
        "role": entry.role,
        "via": entry.via,
        "outcome": entry.outcome,
        "target": f"{entry.target_kind}:{entry.target_id}" if entry.target_kind else None,
        "ip": entry.ip,
    }
    fields.update({k: v for k, v in entry.detail.items() if k not in fields})
    try:
        from ..reliability.observability import event

        return event(f"audit.{entry.action}", **fields)
    except Exception:  # noqa: BLE001 - logging must not depend on the import graph
        rendered = " ".join(f"{k}={_short(str(v))}" for k, v in fields.items() if v is not None)
        return f"audit.{entry.action} | {rendered}"


def _clean_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Drop Nones, mask phones, scrub secrets, cap sizes."""
    try:
        from ..reliability.observability import redact
    except Exception:  # noqa: BLE001

        def redact(text: str) -> str:  # type: ignore[misc]
            return text

    cleaned: dict[str, Any] = {}
    for key, value in list(detail.items())[:MAX_DETAIL_KEYS]:
        if value is None:
            continue
        name = str(key)
        if _SECRET_KEY.search(name):
            # A field *named* like a credential is one, whatever it holds.
            cleaned[name] = "***"
        elif name in PHONE_KEYS:
            cleaned[name] = mask_phone(value)
        elif isinstance(value, bool | int | float):
            cleaned[name] = value
        elif isinstance(value, list | tuple | set | frozenset):
            cleaned[name] = [_short(redact(str(item))) for item in list(value)[:20]]
        elif isinstance(value, dict):
            cleaned[name] = {str(k): _short(redact(str(v))) for k, v in list(value.items())[:20]}
        else:
            cleaned[name] = _short(redact(str(value)))
    return cleaned


def _short(text: str) -> str:
    return text if len(text) <= MAX_DETAIL_CHARS else text[: MAX_DETAIL_CHARS - 1] + "…"


__all__ = ["MAX_DETAIL_CHARS", "NOBODY", "AuditEntry", "AuditLog", "AuditUnavailable"]
