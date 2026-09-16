"""From a `CallResult` to what a CRM is sent. Pure. Phase 15.

Phase 8 built the result to be CRM-ready — flat, typed, every enum with an
`UNKNOWN`, a summary composed from recorded fields so it invents nothing —
and this is the module that finally reads it for a CRM. It adds no facts:
every sentence in the body is a field on the row, restated, and a field the
row does not have is *named as unknown* rather than left out, because a CRM
user who sees no "Meeting" line cannot tell whether nobody asked or nobody
recorded it.

**One mapping for every CRM.** The output is `CallSync`: a `CrmContact` and a
`CallActivity` whose `fields` are neutral names. A provider chooses where
each lands; it never reads the `CallResult` itself. So adding Pipedrive or
Salesforce is a provider module and nothing here.

**The key.** `sync_key(result_id, attempt_id)` is `aiva{result}x{attempt}`:
short, alphanumeric so a CRM's search tokeniser keeps it in one piece, and
derived rather than random so two syncers — or one syncer twice — compute the
same token for the same call. It is written into the activity body, which is
what makes "did my create land" answerable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..campaigns.models import CallAttempt, Campaign, Prospect
from ..campaigns.results import CallbackOutcome, CallResult, Disposition, MeetingOutcome
from .base import CallActivity, CallOutcome, CallSync, CrmContact

#: What each disposition means to a CRM's call log. The conversation
#: dispositions are all "connected" — somebody answered and said something —
#: and the four unreached ones keep their own word.
_OUTCOMES = {
    Disposition.NO_ANSWER: CallOutcome.NO_ANSWER,
    Disposition.BUSY: CallOutcome.BUSY,
    Disposition.FAILED: CallOutcome.FAILED,
    Disposition.VOICEMAIL: CallOutcome.VOICEMAIL,
}

#: The disposition, in the words a person filing the call would use.
_DISPOSITION_WORDS = {
    Disposition.NO_ANSWER: "no answer",
    Disposition.BUSY: "busy",
    Disposition.FAILED: "could not be placed",
    Disposition.VOICEMAIL: "voicemail",
    Disposition.OPTED_OUT: "opted out (asked not to be called)",
    Disposition.DO_NOT_CALL: "do not call",
    Disposition.MEETING_BOOKED: "meeting booked",
    Disposition.TRANSFERRED: "transferred to a person",
    Disposition.CALLBACK_REQUESTED: "callback requested",
    Disposition.NOT_INTERESTED: "not interested",
    Disposition.QUALIFIED: "qualified",
    Disposition.UNQUALIFIED: "unqualified",
    Disposition.COMPLETED: "completed",
}


def sync_key(result_id: int, attempt_id: int) -> str:
    """The idempotency token for one result: `aiva12x34`. Alphanumeric on purpose."""
    return f"aiva{int(result_id)}x{int(attempt_id)}"


def build_call_sync(
    result: CallResult,
    prospect: Prospect,
    *,
    campaign: Campaign | None = None,
    attempt: CallAttempt | None = None,
    from_number: str | None = None,
) -> CallSync:
    """Compose what the CRM is sent for one finished call.

    Args:
        result: The stored result. Must have an `id`.
        prospect: The person, for the contact and the number dialled.
        campaign: Named in the title and the fields, when known.
        attempt: For the call's own timestamp, when known; otherwise the
            result's `created_at`.
        from_number: The caller ID presented, when known.

    Raises:
        ValueError: The result has no id, so no key can be derived.
    """
    if result.id is None:
        raise ValueError("a CallResult must be stored (have an id) before it can be synced")

    contact = CrmContact(
        first_name=prospect.first_name,
        last_name=prospect.last_name,
        phone=prospect.phone_normalized or None,
        email=(prospect.email or "").strip() or None,
        company=(prospect.company or "").strip() or None,
        job_title=(prospect.job_title or "").strip() or None,
    )

    zone = _zone(result.timezone)
    occurred_at = _occurred_at(result, attempt)
    key = sync_key(result.id, result.call_attempt_id)
    outcome = _OUTCOMES.get(result.disposition, CallOutcome.CONNECTED)
    word = _DISPOSITION_WORDS.get(result.disposition, result.disposition.value.lower())

    title = f"AI call — {word} — {contact.display_name}"
    if campaign is not None:
        title += f" ({campaign.name})"

    fields = _fields(result, prospect, campaign, zone)
    body = _body(result, contact, campaign, zone, key, word)

    activity = CallActivity(
        key=key,
        title=title,
        body=body,
        outcome=outcome,
        occurred_at=occurred_at,
        duration_seconds=result.duration_seconds,
        from_number=from_number,
        to_number=prospect.phone_normalized or None,
        fields=fields,
    )
    return CallSync(contact=contact, activity=activity)


# --- The pieces ------------------------------------------------------------------


def _fields(
    result: CallResult, prospect: Prospect, campaign: Campaign | None, zone: Any
) -> dict[str, str]:
    """Every structured fact as a string, by neutral name. Empty strings are omitted."""
    values: dict[str, str | None] = {
        "disposition": result.disposition.value,
        "call_status": result.call_status.value,
        "reached": "yes" if result.reached else "no",
        "qualification_status": result.qualification_status.value,
        "interest_level": result.interest_level.value,
        "buying_timeline": result.buying_timeline.value,
        "decision_role": result.decision_role.value,
        "next_action": result.next_action.value,
        "meeting_status": result.meeting_status.value,
        "meeting_start": _iso(result.meeting_start),
        "meeting_when": result.meeting_when,
        "callback_status": result.callback_status.value,
        "callback_scheduled_for": _iso(result.callback_scheduled_for),
        "callback_when": result.callback_when,
        "pain_points": _lines(result.pain_points),
        "objections": _lines(_objection_lines(result)),
        "questions": _lines(result.questions),
        "summary": result.summary.text,
        "existing_provider": result.existing_provider,
        "current_process": result.current_process,
        "impact": result.impact,
        "desired_outcome": result.desired_outcome,
        "failure_reason": result.failure_reason,
        "duration_seconds": str(result.duration_seconds) if result.duration_seconds is not None else None,
        "campaign": campaign.name if campaign is not None else None,
        "campaign_id": str(campaign.id) if campaign is not None else None,
        "call_attempt_id": str(result.call_attempt_id),
        "call_result_id": str(result.id),
        "prospect_id": str(prospect.id),
        "source": result.source.value,
        "schema_version": str(result.schema_version),
    }
    return {name: value for name, value in values.items() if value}


def _body(
    result: CallResult,
    contact: CrmContact,
    campaign: Campaign | None,
    zone: Any,
    key: str,
    word: str,
) -> str:
    """The account of the call, in headed sections. Restates fields; invents nothing."""
    sections: list[tuple[str, str]] = []

    outcome = f"{word.capitalize()}."
    if result.duration_seconds:
        outcome += f" {_duration(result.duration_seconds)}."
    if result.failure_reason:
        outcome += f" Reason: {result.failure_reason}"
    sections.append(("Outcome", outcome))

    sections.append(("Summary", result.summary.text or "(no summary was recorded)"))

    if result.reached:
        qualification = [
            f"Status: {_word(result.qualification_status.value)}",
            f"Interest: {_word(result.interest_level.value)}",
            f"Timeline: {_word(result.buying_timeline.value)}",
            f"Decision role: {_word(result.decision_role.value)}",
        ]
        sections.append(("Qualification", "\n".join(qualification)))
        sections.append(("Pain points", _bullets(result.pain_points, "none recorded")))
        sections.append(("Objections", _bullets(_objection_lines(result), "none recorded")))
        if result.questions:
            sections.append(("Questions they asked", _bullets(result.questions, "")))
        discovery = [
            f"{label}: {value}"
            for label, value in (
                ("Existing provider", result.existing_provider),
                ("Current process", result.current_process),
                ("Impact", result.impact),
                ("Desired outcome", result.desired_outcome),
            )
            if value
        ]
        if discovery:
            sections.append(("Discovery", "\n".join(discovery)))

    sections.append(("Meeting", _meeting(result, zone)))
    sections.append(("Callback", _callback(result, zone)))
    sections.append(("Next action", _word(result.next_action.value)))

    if result.tool_actions:
        taken = [
            f"{action.get('name', 'action')}: {'ok' if action.get('success') else 'failed'}"
            + (f" — {action['detail']}" if action.get("detail") else "")
            for action in result.tool_actions
            if isinstance(action, dict)
        ]
        if taken:
            sections.append(("Actions taken", _bullets(taken, "")))
    if result.notes:
        sections.append(("Notes", _bullets(result.notes, "")))
    if result.issues:
        sections.append(("Record issues", _bullets(result.issues, "")))

    footer = [f"Campaign: {campaign.name}" if campaign is not None else None]
    footer.append(f"Call attempt {result.call_attempt_id}, result {result.id}, written by the {result.source.value.lower()}")
    footer.append(f"ref {key}")

    text = "\n\n".join(f"{heading}\n{body}" for heading, body in sections)
    return text + "\n\n" + "\n".join(line for line in footer if line)


def _meeting(result: CallResult, zone: Any) -> str:
    status = result.meeting_status
    if status is MeetingOutcome.BOOKED and result.meeting_start is not None:
        when = _when(result.meeting_start, zone)
        reference = f" (ref {result.meeting_reference})" if result.meeting_reference else ""
        return f"Booked for {when}{reference}."
    if status is MeetingOutcome.AGREED:
        said = f' — they said "{result.meeting_when}"' if result.meeting_when else ""
        return f"Agreed, not yet booked{said}. A person must arrange it."
    if status is MeetingOutcome.PROPOSED:
        return "Proposed; no answer recorded."
    if status is MeetingOutcome.DECLINED:
        return "Declined."
    return "Not discussed, or not recorded."


def _callback(result: CallResult, zone: Any) -> str:
    status = result.callback_status
    if status is CallbackOutcome.SCHEDULED and result.callback_scheduled_for is not None:
        return f"Scheduled for {_when(result.callback_scheduled_for, zone)}."
    if status is CallbackOutcome.REQUESTED:
        said = f' — they said "{result.callback_when}"' if result.callback_when else ""
        return f"Requested, not yet scheduled{said}. A person must arrange it."
    if status is CallbackOutcome.PROPOSED:
        return "Proposed; no answer recorded."
    if status is CallbackOutcome.DECLINED:
        return "Declined."
    return "Not discussed, or not recorded."


def _objection_lines(result: CallResult) -> tuple[str, ...]:
    lines = []
    for objection in result.objections:
        if not isinstance(objection, dict):
            continue
        kind = _word(str(objection.get("kind") or "unknown"))
        detail = str(objection.get("detail") or "").strip()
        handled = objection.get("handled")
        line = kind
        if detail:
            line += f": {detail}"
        if handled is True:
            line += " (handled)"
        elif handled is False:
            line += " (not handled)"
        lines.append(line)
    return tuple(lines)


def _occurred_at(result: CallResult, attempt: CallAttempt | None) -> datetime:
    """When the call happened: the attempt's start, else the result's own time, else now."""
    if attempt is not None:
        for candidate in (attempt.started_at, attempt.connected_at, attempt.ended_at, attempt.created_at):
            if candidate is not None:
                return candidate if candidate.tzinfo else candidate.replace(tzinfo=UTC)
    if result.created_at is not None:
        return result.created_at if result.created_at.tzinfo else result.created_at.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _zone(name: str) -> Any:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _when(moment: datetime, zone: Any) -> str:
    local = moment.astimezone(zone) if moment.tzinfo else moment.replace(tzinfo=UTC).astimezone(zone)
    return local.strftime("%a %d %b %Y, %H:%M %Z")


def _duration(seconds: int) -> str:
    minutes, rest = divmod(int(seconds), 60)
    if minutes and rest:
        return f"{minutes} min {rest} s"
    if minutes:
        return f"{minutes} min"
    return f"{rest} s"


def _word(value: str) -> str:
    return value.replace("_", " ").lower()


def _bullets(items: Any, empty: str) -> str:
    lines = [f"- {item}" for item in items if str(item).strip()]
    return "\n".join(lines) if lines else empty


def _lines(items: Any) -> str | None:
    text = "\n".join(str(item).strip() for item in items if str(item).strip())
    return text or None


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).isoformat(timespec="minutes")


__all__ = ["build_call_sync", "sync_key"]
