"""The rows as JSON, one shape each, for the API and the events. Phase 17.

Both surfaces show the same objects — a prospect in a `call.completed`
payload is the prospect `GET /api/v1/prospects/{id}` returns — so the
shapes live once, here, and neither surface invents its own. Every datetime
is ISO 8601 with its offset; every enum is its string value; nothing is
renamed from the dataclass it came from, so a field in the docs is a field
in `campaigns/models.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..campaigns.models import (
    AutomationEvent,
    CallAttempt,
    CallTransfer,
    Campaign,
    CampaignProspect,
    Meeting,
    Prospect,
    ScheduledCallback,
)
from ..campaigns.results import CallResult
from ..campaigns.store import CampaignCounts, QueueOutlook


def iso(moment: datetime | None) -> str | None:
    """An ISO 8601 string with offset, or None."""
    return moment.isoformat() if moment is not None else None


def prospect_dict(prospect: Prospect) -> dict[str, Any]:
    """A prospect, as the API shows it."""
    return {
        "id": prospect.id,
        "first_name": prospect.first_name,
        "last_name": prospect.last_name,
        "full_name": prospect.full_name,
        "phone": prospect.phone,
        "phone_normalized": prospect.phone_normalized,
        "email": prospect.email,
        "company": prospect.company,
        "job_title": prospect.job_title,
        "industry": prospect.industry,
        "location": prospect.location,
        "website": prospect.website,
        "custom_data": dict(prospect.custom_data),
        "status": prospect.status.value,
        "dialable": prospect.is_callable,
        "created_at": iso(prospect.created_at),
        "updated_at": iso(prospect.updated_at),
    }


def campaign_dict(
    campaign: Campaign,
    *,
    counts: CampaignCounts | None = None,
    outlook: QueueOutlook | None = None,
) -> dict[str, Any]:
    """A campaign, with its membership counts and queue outlook when given."""
    data: dict[str, Any] = {
        "id": campaign.id,
        "name": campaign.name,
        "description": campaign.description,
        "status": campaign.status.value,
        "dialable": campaign.status.is_dialable,
        "configuration": dict(campaign.configuration),
        "created_at": iso(campaign.created_at),
        "updated_at": iso(campaign.updated_at),
        "started_at": iso(campaign.started_at),
        "paused_at": iso(campaign.paused_at),
        "completed_at": iso(campaign.completed_at),
    }
    if counts is not None:
        data["counts"] = {
            "total": counts.total,
            "pending": counts.pending,
            "in_progress": counts.in_progress,
            "completed": counts.completed,
            "exhausted": counts.exhausted,
            "skipped": counts.skipped,
        }
    if outlook is not None:
        data["queue"] = {
            "due_now": outlook.due_now,
            "next_due_at": iso(outlook.next_due_at),
            "undialable": outlook.undialable,
            "pending_callbacks": outlook.pending_callbacks,
            "next_callback_at": iso(outlook.next_callback_at),
            "has_live_work": outlook.has_live_work,
            "is_finished": outlook.is_finished,
        }
    return data


def membership_dict(membership: CampaignProspect) -> dict[str, Any]:
    """One prospect's membership of one campaign."""
    return {
        "id": membership.id,
        "campaign_id": membership.campaign_id,
        "prospect_id": membership.prospect_id,
        "status": membership.status.value,
        "attempt_count": membership.attempt_count,
        "last_attempt_at": iso(membership.last_attempt_at),
        "next_attempt_at": iso(membership.next_attempt_at),
        "created_at": iso(membership.created_at),
        "updated_at": iso(membership.updated_at),
    }


def attempt_dict(attempt: CallAttempt) -> dict[str, Any]:
    """One dial. The conversation's data is on the result, not here."""
    return {
        "id": attempt.id,
        "prospect_id": attempt.prospect_id,
        "campaign_id": attempt.campaign_id,
        "campaign_prospect_id": attempt.campaign_prospect_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status.value,
        "live": attempt.status.is_live,
        "final": attempt.status.is_final,
        "telephony_call_id": attempt.telephony_call_id,
        "telephony_provider": attempt.telephony_provider,
        "placement_started_at": iso(attempt.placement_started_at),
        "started_at": iso(attempt.started_at),
        "connected_at": iso(attempt.connected_at),
        "ended_at": iso(attempt.ended_at),
        "duration_seconds": attempt.duration_seconds,
        "failure_reason": attempt.failure_reason,
        # Phase 22: the correlation id on every process's log lines about
        # this call, so a workflow can quote it back when something is wrong.
        "trace_id": getattr(attempt, "trace_id", None),
        "created_at": iso(attempt.created_at),
        "updated_at": iso(attempt.updated_at),
    }


