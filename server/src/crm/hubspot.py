"""HubSpot: the only file in this project that knows HubSpot exists. Phase 15.

Everything vendor-specific is here — the endpoints, the property names, the
disposition ids, the error bodies. The syncer talks to `CrmProvider` and never
imports this module; `make_crm_provider` builds it when `CRM_PROVIDER=hubspot`.

**What a call becomes.** A *contact* (`/crm/v3/objects/contacts`), matched by
email or phone and created if absent, and a *call engagement*
(`/crm/v3/objects/calls`) associated to it with the HubSpot-defined
call→contact association (type id 194). The engagement's standard properties
carry the time, the title, the body, the duration (milliseconds), the
direction, the status and the disposition — the latter one of HubSpot's six
built-in disposition ids, which are the same in every portal. The structured
facts additionally land on the contact as custom `ai_*` properties, created
on first run by `ensure_schema`; a token without the properties scope loses
those and keeps everything else.

**Authentication** is a private-app access token as a bearer header. It is
never logged; `describe()` shows its tail.

**Written against the documented API, not against a live portal.** As with
SignalWire, Cal.com and the webhooks before it, the request shapes are pinned
by `tests/test_crm.py` against a stub HTTP session and the first live sync is
the first real test. The one behaviour that is documented and worth knowing:
HubSpot's search index lags a create by "a few moments", so `find_activity`
may not see an engagement created a second ago — which is why the syncer's
first retry waits a minute rather than a second.

**Why `aiohttp` and not the `hubspot-api-client` SDK.** Same reason as every
other vendor here: a handful of requests, an HTTP library already in the tree,
and no synchronous SDK to block an event loop with.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import aiohttp
from loguru import logger

from .base import (
    CallActivity,
    CallOutcome,
    CrmAuthError,
    CrmContact,
    CrmProvider,
    CrmRejectedError,
    CrmUnavailableError,
)

DEFAULT_API_BASE = "https://api.hubapi.com"

#: HubSpot's built-in call dispositions. Fixed ids, the same in every portal.
DISPOSITIONS = {
    CallOutcome.CONNECTED: "f240bbac-87c9-4f6e-bf70-924b57d47db7",
    CallOutcome.NO_ANSWER: "73a0d17f-1163-4015-bdd5-ec830791da20",
    CallOutcome.BUSY: "9d9162e7-6cf3-4944-bf63-4dff82258764",
    CallOutcome.VOICEMAIL: "b2cf5968-551e-4856-9783-52b3da59a7d0",
    # No built-in disposition means "failed"; the status says it instead.
}

#: `hs_call_status`, from the outcome.
_CALL_STATUSES = {
    CallOutcome.CONNECTED: "COMPLETED",
    CallOutcome.NO_ANSWER: "NO_ANSWER",
    CallOutcome.BUSY: "BUSY",
    CallOutcome.VOICEMAIL: "COMPLETED",
    CallOutcome.FAILED: "FAILED",
}

#: The HubSpot-defined association from a call to a contact.
CALL_TO_CONTACT = 194

#: The custom contact properties this integration files the structured facts
#: in: (name, label, type, fieldType, the neutral field it is filled from).
#: Created by `ensure_schema` under the standard contact information group.
CONTACT_PROPERTIES: tuple[tuple[str, str, str, str, str], ...] = (
    ("ai_last_call_at", "AI call: last call at", "datetime", "date", "occurred_at"),
    ("ai_last_call_disposition", "AI call: last disposition", "string", "text", "disposition"),
    ("ai_qualification_status", "AI call: qualification", "string", "text", "qualification_status"),
    ("ai_interest_level", "AI call: interest", "string", "text", "interest_level"),
    ("ai_next_action", "AI call: next action", "string", "text", "next_action"),
    ("ai_meeting_status", "AI call: meeting", "string", "text", "meeting_status"),
    ("ai_meeting_at", "AI call: meeting at", "datetime", "date", "meeting_start"),
    ("ai_callback_status", "AI call: callback", "string", "text", "callback_status"),
    ("ai_callback_at", "AI call: callback at", "datetime", "date", "callback_scheduled_for"),
    ("ai_pain_points", "AI call: pain points", "string", "textarea", "pain_points"),
    ("ai_objections", "AI call: objections", "string", "textarea", "objections"),
    ("ai_last_call_summary", "AI call: last summary", "string", "textarea", "summary"),
    ("ai_campaign", "AI call: campaign", "string", "text", "campaign"),
)
_CONTACT_GROUP = "contactinformation"

#: HubSpot says which contact exists when a create collides on email:
#: "Contact already exists. Existing ID: 12345".
_EXISTING_ID = re.compile(r"Existing ID:\s*(\d+)")


class HubSpotProvider(CrmProvider):
    """Files calls in HubSpot through its CRM v3 API."""

    name = "hubspot"

    def __init__(
        self,
        access_token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        session: aiohttp.ClientSession | None = None,
        timeout_secs: float = 20.0,
        custom_properties: bool = True,
    ) -> None:
        """Create the provider.

        Args:
            access_token: A private-app access token (`pat-…`). Never logged.
            api_base: The API host. Overridable for a test double.
            session: An existing HTTP session; the tests pass a stub.
            timeout_secs: Ceiling on one request. A timed-out write is
                ambiguous, and the syncer treats it so.
            custom_properties: Whether to file the structured facts in `ai_*`
                contact properties (created by `ensure_schema`). False writes
                only standard properties and never touches the properties API.
        """
        self._token = access_token
        self._api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout_secs = timeout_secs
        self._custom = custom_properties
        self._schema_ready = False

    def describe(self) -> str:
        """One line for the startup log. Shows the token's tail, never the token."""
        tail = self._token[-4:] if len(self._token) >= 8 else "…"
        extras = "custom properties" if self._custom else "standard properties only"
        return f"hubspot (token …{tail}, {extras})"

    @property
    def custom_properties(self) -> bool:
        """Whether the structured facts are being filed as contact properties."""
        return self._custom

    # --- Schema ----------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create the `ai_*` contact properties that are missing. Idempotent.

        Reads each property first and creates only the absent ones, so a
        portal that already has them costs one GET each and no writes. A
        refusal (no `crm.schemas.contacts.write` scope, say) raises; the
        syncer then turns custom properties off for the run and files the
        call with standard properties only, which is still the whole record —
        the body carries every fact.
        """
        if not self._custom or self._schema_ready:
            return
        for name, label, kind, field_type, _source in CONTACT_PROPERTIES:
            path = f"/crm/v3/properties/contacts/{name}"
            try:
                await self._request("GET", path)
                continue
            except CrmRejectedError as exc:
                if exc.status != 404:
                    raise
            await self._request(
                "POST",
                "/crm/v3/properties/contacts",
                json={
                    "name": name,
                    "label": label,
                    "type": kind,
                    "fieldType": field_type,
                    "groupName": _CONTACT_GROUP,
                    "description": "Written by the AI calling agent after each call.",
                },
            )
            logger.info(f"CRM | hubspot | created contact property {name}")
        self._schema_ready = True

    def disable_custom_properties(self) -> None:
        """Stop filing the `ai_*` properties for the rest of this run."""
        self._custom = False

    # --- Contacts ----------------------------------------------------------------

    async def find_contact(self, contact: CrmContact) -> str | None:
        """Search by email, then by phone. Never by name."""
        if contact.email:
            found = await self._search(
                "contacts", "email", "EQ", contact.email.lower(), properties=["email", "phone"]
            )
            for entry in found:
                if _string(_prop(entry, "email")).lower() == contact.email.lower():
                    return str(entry["id"])
        if contact.phone:
            # HubSpot matches a phone search on the area code and local number
            # only, so the candidates are checked against the full number.
            found = await self._search(
                "contacts", "phone", "EQ", contact.phone, properties=["email", "phone", "mobilephone"]
            )
            wanted = _digits(contact.phone)
            for entry in found:
                for field_name in ("phone", "mobilephone"):
                    if _same_number(_digits(_prop(entry, field_name)), wanted):
                        return str(entry["id"])
        return None

    async def create_contact(self, contact: CrmContact) -> str:
        """Create the contact, or return the id HubSpot says already has that email."""
        properties = {
            name: value
            for name, value in (
                ("firstname", contact.first_name),
                ("lastname", contact.last_name),
                ("phone", contact.phone),
                ("email", contact.email),
                ("company", contact.company),
                ("jobtitle", contact.job_title),
            )
            if value
        }
        try:
            data = await self._request("POST", "/crm/v3/objects/contacts", json={"properties": properties})
        except CrmRejectedError as exc:
            if exc.status == 409:
                match = _EXISTING_ID.search(str(exc))
                if match:
                    return match.group(1)
            raise
        return str(data["id"])

    async def update_contact(self, contact_id: str, contact: CrmContact, activity: CallActivity) -> None:
        """Write the latest call's facts onto the `ai_*` properties. Nothing else."""
        if not self._custom:
            return
        properties: dict[str, str] = {}
        for name, _label, kind, _field_type, source in CONTACT_PROPERTIES:
            if source == "occurred_at":
                value: str | None = _millis(activity.occurred_at.timestamp())
            else:
                raw = activity.fields.get(source)
                if raw is None:
                    continue
                value = _millis_from_iso(raw) if kind == "datetime" else raw
            if value:
                properties[name] = value
        if properties:
            await self._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": properties})

    # --- Calls -------------------------------------------------------------------

    async def find_activity(self, key: str) -> str | None:
        """The engagement whose body carries `key`, or None."""
        found = await self._search("calls", "hs_call_body", "CONTAINS_TOKEN", key, properties=["hs_call_title"])
        return str(found[0]["id"]) if found else None

    async def create_activity(self, contact_id: str, activity: CallActivity) -> str:
        """Log the call and associate it with the contact in one request."""
        data = await self._request(
            "POST",
            "/crm/v3/objects/calls",
            json={
                "properties": self._call_properties(activity),
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": CALL_TO_CONTACT}
                        ],
                    }
                ],
            },
        )
        return str(data["id"])

    async def update_activity(self, activity_id: str, contact_id: str, activity: CallActivity) -> None:
        """Rewrite the engagement's properties. The association stands."""
        await self._request(
            "PATCH", f"/crm/v3/objects/calls/{activity_id}", json={"properties": self._call_properties(activity)}
        )

    def _call_properties(self, activity: CallActivity) -> dict[str, str]:
        properties = {
            "hs_timestamp": _millis(activity.occurred_at.timestamp()),
            "hs_call_title": activity.title[:200],
            "hs_call_body": f"{activity.body}\n\nref {activity.key}",
            "hs_call_direction": "OUTBOUND",
            "hs_call_status": _CALL_STATUSES[activity.outcome],
        }
        disposition = DISPOSITIONS.get(activity.outcome)
        if disposition:
            properties["hs_call_disposition"] = disposition
        if activity.duration_seconds is not None:
            properties["hs_call_duration"] = str(int(activity.duration_seconds) * 1000)
        if activity.from_number:
            properties["hs_call_from_number"] = activity.from_number
        if activity.to_number:
            properties["hs_call_to_number"] = activity.to_number
        return properties

    # --- Health --------------------------------------------------------------------

    async def check_credentials(self) -> str:
        """Read one contact: the cheapest authenticated call. Writes nothing."""
        data = await self._request("GET", "/crm/v3/objects/contacts", params={"limit": "1"})
        count = len(data.get("results") or [])
        return "contacts readable" + ("" if count else " (portal has no contacts yet)")

    async def close(self) -> None:
        """Close the HTTP session, if this provider created one."""
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    # --- HTTP ------------------------------------------------------------------------

    async def _search(
        self, object_type: str, property_name: str, operator: str, value: str, *, properties: list[str]
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "POST",
            f"/crm/v3/objects/{object_type}/search",
            json={
                "filterGroups": [{"filters": [{"propertyName": property_name, "operator": operator, "value": value}]}],
                "properties": properties,
                "limit": 10,
            },
        )
        results = data.get("results")
        return [entry for entry in results if isinstance(entry, dict) and "id" in entry] if isinstance(results, list) else []

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One authenticated request, decoded.

        Raises:
            CrmAuthError: 401 or 403 — the token, or a scope it lacks.
            CrmUnavailableError: 429 (with HubSpot's `Retry-After`), 5xx, a
                timeout, or no connection. Retryable; ambiguous on a write.
            CrmRejectedError: Any other 4xx, with HubSpot's own message and
                the status on the exception.
        """
        session = await self._http()
        url = f"{self._api_base}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=self._timeout_secs)
        try:
            async with session.request(method, url, headers=headers, json=json, params=params, timeout=timeout) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - a non-JSON body is itself the finding
                    body = {"message": (await response.text())[:400]}
                if 200 <= status < 300:
                    return body if isinstance(body, dict) else {}
                message = _error_text(body)
                if status in (401, 403):
                    raise CrmAuthError(
                        f"HubSpot rejected the token (HTTP {status}) for {method} {path}: {message} "
                        f"— check HUBSPOT_ACCESS_TOKEN and the private app's scopes"
                    )
                if status == 429:
                    raise CrmUnavailableError(
                        f"HubSpot rate limit (HTTP 429) for {method} {path}: {message}",
                        retry_after_secs=_retry_after(response.headers.get("Retry-After")),
                    )
                if status >= 500:
                    raise CrmUnavailableError(f"HubSpot returned {status} for {method} {path}: {message}")
                raise CrmRejectedError(f"HubSpot refused {method} {path} (HTTP {status}): {message}", status=status)
        except TimeoutError as exc:
            raise CrmUnavailableError(
                f"HubSpot did not answer within {self._timeout_secs:g}s for {method} {path}; "
                f"whether it acted on the request is unknown"
            ) from exc
        except aiohttp.ClientError as exc:
            raise CrmUnavailableError(f"Could not reach HubSpot ({exc.__class__.__name__}: {exc})") from exc


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        parts = [str(body.get("message") or "").strip()]
        category = body.get("category")
        if category:
            parts.append(f"[{category}]")
        errors = body.get("errors")
        if isinstance(errors, list):
            parts.extend(str(e.get("message") or "") for e in errors if isinstance(e, dict))
        return " ".join(p for p in parts if p)[:400]
    return ""


def _retry_after(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _prop(entry: dict[str, Any], name: str) -> Any:
    properties = entry.get("properties")
    return properties.get(name) if isinstance(properties, dict) else None


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", _string(value))


def _same_number(candidate: str, wanted: str) -> bool:
    """Whether two digit strings are the same phone number.

    A CRM often holds a number the way a person wrote it — `0300 1234567` —
    while the campaign dialled its E.164 form, `+923001234567`. The trunk
    prefix (the leading zero) stands in for the country code, so both are
    stripped of leading zeros and the shorter must be the tail of the longer,
    with at least nine digits in common so an area code alone never matches.
    """
    candidate, wanted = candidate.lstrip("0"), wanted.lstrip("0")
    if not candidate or not wanted:
        return False
    if candidate == wanted:
        return True
    shorter, longer = sorted((candidate, wanted), key=len)
    return len(shorter) >= 9 and longer.endswith(shorter)


def _millis(seconds: float) -> str:
    return str(int(round(seconds * 1000)))


def _millis_from_iso(value: str) -> str | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _millis(moment.timestamp())


__all__ = ["CALL_TO_CONTACT", "CONTACT_PROPERTIES", "DISPOSITIONS", "HubSpotProvider"]
