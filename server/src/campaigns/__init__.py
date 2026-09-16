"""Prospects, campaigns, and the queue that decides who to call next.

The public surface of the package. Import from here rather than from the modules
underneath, so that the internal split can change without every caller moving.

    from src.campaigns import CampaignService, CampaignStore

    store = await CampaignStore.connect(config.database_url)
    service = CampaignService(store, default_region=config.default_phone_region)
    queued = await service.next_call(campaign_id)

**How the pieces divide, and why.**

| Module         | Knows about                                        |
|----------------|----------------------------------------------------|
| `models.py`    | The four entities and their states. No I/O.        |
| `phone.py`     | Turning written numbers into dialable ones. Pure.  |
| `csv_import.py`| Turning a spreadsheet into rows. Pure, no database.|
| `store.py`     | SQL. No policy beyond what a transaction needs.    |
| `service.py`   | The rules: who may be called, what an outcome means|
| `dialer.py`    | The one place campaigns meet the telephony provider|
| `results.py`   | What a finished call produced, validated. Pure.    |
| `recovery.py`  | What a restart does about calls that were in flight |
| `worker.py`    | The scheduler: the loop that places calls unattended (Phase 13) |
| `webhooks.py`  | The carrier's pushed call events: verified, decoded, applied once (Phase 14) |
| `coordination.py` | What several workers share: lock keys, identity, heartbeat records, queue depth, the transient-failure rule (Phase 21) |

Two of those are worth stating as rules rather than a table. `phone.py` and
`csv_import.py` touch nothing external, which is why the import logic can be
tested exhaustively without a database or a phone. And only the three modules
that *join* the two worlds — `dialer.py`, `recovery.py` and `webhooks.py` —
import `src.telephony`: campaign logic and the carrier are kept apart on
purpose, so that swapping Twilio for SignalWire — or for anything else —
remains a change in one package that this one never notices.
"""

from __future__ import annotations

from .briefing import (
    Briefing,
    CampaignConversationSink,
    CampaignProspectSource,
    open_briefing,
)
from .coordination import (
    QueueDepth,
    WorkerRecord,
    WorkerSummary,
    make_worker_id,
    transient_failure,
)
from .csv_import import (
    HEADER_ALIASES,
    KNOWN_FIELDS,
    REQUIRED_FIELDS,
    ColumnMapping,
    ParsedRow,
    ParseReport,
    map_headers,
    parse_csv,
)
from .dialer import CampaignDialer, DialResult, map_call_status
from .models import (
    ApiRequestRecord,
    AutomationEvent,
    AutomationEventState,
    CallAttempt,
    CallAttemptStatus,
    CallbackStatus,
    CallTransfer,
    Campaign,
    CampaignProspect,
    CampaignStatus,
    CrmSyncRecord,
    CrmSyncState,
    Meeting,
    MeetingStatus,
    MembershipStatus,
    Prospect,
    ProspectStatus,
    QueuedCall,
    ScheduledCallback,
    TransferStatus,
    WebhookDelivery,
    WebhookOutcome,
)
from .phone import NormalizedPhone, PhoneQuality, normalize_phone, same_number
from .recovery import AttemptRecovery, RecoveryReport
from .results import (
    SCHEMA_VERSION,
    CallbackOutcome,
    CallResult,
    CallResultValidationError,
    CallSummary,
    Disposition,
    MeetingOutcome,
    ResultSource,
    attempt_status_for,
    build_carrier_result,
    build_conversation_result,
    derive_disposition,
    extract_questions,
    status_for_final_state,
    validate_call_result,
)
from .service import CallabilityCheck, CampaignService, ImportOutcome
from .store import (
    API_REQUESTS_TABLE,
    ATTEMPTS_TABLE,
    AUTOMATION_EVENT_KINDS,
    AUTOMATION_EVENTS_TABLE,
    CALLBACKS_TABLE,
    CAMPAIGNS_TABLE,
    CRM_SYNC_TABLE,
    MEETINGS_TABLE,
    MEMBERSHIPS_TABLE,
    PROGRESS_KEYS,
    PROSPECTS_TABLE,
    RESULTS_TABLE,
    TRANSFERS_TABLE,
    WEBHOOKS_TABLE,
    CampaignCounts,
    CampaignStore,
    CampaignStoreError,
    DuplicateProspectError,
    MeetingConflictError,
    QueueOutlook,
    campaign_concurrency,
)
from .webhooks import (
    WebhookMetrics,
    WebhookProcessor,
    WebhookReceipt,
    build_webhook_processor,
    create_webhook_app,
    create_webhook_router,
    install_webhook_receiver,
)
from .worker import CampaignWorker, TickReport, WorkerMetrics, install_signal_handlers

__all__ = [
    "API_REQUESTS_TABLE",
    "AUTOMATION_EVENTS_TABLE",
    "AUTOMATION_EVENT_KINDS",
    "ApiRequestRecord",
    "AutomationEvent",
    "AutomationEventState",
    "ATTEMPTS_TABLE",
    "CALLBACKS_TABLE",
    "CAMPAIGNS_TABLE",
    "MEETINGS_TABLE",
    "RESULTS_TABLE",
    "SCHEMA_VERSION",
    "AttemptRecovery",
    "CampaignWorker",
    "TickReport",
    "WorkerMetrics",
    "install_signal_handlers",
    "WEBHOOKS_TABLE",
    "CRM_SYNC_TABLE",
    "TRANSFERS_TABLE",
    "CallTransfer",
    "TransferStatus",
    "MeetingConflictError",
    "CrmSyncRecord",
    "CrmSyncState",
    "WebhookDelivery",
    "WebhookMetrics",
    "WebhookOutcome",
    "WebhookProcessor",
    "WebhookReceipt",
    "build_webhook_processor",
    "create_webhook_app",
    "create_webhook_router",
    "install_webhook_receiver",
    "map_call_status",
    "QueueOutlook",
    "CallResult",
    "CallResultValidationError",
    "CallSummary",
    "RecoveryReport",
    "CallbackOutcome",
    "Disposition",
    "MeetingOutcome",
    "ResultSource",
    "attempt_status_for",
    "build_carrier_result",
    "build_conversation_result",
    "derive_disposition",
    "extract_questions",
    "status_for_final_state",
    "validate_call_result",
    "CallbackStatus",
    "Meeting",
    "MeetingStatus",
    "ScheduledCallback",
    "Briefing",
    "CampaignConversationSink",
    "CampaignProspectSource",
    "open_briefing",
    "HEADER_ALIASES",
    "KNOWN_FIELDS",
    "MEMBERSHIPS_TABLE",
    "PROSPECTS_TABLE",
    "REQUIRED_FIELDS",
    "CallAttempt",
    "CallAttemptStatus",
    "CallabilityCheck",
    "Campaign",
    "CampaignCounts",
    "CampaignDialer",
    "CampaignProspect",
    "CampaignService",
    "CampaignStatus",
    "CampaignStore",
    "CampaignStoreError",
    "PROGRESS_KEYS",
    "campaign_concurrency",
    "ColumnMapping",
    "DialResult",
    "DuplicateProspectError",
    "ImportOutcome",
    "MembershipStatus",
    "NormalizedPhone",
    "ParseReport",
    "ParsedRow",
    "PhoneQuality",
    "Prospect",
    "ProspectStatus",
    "QueuedCall",
    "map_headers",
    "normalize_phone",
    "parse_csv",
    "same_number",
]
