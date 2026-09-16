"""The do-not-call list: one row per number that must never be dialled. Phase 19.

Until this phase a do-not-call lived on the *prospect* row (`ProspectStatus.
DO_NOT_CALL`), which is the right place for "this person asked" and the
wrong place for three things: a number imported again under a new prospect
row, a number that was never a prospect (a registry, a suppression file, a
caller who rang in), and a record of *when* and *why* and *who* — the fact
an operator has to produce when asked. The `dnc_numbers` table is that
record. It is keyed by the normalised number, it is never deleted (a
removal is a `revoked_at` stamp with a name on it), and the queue's SQL, the
pre-dial gate, the importer and the API all consult it independently of the
prospect's status.

The prospect status stays. Both are enforced, because each covers what the
other cannot: the status closes a person's open memberships the moment they
ask; the list survives the person's row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class DncSource(StrEnum):
    """How a number came to be on the list. Recorded, never inferred."""

    VERBAL = "verbal"
    """The person asked during a call — the agent's detector or its tool."""

    API = "api"
    """`POST /api/v1/dnc` or `/prospects/{id}/do-not-call`, by an API key."""

    CLI = "cli"
    """`campaign.py dnc`, by whoever ran it."""

    IMPORT = "import"
    """A suppression file (`campaign.py dnc-import`)."""

    REGISTRY = "registry"
    """An external do-not-call registry the operator screens against."""

    MANUAL = "manual"
    """Anything else an operator did by hand."""


@dataclass(frozen=True)
class DncEntry:
    """One number on the list, and the facts around it."""

    phone_normalized: str
    source: DncSource = DncSource.MANUAL
    reason: str | None = None
    prospect_id: int | None = None
    campaign_id: int | None = None
    call_attempt_id: int | None = None
    created_by: str | None = None
    note: str | None = None
    id: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoke_reason: str | None = None

    @property
    def active(self) -> bool:
        """Whether the number is currently blocked (not revoked, not expired)."""
        return self.revoked_at is None

    def is_active_at(self, moment: datetime) -> bool:
        """Whether the entry blocks at `moment`, honouring an expiry."""
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > moment

    def to_dict(self) -> dict[str, Any]:
        """The entry as JSON, for the API and the CLI. The number is masked by the API for a viewer."""
        return {
            "id": self.id,
            "phone_normalized": self.phone_normalized,
            "source": self.source.value,
            "reason": self.reason,
            "prospect_id": self.prospect_id,
            "campaign_id": self.campaign_id,
            "call_attempt_id": self.call_attempt_id,
            "created_by": self.created_by,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_by": self.revoked_by,
            "revoke_reason": self.revoke_reason,
            "active": self.active,
        }


def parse_source(text: str | None, default: DncSource = DncSource.MANUAL) -> DncSource:
    """A source from its name; the default for anything unrecognised or empty."""
    try:
        return DncSource(str(text or "").strip().lower()) if text else default
    except ValueError:
        return default


__all__ = ["DncEntry", "DncSource", "parse_source"]
