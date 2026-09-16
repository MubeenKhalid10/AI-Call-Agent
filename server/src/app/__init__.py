"""The unified application. Phase 24.

One process, one login, one address: the browser application in `web/`,
served beside the two servers that already existed — the dashboard
(Phase 10/18/20: the read side, the login, the roles) mounted at
`/dashboard`, and the automation API (Phase 17/18/19: every write) mounted at
`/automation` — plus the few routes the application needed that neither
had: who am I, the safe view of the configuration, an on-demand health
check, and the knowledge base (list, upload, delete, search).

Nothing in the call path changed. The bot, the scheduler, the receiver and
the syncer are the processes they were; this one only reads and writes the
rows they share, exactly as `dashboard.py` and `automation.py` did, and it
can start the scheduler beside itself (`app.py --with-scheduler`) as a child
process rather than a second implementation.

`create_unified_app` is the factory; `app.py` is the command.
"""

from __future__ import annotations

from .server import APP_PATH, DEFAULT_APP_PORT, STATIC_PATH, create_unified_app

__all__ = ["APP_PATH", "DEFAULT_APP_PORT", "STATIC_PATH", "create_unified_app"]
