"""Building the scheduler from configuration, in one place. Phase 25.

`campaign.py run` has assembled the service, the guards, the compliance
gate, the recovery pass, the dialer and the worker since Phase 13, and the
unified application now runs the same loop inside itself (`src/app/engine.py`).
Two assemblies would drift; this module is the one both call. Nothing here
decides anything — every value comes from `Config`, every object is the one
the earlier phases built — and nothing here dials: `build_worker` returns a
worker that has not started.

The compliance gate is imported inside the function rather than at the top
because `src.compliance` reaches back into this package (see the Phase 19
note in the handoff); importing it here at module level would make the
package import itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..reliability.guardrails import CallingWindow, CampaignGuards, PacingLimiter
from ..telephony import TelephonyProvider
from .dialer import CampaignDialer
from .recovery import AttemptRecovery
from .service import CampaignService
from .store import CampaignStore
from .worker import CampaignWorker


def build_service(config: Config, store: CampaignStore) -> CampaignService:
    """The service with the calling rules from configuration (Phases 5, 19, 21)."""
    return CampaignService(
        store,
        default_region=config.default_phone_region,
        max_attempts=config.campaign_max_attempts,
        retry_minutes=config.campaign_retry_minutes,
        compliance=config.policy_resolver(),
        retry_transient_failures=config.worker.retry_transient_failures,
        transient_retry_minutes=config.worker.transient_retry_minutes,
    )


def build_guards(config: Config) -> CampaignGuards:
    """The calling-hours, concurrency and pacing limits from configuration (Phase 9)."""
    reliability = config.reliability
    return CampaignGuards(
        window=CallingWindow.parse(
            reliability.calling_hours,
            reliability.calling_days,
            reliability.calling_timezone,
            enabled=reliability.enforce_calling_hours,
        ),
        pacing=PacingLimiter(reliability.pacing_secs),
        max_concurrent=reliability.max_concurrent_calls,
    )


def build_gate(config: Config, service: CampaignService, *, actor: str = "dialer") -> Any:
    """The compliance gate every dial passes through (Phase 19)."""
    from ..compliance import ComplianceGate
    from ..security import AuditLog

    return ComplianceGate(
        service,
        config.policy_resolver(),
        audit=AuditLog(
            lambda: service.store,
            enabled=config.security.audit_enabled,
            strict=config.security.audit_strict,
        ),
        actor=actor,
    )


@dataclass
class Scheduler:
    """What `build_worker` assembled, kept together so a host can describe and stop it."""

    service: CampaignService
    guards: CampaignGuards
    dialer: CampaignDialer
    recovery: AttemptRecovery
    worker: CampaignWorker


def build_worker(
    config: Config,
    store: CampaignStore,
    provider: TelephonyProvider,
    *,
    campaign_ids: list[int] | None = None,
    max_calls: int | None = None,
    once: bool = False,
    auto_complete: bool | None = None,
    worker_id: str | None = None,
    status_callback_url: str | None = None,
    public_url: str | None = None,
) -> Scheduler:
    """Assemble the scheduler exactly as `campaign.py run` does.

    Args:
        config: The deployment's configuration.
        store: An open store; the caller owns it.
        provider: The carrier. `make_provider(config.telephony)` in
            production; the checks and the audit hand in a stand-in.
        campaign_ids: Serve only these campaigns (None: every `ACTIVE` one).
        max_calls / once: The CLI's bounds; None / False for a service.
        auto_complete: Mark a campaign `COMPLETED` when its queue is empty.
            None takes `WORKER_AUTO_COMPLETE`.
        worker_id: This process's name in the fleet. None takes `WORKER_ID`,
            and the worker makes one up when that is unset too.
        status_callback_url: Where the carrier posts call events. None takes
            the configured webhook URL, which is None when webhooks are off.
        public_url: Where the carrier streams the call's audio (the bot).
            None takes `TELEPHONY_PUBLIC_URL`.
    """
    telephony = config.telephony
    service = build_service(config, store)
    guards = build_guards(config)
    recovery = AttemptRecovery(
        service, provider, min_age_secs=config.reliability.recovery_min_age_secs
    )
    webhook_url = telephony.webhook_url() if status_callback_url is None else status_callback_url
    dialer = CampaignDialer(
        service,
        provider,
        from_number=telephony.from_number or "",
        public_url=(public_url if public_url is not None else telephony.public_url) or "",
        stream_path=telephony.stream_path,
        answer_timeout_secs=telephony.answer_timeout_secs,
        guards=guards,
        machine_detection=telephony.machine_detection,
        status_callback_url=webhook_url,
        gate=build_gate(config, service),
    )
    worker = CampaignWorker(
        service,
        dialer,
        recovery=recovery,
        guards=guards,
        campaign_ids=campaign_ids,
        poll_secs=config.worker.poll_secs,
        idle_secs=config.worker.idle_secs,
        recovery_interval_secs=config.worker.recovery_interval_secs,
        recovery_min_age_secs=config.reliability.recovery_min_age_secs,
        drain_secs=config.worker.drain_secs,
        report_secs=config.worker.report_secs,
        auto_complete=config.worker.auto_complete if auto_complete is None else auto_complete,
        max_calls=max_calls,
        once=once,
        webhook_poll_secs=config.worker.webhook_poll_secs if webhook_url else 0.0,
        worker_id=worker_id or config.worker.worker_id,
        heartbeat_secs=config.worker.heartbeat_secs,
        stale_secs=config.worker.stale_secs,
        adopt_secs=config.worker.adopt_secs,
    )
    return Scheduler(service=service, guards=guards, dialer=dialer, recovery=recovery, worker=worker)


__all__ = ["Scheduler", "build_gate", "build_guards", "build_service", "build_worker"]
