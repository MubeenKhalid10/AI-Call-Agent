"""A read-only view of what the calling system has done. Phase 10.

The first thing in this project with a screen. Everything before it reported
through a CLI or a log line, which is right for placing a call and wrong for
the question "how is the campaign going" — that one wants eight numbers next to
each other.

**It adds no data and no store.** Every figure comes from the same PostgreSQL
tables the dialer writes, through the same `CampaignStore`, using SQL
aggregates added alongside the queries they resemble. There is no analytics
database, no warehouse, no second copy of anything, and nothing here is
computed on a schedule — the page is a live read, so it cannot go stale or
disagree with `campaign.py`.

| Module | Owns |
|---|---|
| `stats.py` | What each number means, and the footnote that keeps it honest. Counts nothing itself |
| `page.py` | The document: one HTML file, no build step, no CDN |
| `web.py` | The routes. Every one of them a read |

**The boundary.** `dashboard/` reads `campaigns/` and nothing reads
`dashboard/` — it is a leaf, deliberately, so that a reporting change can never
affect a call. It does not import `bot.py`, the pipeline, the telephony
provider or the conversation layer, and it is served by its own process
(`dashboard.py`), not by the bot's runner: the runner's job is answering calls,
and a page that refreshes every fifteen seconds has no business sharing that
process.

**Two numbers here could mislead, so both carry their footnote** — *completed*
and *answered* overlap and are not the same, and an average duration means
nothing without the count it averages. `stats.py` explains both. And a missing
optional table is reported as unavailable, never as zero, because a zero next
to "Qualified prospects" is a claim and an absence is not.
"""

from __future__ import annotations

from .page import render_call_page, render_login, render_page
from .stats import Metric, ReportFilter, Snapshot, call_detail, collect, list_calls, search_people
from .web import (
    API_PATH,
    CALL_PAGE_PATH,
    CALLS_API_PATH,
    CAMPAIGNS_API_PATH,
    LOGIN_PATH,
    LOGOUT_PATH,
    ME_PATH,
    REFRESH_SECS,
    SEARCH_API_PATH,
    create_app,
)

__all__ = [
    "API_PATH",
    "REFRESH_SECS",
    "Metric",
    "Snapshot",
    "collect",
    "create_app",
    "render_page",
]
