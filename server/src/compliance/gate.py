"""The gate every outbound call passes through, and the record it leaves. Phase 19.

One object, one question: *may this call be placed, for this person, under
the policy that applies to them, right now?* — answered in an order that
puts the strongest rule first and never raises:

    1. the do-not-call list          the number, whatever row it is on
    2. the prospect's status         `DO_NOT_CALL` on the person
    3. the campaign's rules          `service.check_callable`: a dialable number, an
                                     ACTIVE campaign, an open membership, the attempt
                                     ceiling *from the policy*, the retry wait, no live call
    4. the calling window            the policy's hours, in the prospect's own zone

Every decision — refused *or allowed* — is written to the audit log with the
policy it was taken under (`compliance.blocked`, `compliance.allowed`), so
"why was this person phoned at 09:03 on a Tuesday" and "why was this person
not phoned" have the same answer: a row.

The gate does not dial and does not change a row itself. It tells the dialer
what kind of refusal this is (`Verdict`), and the dialer acts: a do-not-call
closes the attempt as `DO_NOT_CALL` (a clear disposition, not a `FAILED`
with a sentence in it); a closed window or a retry that is not yet due
gives the reservation back unspent; an exhausted ceiling closes the
membership; anything else releases as before.

Phase 9's `check_callable` and Phase 13's window check are what the gate
*calls*; nothing was removed. The gate is where they are called from now,
with the policy's numbers instead of the environment's, and with the list
in front of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..reliability.guardrails import Decision, prospect_timezone
from ..reliability.observability import event
from ..security import AuditLog, Principal, Role
from .dnc import DncEntry
from .policy import CompliancePolicy, PolicyResolver

if TYPE_CHECKING:  # pragma: no cover - typing only; the gate never imports the store at runtime
    from ..campaigns.models import Campaign, CampaignProspect, Prospect
    from ..campaigns.service import CampaignService


class Verdict(StrEnum):
    """What the dialer should do with a decision."""

    ALLOW = "allow"
    DNC = "dnc"
    """Close the attempt as `DO_NOT_CALL`. The number must not be dialled, now or later."""
    DEFER = "defer"
    """Give the reservation back unspent; ask again at `retry_after_secs`."""
    EXHAUST = "exhaust"
    """Give the reservation back and close the membership: the ceiling is reached."""
    RELEASE = "release"
    """Record a failed attempt with the reason, as Phase 9 did."""


@dataclass(frozen=True)
class ComplianceDecision:
    """Whether a call may go out, why not, and what to do about it."""

    allowed: bool
    verdict: Verdict
    code: str = "ok"
    reason: str = ""
    retry_after_secs: float | None = None
    policy: CompliancePolicy | None = None
    region: str | None = None
    entry: DncEntry | None = None

    @property
    def refused(self) -> bool:
        return not self.allowed

    def as_decision(self) -> Decision:
        """The Phase 9 shape, for `DialResult.blocked_by`."""
        return Decision.ok() if self.allowed else Decision.no(self.reason, retry_after_secs=self.retry_after_secs)

    @classmethod
    def ok(cls, policy: CompliancePolicy, region: str | None) -> ComplianceDecision:
        return cls(True, Verdict.ALLOW, policy=policy, region=region)

    @classmethod
    def no(
        cls,
        verdict: Verdict,
        code: str,
        reason: str,
        *,
        policy: CompliancePolicy | None,
        region: str | None,
        retry_after_secs: float | None = None,
        entry: DncEntry | None = None,
    ) -> ComplianceDecision:
        return cls(False, verdict, code, reason, retry_after_secs, policy, region, entry)


#: The process principals the gate writes audit rows as. Not people: the
#: dialer and the scheduler act on rows people wrote, and the row says so.
PROCESS_ROLE = Role.OPERATOR


class ComplianceGate:
    """Checks one call against the list, the person, the campaign and the policy."""

    def __init__(
        self,
        service: CampaignService,
        resolver: PolicyResolver,
        *,
        audit: AuditLog | None = None,
        clock: Callable[[], datetime] | None = None,
        actor: str = "dialer",
    ) -> None:
        """Create the gate.

        Args:
            service: Campaign rules and persistence. `check_callable` and the
                store's `find_dnc` are what it calls.
            resolver: Builds the policy for a call from the three layers.
            audit: Where decisions are recorded. None writes the log line only.
            clock: Where "now" comes from; the scheduler's checks inject one.
            actor: The process name on the audit rows (`dialer`, `worker`).
        """
        self._service = service
        self._resolver = resolver
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._principal = Principal(name=actor, role=PROCESS_ROLE, via="process")
        self._list_unavailable_warned = False
        self.checked = 0
        self.refused = 0

    @property
    def resolver(self) -> PolicyResolver:
        return self._resolver

    def now(self) -> datetime:
        return self._clock()

    def policy_for(self, campaign: Campaign | None, prospect: Prospect | None) -> tuple[CompliancePolicy, str | None]:
        """The policy that would apply to this person on this campaign."""
        return self._resolver.for_call(
            campaign.configuration if campaign is not None else None,
            prospect.phone_normalized if prospect is not None else None,
            campaign_id=campaign.id if campaign is not None else None,
        )

    async def check(
        self,
        prospect: Prospect,
        campaign: Campaign | None,
        membership: CampaignProspect | None,
        *,
        ignore_attempt_id: int | None = None,
        ignore_attempt_limit: bool = False,
        attempt_id: int | None = None,
        purpose: str = "dial",
    ) -> ComplianceDecision:
        """Decide, and record the decision. Never raises."""
        self.checked += 1
        policy, region = self.policy_for(campaign, prospect)
        decision = await self._decide(
            prospect, campaign, membership, policy, region,
            ignore_attempt_id=ignore_attempt_id, ignore_attempt_limit=ignore_attempt_limit,
        )
        if decision.refused:
            self.refused += 1
        await self._record(decision, prospect, campaign, attempt_id=attempt_id, purpose=purpose)
        return decision

    async def _decide(
        self,
        prospect: Prospect,
        campaign: Campaign | None,
        membership: CampaignProspect | None,
        policy: CompliancePolicy,
        region: str | None,
        *,
        ignore_attempt_id: int | None,
        ignore_attempt_limit: bool,
    ) -> ComplianceDecision:
        number = prospect.phone_normalized
        # 1. The list. Consulted before anything else, because a number on it
        #    is blocked whatever the prospect row says and whichever row it is.
        entry = await self._listed(number)
        if entry is not None:
            parts = [f"source {entry.source.value}"]
            if entry.reason:
                parts.append(entry.reason)
            if entry.created_at:
                parts.append(f"since {entry.created_at:%Y-%m-%d}")
            return ComplianceDecision.no(
                Verdict.DNC,
                "dnc_list",
                f"the number is on the do-not-call list ({', '.join(parts)})",
                policy=policy,
                region=region,
                entry=entry,
            )
        # 2. The person.
        if prospect.status.value == "DO_NOT_CALL":
            return ComplianceDecision.no(Verdict.DNC, "dnc_status", "the prospect is marked DO_NOT_CALL", policy=policy, region=region)

        # 3. The campaign's rules, with the policy's ceiling.
        check = await self._service.check_callable(
            prospect,
            campaign,
            membership,
            ignore_attempt_id=ignore_attempt_id,
            ignore_attempt_limit=ignore_attempt_limit,
            max_attempts=policy.max_attempts,
        )
        if not check:
            reason = check.reason
            lowered = reason.lower()
            if "do_not_call" in lowered:
                return ComplianceDecision.no(Verdict.DNC, "dnc_status", reason, policy=policy, region=region)
            if "attempt limit" in lowered:
                return ComplianceDecision.no(Verdict.EXHAUST, "attempt_limit", reason, policy=policy, region=region)
            if "not due for a retry" in lowered:
                wait = 60.0
                if membership is not None and membership.next_attempt_at is not None:
                    wait = max(1.0, (membership.next_attempt_at - self.now()).total_seconds())
                return ComplianceDecision.no(Verdict.DEFER, "retry_wait", reason, policy=policy, region=region, retry_after_secs=wait)
            if "already on a call" in lowered:
                return ComplianceDecision.no(Verdict.DEFER, "live_call", reason, policy=policy, region=region, retry_after_secs=30.0)
            code = "campaign_inactive" if "not ACTIVE" in reason else "membership_closed" if "for them" in reason else "not_dialable"
            return ComplianceDecision.no(Verdict.RELEASE, code, reason, policy=policy, region=region)

        # 4. The window, under the policy, in their own zone where the record gives one.
        if policy.enforce_calling_hours:
            window = policy.window(clock=self._clock).check(
                now=self.now(), timezone=prospect_timezone(prospect.custom_data) or policy.timezone
            )
            if window.refused:
                return ComplianceDecision.no(
                    Verdict.DEFER, "window_closed", window.reason, policy=policy, region=region,
                    retry_after_secs=window.retry_after_secs or 0.0,
                )
        return ComplianceDecision.ok(policy, region)

    async def _listed(self, number: str | None) -> DncEntry | None:
        """The active list entry for a number, or None. A missing table is a warning, once."""
        if not number:
            return None
        finder = getattr(self._service.store, "find_dnc", None)
        if finder is None:
            return None
        try:
            entry = await finder(number)
        except Exception as exc:  # noqa: BLE001 - a store without the table must not stop the campaign
            if not self._list_unavailable_warned:
                self._list_unavailable_warned = True
                logger.warning(
                    event(
                        "compliance.dnc_list_unavailable",
                        error=(str(exc).splitlines() or [type(exc).__name__])[0],
                        outcome="the prospect's status still applies; run `uv run campaign.py init`",
                    )
                )
            return None
        if entry is not None and not entry.is_active_at(self.now()):
            return None
        return entry

    async def _record(
        self,
        decision: ComplianceDecision,
        prospect: Prospect,
        campaign: Campaign | None,
        *,
        attempt_id: int | None,
        purpose: str,
    ) -> None:
        policy = decision.policy
        detail: dict[str, Any] = {
            "purpose": purpose,
            "code": decision.code,
            "region": decision.region,
            "jurisdiction": policy.jurisdiction if policy else None,
            "policy": policy.describe() if policy else None,
            "campaign": campaign.id if campaign else None,
            "attempt": attempt_id,
            "retry_after_secs": int(decision.retry_after_secs) if decision.retry_after_secs else None,
            "dnc_source": decision.entry.source.value if decision.entry else None,
        }
        if decision.refused:
            detail["reason"] = decision.reason
        action = "compliance.allowed" if decision.allowed else "compliance.blocked"
        if self._audit is None:
            logger.info(event(action, outcome=decision.code, prospect=prospect.id, **{k: v for k, v in detail.items() if k in ("region", "jurisdiction")}))
            return
        await self._audit.record(
            action,
            principal=self._principal,
            outcome=decision.code if decision.refused else "allowed",
            target=("prospect", prospect.id),
            **detail,
        )


__all__ = ["PROCESS_ROLE", "ComplianceDecision", "ComplianceGate", "Verdict"]
