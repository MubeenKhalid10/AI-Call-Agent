"""The seam between "push this call to the CRM" and "which CRM". Phase 15.

Everything in this module is CRM-agnostic. A provider implementation
(`hubspot.py`) is the only place that knows a vendor's endpoints, property
names and error bodies; the syncer (`sync.py`) talks to `CrmProvider` and the
types below and never imports a vendor module — `make_crm_provider` in
`__init__.py` is the single point that knows which one is configured, exactly
as `telephony.make_provider` is for the carrier.

**What a CRM gets, and in what shape.** Two objects per finished call:

* a **contact** — the person the campaign phoned, matched by email or phone,
  created if the CRM has never seen them, and updated with what the latest
  call learned (`CrmContact`);
* an **activity** — the call itself: when, how long, what came of it, and a
  body that carries the summary, the qualification, the pain points, the
  objections, the meeting, the callback and the next action, composed by
  `mapping.py` from the `CallResult` and nothing else (`CallActivity`).

Vendors disagree about *where* each field goes — HubSpot has a call
engagement, Pipedrive an activity of type `call`, Salesforce a Task — so the
activity carries a neutral `fields` mapping (`qualification_status`,
`next_action`, `pain_points`, …) and each provider decides which of its
properties, standard or custom, each one lands in. Nothing here is vendor
vocabulary.

**Idempotency is the caller's, not the CRM's.** Neither HubSpot nor its
peers accept an idempotency key on create, so `CallActivity.key` — a short
token derived from the result and the attempt — is written *into* the
activity, and `find_activity` is how a syncer that lost the answer to a
create asks whether it happened before creating again. Same shape as
`TelephonyProvider.find_recent_calls` in Phase 9, for the same reason.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class CrmError(RuntimeError):
    """Something went wrong talking to the CRM. Message is for the log and the row."""

    #: Whether trying the same operation again could reasonably succeed. Read
    #: by `reliability/retry.py`'s classifiers, as `TelephonyError.retryable` is.
    retryable: bool = False


class CrmUnavailableError(CrmError):
    """The CRM could not be reached, rate-limited us, or answered with a server error.

    Transient by definition: nothing was wrong with the request. On a *write*
    it is the ambiguous case — the request may have been acted on before the
    answer was lost — and the syncer treats it as such.

    Attributes:
        retry_after_secs: What the CRM asked us to wait, when it said.
    """

    retryable = True

    def __init__(self, message: str, *, retry_after_secs: float | None = None) -> None:
        """Record the message and the CRM's own wait, if it gave one."""
        super().__init__(message)
        self.retry_after_secs = retry_after_secs


class CrmAuthError(CrmError):
    """The credentials were rejected, or lack a scope the sync needs.

    Not a property of one record: every record would fail the same way, so
    the syncer stops the pass rather than marking a hundred rows failed.
    """


class CrmRejectedError(CrmError):
    """The CRM read the request and refused it on its merits.

    A property that does not exist, a value it will not take, a malformed
    email. Retrying reproduces the refusal, so the record is marked failed
    with the CRM's own words for a person to read.

    Attributes:
        status: The HTTP status, when the refusal came over HTTP — a provider
            reads it to tell "not found" (create it) from "not allowed".
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Record the message and the status it came with."""
        super().__init__(message)
        self.status = status


class CallOutcome(StrEnum):
    """What the call came to, in the four words every CRM's call log has a home for."""

    CONNECTED = "CONNECTED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    VOICEMAIL = "VOICEMAIL"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CrmContact:
    """The person, as a CRM would file them.

    Attributes:
        first_name / last_name: From the prospect row.
        phone: E.164, the number that was dialled. The primary match key when
            there is no email.
        email: The primary match key when present — every CRM treats it as
            the contact's unique identifier.
        company / job_title: From the prospect row, when the import had them.
        external_id: The CRM's own id, once known.
    """

    first_name: str
    last_name: str
    phone: str | None = None
    email: str | None = None
    company: str | None = None
    job_title: str | None = None
    external_id: str | None = None

    @property
    def has_identity(self) -> bool:
        """Whether there is anything to match the contact on."""
        return bool(self.email or self.phone)

    @property
    def display_name(self) -> str:
        """First and last name, for a title or a log line."""
        return f"{self.first_name} {self.last_name}".strip() or "Unknown"


