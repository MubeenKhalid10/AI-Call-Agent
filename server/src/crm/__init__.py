"""CRM integration: every finished call's result, filed with an external CRM. Phase 15.

The public surface of the package. Import from here, not from the modules
underneath — `make_crm_provider` is the only thing that knows which CRM is
configured, and keeping that single point is what makes the CRM swappable.

    from src.crm import CrmSyncer, make_crm_provider

    provider = make_crm_provider(config.crm)
    syncer = CrmSyncer(store, provider, from_number=config.telephony.from_number)
    await syncer.run(poll_secs=config.crm.sync_poll_secs)

| Module        | Knows about                                                      |
|---------------|------------------------------------------------------------------|
| `base.py`     | The contract: `CrmProvider`, the neutral contact and activity, the errors. No vendor |
| `mapping.py`  | `CallResult` → what a CRM is sent. Pure; no HTTP, no vendor      |
| `hubspot.py`  | HubSpot's endpoints, properties and error bodies. The only vendor file |
| `sync.py`     | When and again: claiming rows, retries, idempotency, the outcome on the row |

**Adding a CRM** is three edits, none of them in `sync.py` or `mapping.py`:

1. Write `src/crm/<name>.py` with a `CrmProvider` subclass — six methods.
2. Add its name and its credential variables to `CrmConfig` in `config.py`.
3. Add a branch to `make_crm_provider` below.

**The boundary.** `src/crm/` reads `src/campaigns/` (the rows) and
`src/reliability/` (retries, logs) and nothing reads it back: not the bot, not
the conversation, not the sink, not the worker. The sync runs from
`campaign.py crm-sync` in a process of its own. `tests/test_crm.py` asserts
the direction of that arrow.
"""

from __future__ import annotations

from ..config import ConfigError, CrmConfig
from .base import (
    CallActivity,
    CallOutcome,
    CallSync,
    CrmAuthError,
    CrmContact,
    CrmError,
    CrmProvider,
    CrmRejectedError,
    CrmUnavailableError,
    SyncReceipt,
)
from .mapping import build_call_sync, sync_key
from .sync import WRITE_POLICY, CrmSyncer, SyncReport, SyncTotals

__all__ = [
    "WRITE_POLICY",
    "CallActivity",
    "CallOutcome",
    "CallSync",
    "CrmAuthError",
    "CrmContact",
    "CrmError",
    "CrmProvider",
    "CrmRejectedError",
    "CrmSyncer",
    "CrmUnavailableError",
    "SyncReceipt",
    "SyncReport",
    "SyncTotals",
    "build_call_sync",
    "make_crm_provider",
    "sync_key",
]


def make_crm_provider(config: CrmConfig, *, timeout_secs: float | None = None) -> CrmProvider:
    """Build the configured CRM's provider.

    Args:
        config: Which CRM, and its credentials.
        timeout_secs: Ceiling on one HTTP request. None uses the provider's own.

    Raises:
        ConfigError: No CRM is selected, or its credentials are missing.
    """
    if not config.enabled:
        raise ConfigError("No CRM is configured. Set CRM_PROVIDER=hubspot and HUBSPOT_ACCESS_TOKEN in server/.env.")
    config.require_credentials()
    timeout = {"timeout_secs": timeout_secs} if timeout_secs is not None else {}

    if config.provider == "hubspot":
        from .hubspot import HubSpotProvider

        return HubSpotProvider(
            config.hubspot_access_token or "",
            api_base=config.hubspot_api_base or "https://api.hubapi.com",
            custom_properties=config.custom_properties,
            **timeout,
        )

    raise ConfigError(f"No CRM provider is implemented for CRM_PROVIDER={config.provider!r}.")
