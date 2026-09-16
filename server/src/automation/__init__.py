"""The automation integration: an API n8n drives, and the events it is sent. Phase 17.

```
CSV / CRM / event
    ↓
n8n                       (asynchronous; never in the audio path)
    ↓  HTTP + API key
the automation API        `uv run automation.py`  — src/automation/api.py
    ↓  rows
the scheduler             `uv run campaign.py run` — places the call, as always
    ↓
the bot                   holds the conversation; knows nothing of any of this
    ↓  rows (call_results, meetings, callbacks)
the outbox deliverer      `uv run automation.py`  — src/automation/events.py
    ↓  POST, signed
n8n                       → CRM, calendar, notifications
```

| Module         | Owns                                                              |
|----------------|-------------------------------------------------------------------|
| `auth.py`      | API keys (constant-time), the outbound HMAC signature. Pure.      |
| `serialize.py` | Every row as JSON, one shape each, for the API and the events.    |
| `events.py`    | The outbox deliverer: claim, build, POST once, back off, close.   |
| `api.py`       | The FastAPI app: prospects, campaigns, calls, callbacks, results. |

Nothing here is imported by `bot.py`, `src/conversation/`, `src/campaigns/`
or `src/actions/`; `tests/test_automation.py` asserts it. The package reads
`src/campaigns/` and `src/config.py`; nothing reads it back.
"""

from __future__ import annotations

from .api import (
    API_PREFIX,
    API_VERSION,
    IDEMPOTENCY_HEADER,
    PING_PATH,
    REPLAYED_HEADER,
    ApiError,
    ApiSettings,
    CampaignAction,
    create_automation_app,
)
from .auth import (
    SIGNATURE_HEADER,
    extract_key,
    fingerprint,
    key_matches,
    sign,
    verify_signature,
)
from .events import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    EVENT_ID_HEADER,
    TIMESTAMP_HEADER,
    AiohttpSender,
    DeliveryReport,
    DeliveryTotals,
    EventDeliverer,
    SendResult,
    build_payload,
    encode_payload,
)
from .serialize import (
    attempt_dict,
    callback_dict,
    campaign_dict,
    event_dict,
    meeting_dict,
    membership_dict,
    prospect_dict,
    result_dict,
    transfer_dict,
)

__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "EVENT_ID_HEADER",
    "IDEMPOTENCY_HEADER",
    "PING_PATH",
    "REPLAYED_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "AiohttpSender",
    "ApiError",
    "ApiSettings",
    "CampaignAction",
    "DeliveryReport",
    "DeliveryTotals",
    "EventDeliverer",
    "SendResult",
    "attempt_dict",
    "build_payload",
    "callback_dict",
    "campaign_dict",
    "create_automation_app",
    "encode_payload",
    "event_dict",
    "extract_key",
    "fingerprint",
    "key_matches",
    "meeting_dict",
    "membership_dict",
    "prospect_dict",
    "result_dict",
    "sign",
    "transfer_dict",
    "verify_signature",
]