@dataclass(frozen=True)
class CallActivity:
    """One call, as a CRM would log it against the contact.

    Attributes:
        key: The idempotency token, written into the activity so a lost
            answer can be resolved by `CrmProvider.find_activity`. Alphanumeric
            only, so a CRM's text tokeniser keeps it whole.
        title: One line, e.g. `AI call — meeting booked — Sara Ali`.
        body: The full account, composed by `mapping.py`: summary, outcome,
            qualification, pain points, objections, meeting, callback, next
            action. Plain text with headed sections; every CRM's note field
            takes it.
        outcome: The one of five words the CRM's disposition field takes.
        occurred_at: When the call was made. Timezone-aware.
        duration_seconds: The call's length, when known.
        from_number / to_number: The caller ID and the number dialled.
        fields: The structured facts by neutral name — `disposition`,
            `qualification_status`, `interest_level`, `next_action`,
            `meeting_status`, `meeting_start`, `callback_status`,
            `callback_scheduled_for`, `pain_points`, `objections`, `summary`,
            `campaign`, … — each a string, so a provider can put it in a
            property of its own without re-deriving anything.
    """

    key: str
    title: str
    body: str
    outcome: CallOutcome
    occurred_at: datetime
    duration_seconds: int | None = None
    from_number: str | None = None
    to_number: str | None = None
    fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CallSync:
    """Everything one finished call sends to the CRM: the person and the call."""

    contact: CrmContact
    activity: CallActivity


@dataclass(frozen=True)
class SyncReceipt:
    """What the CRM now holds for one call."""

    contact_id: str
    activity_id: str
    created: bool
    """Whether the activity was created on this pass (False: an existing one was updated)."""


class CrmProvider(ABC):
    """Files calls with one CRM. Six methods is the whole contract.

    Deliberately not in it: deciding *what* to send (that is `mapping.py`,
    vendor-neutral), and deciding *when* or *again* (that is `sync.py`). A
    provider translates; it does not schedule and it does not retry.
    """

    #: Provider name, matching the `CRM_PROVIDER` value that selects it.
    name: str = "unknown"

    async def ensure_schema(self) -> None:
        """Create whatever custom fields this provider files the call facts in.

        Idempotent, and optional: a provider that files everything in
        standard fields does nothing here. Called once per sync run. A failure
        — a token without the properties scope, say — raises `CrmError`; the
        syncer logs it and carries on with the standard fields only, because a
        richer record that cannot be written should not stop the plain one.
        """

    @abstractmethod
    async def find_contact(self, contact: CrmContact) -> str | None:
        """The CRM's id for this person, or None if it has never seen them.

        Matches on email first, then on phone, and never on name alone: two
        people can share a name and nothing here may file one call under the
        other. Raises `CrmUnavailableError` / `CrmAuthError` as `_request` does.
        """

    @abstractmethod
    async def create_contact(self, contact: CrmContact) -> str:
        """Create the contact and return the CRM's id.

        A CRM that refuses because the person already exists must return the
        existing id rather than raise: that answer *is* the contact.
        """

    @abstractmethod
    async def update_contact(self, contact_id: str, contact: CrmContact, activity: CallActivity) -> None:
        """Write what the latest call learned onto the contact.

        The qualification, the next action, the last disposition and when —
        into whatever fields this provider keeps them in. Must not overwrite
        the contact's own identity fields with campaign data: a CRM's record of
        a person is richer than a CSV import, and this is not the place to
        flatten it.
        """

    @abstractmethod
    async def find_activity(self, key: str) -> str | None:
        """The activity that carries `key`, or None.

        The ambiguity resolver: asked before a create is repeated after a lost
        answer. May lag a moment behind a create on CRMs that index
        asynchronously; the syncer's backoff is what covers that.
        """

    @abstractmethod
    async def create_activity(self, contact_id: str, activity: CallActivity) -> str:
        """Log the call against the contact and return the activity's id."""

    @abstractmethod
    async def update_activity(self, activity_id: str, contact_id: str, activity: CallActivity) -> None:
        """Rewrite an activity that was filed before, from a result that changed since."""

    async def check_credentials(self) -> str:
        """Confirm the token is accepted with the cheapest read, for the health check.

        Raises:
            CrmAuthError: The credentials were rejected.
            CrmUnavailableError: The CRM was unreachable.
            NotImplementedError: This provider has no cheap check.
        """
        raise NotImplementedError(f"{self.name} has no credential check")

    async def close(self) -> None:
        """Release the provider's HTTP resources. Safe to call more than once."""

    def describe(self) -> str:
        """One line for the startup log. Never a credential."""
        return self.name


__all__ = [
    "CallActivity",
    "CallOutcome",
    "CallSync",
    "CrmAuthError",
    "CrmContact",
    "CrmError",
    "CrmProvider",
    "CrmRejectedError",
    "CrmUnavailableError",
    "SyncReceipt",
]