def callback_dict(callback: ScheduledCallback) -> dict[str, Any]:
    """A promise to call somebody back."""
    return {
        "id": callback.id,
        "prospect_id": callback.prospect_id,
        "campaign_id": callback.campaign_id,
        "call_attempt_id": callback.call_attempt_id,
        "campaign_prospect_id": callback.campaign_prospect_id,
        "scheduled_for": iso(callback.scheduled_for),
        "status": callback.status.value,
        "note": callback.note,
        "created_at": iso(callback.created_at),
        "updated_at": iso(callback.updated_at),
    }


def meeting_dict(meeting: Meeting) -> dict[str, Any]:
    """A meeting the agent booked."""
    return {
        "id": meeting.id,
        "prospect_id": meeting.prospect_id,
        "campaign_id": meeting.campaign_id,
        "call_attempt_id": meeting.call_attempt_id,
        "start_at": iso(meeting.start_at),
        "end_at": iso(meeting.end_at),
        "timezone": meeting.timezone,
        "provider": meeting.provider,
        "reference": meeting.reference,
        "status": meeting.status.value,
        "attendee_name": meeting.attendee_name,
        "attendee_email": meeting.attendee_email,
        "notes": meeting.notes,
        "created_at": iso(meeting.created_at),
    }


def transfer_dict(transfer: CallTransfer) -> dict[str, Any]:
    """A hand-off to a person, and how it ended."""
    return {
        "id": transfer.id,
        "call_attempt_id": transfer.call_attempt_id,
        "prospect_id": transfer.prospect_id,
        "telephony_call_id": transfer.telephony_call_id,
        "provider": transfer.provider,
        "to_number": transfer.to_number,
        "status": transfer.status.value,
        "reason": transfer.reason,
        "dial_call_id": transfer.dial_call_id,
        "duration_seconds": transfer.duration_seconds,
        "error": transfer.error,
        "requested_at": iso(transfer.requested_at),
        "completed_at": iso(transfer.completed_at),
    }


def result_dict(result: CallResult, *, include_transcript: bool = False) -> dict[str, Any]:
    """A call result: Phase 8's export shape, with the transcript on request.

    The transcript is the largest thing in a result and most workflows read
    the summary, so it is carried only when asked for; `transcript_included`
    says which, so a consumer never mistakes "omitted" for "empty".
    """
    data = result.to_dict()
    data["transcript_included"] = include_transcript
    if not include_transcript:
        data["transcript"] = None
    data["summary_text"] = result.summary.text
    return data


def event_dict(event: AutomationEvent, *, include_payload: bool = False) -> dict[str, Any]:
    """An outbox row, as `GET /api/v1/events` shows it."""
    data: dict[str, Any] = {
        "id": event.id,
        "event_id": event.event_key,
        "kind": event.kind,
        "state": event.state.value,
        "call_result_id": event.call_result_id,
        "call_attempt_id": event.call_attempt_id,
        "prospect_id": event.prospect_id,
        "campaign_id": event.campaign_id,
        "meeting_id": event.meeting_id,
        "callback_id": event.callback_id,
        "occurred_at": iso(event.occurred_at),
        "target_url": event.target_url,
        "attempts": event.attempts,
        "last_status": event.last_status,
        "last_error": event.last_error,
        "next_attempt_at": iso(event.next_attempt_at),
        "delivered_at": iso(event.delivered_at),
        "created_at": iso(event.created_at),
        "updated_at": iso(event.updated_at),
    }
    if include_payload:
        data["payload"] = event.payload
    return data


__all__ = [
    "attempt_dict",
    "callback_dict",
    "campaign_dict",
    "event_dict",
    "iso",
    "meeting_dict",
    "membership_dict",
    "prospect_dict",
    "result_dict",
    "transfer_dict",
]
