"""A campaign's progress as one dictionary, for a page that updates as calls end. Phase 25.

Reads only: the membership and attempt counters the store returns in two
statements (`CampaignStore.campaign_progress`), the result-derived columns
the dashboard has shown since Phase 8 (`campaign_result_counts`), and the
campaign row. Nothing here is cached and nothing is kept in memory: every
figure comes from PostgreSQL each time, which is what makes the same numbers
appear whichever process — the application's engine, a `campaign.py run`
beside it — placed the calls.

`cancelled` is not an attempt status. A call is never cancelled by this
system once placed (the carrier's own cancel is recorded as `FAILED` with
the carrier's reason); what a stop cancels is the *queue* — the contacts a
completed or cancelled campaign never reached — and that is what the
counter says.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import Campaign, CampaignStatus
from .store import CampaignStoreError

FINISHED = frozenset({CampaignStatus.COMPLETED, CampaignStatus.CANCELLED})


async def campaign_progress(store: Any, campaign: Campaign) -> dict[str, Any]:
    """Every counter a live progress view needs for one campaign.

    Args:
        store: A `CampaignStore` (or the checks' double).
        campaign: The campaign row, freshly read by the caller.
    """
    counters = await store.campaign_progress(campaign.id)
    # `None` for the result-derived figures only when there is no result
    # table to read (a store that predates Phase 8, or the checks' double);
    # a campaign with no results yet has zero qualified leads, not "unknown".
    results: dict[str, Any] | None = None
    reader = getattr(store, "campaign_result_counts", None)
    if reader is not None:
        try:
            found = await reader(campaign_id=campaign.id)
        except CampaignStoreError:
            found = None
        if found is not None:
            results = (found.get(campaign.id, {}) or {}) if isinstance(found, dict) else {}
    qualified = results.get("qualified", 0) if results is not None else None
    meetings = results.get("meetings", 0) if results is not None else None
    finished = campaign.status in FINISHED
    contacts = counters["contacts"]
    done = counters["members_completed"] + counters["exhausted"] + counters["skipped"]
    return {
        "campaign_id": campaign.id,
        "name": campaign.name,
        "status": campaign.status.value,
        "dialable": campaign.status.is_dialable,
        "finished": finished,
        **counters,
        "qualified": qualified,
        "meetings": meetings,
        "cancelled": counters["pending"] if finished else 0,
        "remaining": 0 if finished else counters["pending"] + counters["in_progress"],
        "done": done,
        "progress_pct": round(done * 100 / contacts) if contacts else None,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


__all__ = ["FINISHED", "campaign_progress"]
