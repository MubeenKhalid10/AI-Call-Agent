# Outbound Calling Compliance — Ai-Voice-Agent (Phase 19)

This document says which compliance and safety **controls the software
implements**, which **policies the operator must configure**, and how each
decision is recorded. It is written so that the person answering "why did
this number ring, and why did that one not" can point at a setting and a
row.

> **This is not legal advice, and running this software does not make a
> deployment compliant with anything.** Telemarketing, robocall, consent,
> do-not-call, recording and AI-disclosure rules differ by country, state
> and industry, and change. The software applies the rules you configure
> and records that it did. Deciding what those rules must be for the
> numbers you dial — including whether you may dial them at all — is the
> operator's responsibility, and the place to get that answer is a lawyer
> who knows your jurisdiction, not this file.

---

## 1. Controls the software implements

| Control | Where | What it does |
|---|---|---|
| **Do-not-call list** | `dnc_numbers` table; `campaign.py dnc*`; `POST /api/v1/dnc` | One active row per normalised number, with source, reason, who and when. Never deleted: a removal is a `revoked_at` stamp with a name on it. |
| **Centralised pre-call gate** | `src/compliance/gate.py` (`ComplianceGate`), run by the dialer before **every** outbound placement | In order: the list → the prospect's `DO_NOT_CALL` status → the campaign's rules under the policy for that number (dialable number, ACTIVE campaign, open membership, attempt ceiling, retry wait, no live call) → the calling window in the prospect's own timezone. Never raises. |
| **Queue-level exclusion** | `CampaignStore._reserve` SQL | A listed number, or a `DO_NOT_CALL` prospect, is never handed out by the reservation transaction, independently of the gate. |
| **Immediate opt-out** | the conversation's detector and `do_not_call` tool → `CampaignConversationSink.on_do_not_call` | The moment a request is recognised mid-call: the prospect's status is written (closing every open membership and pending callback), the number goes on the list (`source: verbal`, with the campaign and attempt ids), and the call moves to its goodbye. An anonymous caller with a number is listed by number. |
| **Import and create screening** | `CampaignService.create_prospect`, `import_csv`, `add_prospects` | A number on the list is marked `DO_NOT_CALL` the moment its prospect row exists, and a `DO_NOT_CALL` prospect never joins a campaign. |
| **Calling windows** | `CALLING_HOURS` / `CALLING_DAYS` / `CALLING_TIMEZONE` (Phase 9), per campaign, per jurisdiction | Enforced in the prospect's own timezone when their record carries one (`custom_data.timezone`), else the policy's. A closed window defers the reservation unspent until it opens. |
| **Attempt ceiling** | `CAMPAIGN_MAX_ATTEMPTS`, per campaign, per jurisdiction | Applied in the reservation SQL (the campaign's figure) and at the gate (the jurisdiction's, which may be lower). A callback the person asked for may waive the ceiling; nothing waives the list. |
| **Retry delays** | `CAMPAIGN_RETRY_MINUTES`, `COMPLIANCE_RETRY_MINUTES_NO_ANSWER` / `_BUSY` / `_VOICEMAIL`, per campaign, per jurisdiction | The wait before an unreached call is tried again, per outcome. `FAILED` is never retried (Phase 5). |
| **AI disclosure** | `COMPLIANCE_AI_DISCLOSURE`, `COMPLIANCE_AI_DISCLOSURE_REQUIRED`, per campaign, per jurisdiction | When required, the agent's **first sentence** must include the configured text; the instruction is on the audit log. When not required, the agent still answers honestly the moment anyone asks whether it is human (Phase 6, under every setting). |
| **Recording disclosure** | `COMPLIANCE_RECORDING_ENABLED`, `COMPLIANCE_RECORDING_DISCLOSURE`, `COMPLIANCE_RECORDING_DISCLOSURE_REQUIRED` | When required, the agent's first sentence includes the configured text. **This software does not record audio itself**; the flag exists because the operator's carrier or proxy may. |
| **Campaign-level settings** | `campaigns.configuration["compliance"]`; `campaign.py compliance <campaign> --set …`; `PUT /api/v1/campaigns/{id}/compliance` | Any of the keys in §3, validated before they are written. |
| **Jurisdiction rules** | `COMPLIANCE_JURISDICTIONS` (JSON: region code → the same keys) | Applied by the **country code of the number** (libphonenumber), *after* the campaign's settings, so a campaign can never loosen a country's rule. |
| **Clear dispositions** | `Disposition` (Phase 8, extended) | `OPTED_OUT` (they asked, on this call), `DO_NOT_CALL` (the list refused the dial, or a status with no conversation behind it), `NO_ANSWER`, `BUSY`, `VOICEMAIL`, `FAILED`, `COMPLETED`, plus the conversation outcomes. A dial the gate refused for the list closes its attempt as `DO_NOT_CALL`, not as a `FAILED` with a sentence in it. |
| **Audit of every decision** | `audit_log` (Phase 18); `campaign.py compliance-log`; `GET /api/v1/audit?action=compliance.` | `compliance.allowed` and `compliance.blocked` per gate decision with the policy, the region and the code; `compliance.dnc_added` / `dnc_removed` / `dnc_imported` / `dnc_applied`; `compliance.disclosure` per briefed call; `campaign.compliance_updated`; `prospect.do_not_call`. |

Nothing from earlier phases was removed: Phase 5's status, Phase 9's window, concurrency and pacing guards, Phase 13's deferral and the reservation's own SQL rules all still run. The gate is where the pre-dial rules are called *from* now, with the policy's figures instead of the environment's, and with the list in front of them.

## 2. What the operator must decide and configure

The software ships with **no jurisdiction rules**, **no disclosure required**, the calling window and ceiling Phase 9 and Phase 5 shipped with, and an **empty do-not-call list**. Every one of the following is a decision the operator makes, and for most of them the answer depends on where the people being called are:

1. **Whether you may call these numbers at all.** Consent, existing-relationship and business-to-business rules are outside anything this software can check. Screen your lists before they are imported.
2. **Which external do-not-call registries apply**, how often to screen against them, and how to load the result: `campaign.py dnc-import <file> --source registry` takes one number per line or a CSV with a phone column. Nothing here contacts a registry.
3. **The calling window per country or state**, and whether the prospect's own timezone is known (`custom_data.timezone` on import). A window in the *wrong* zone is the easiest serious mistake this system can make; see Phase 9's notes on why a country code is never turned into a timezone.
4. **The attempt ceiling and the retry delays** — per campaign, and lower where a jurisdiction requires it.
5. **Whether an AI disclosure is required in the opening**, and its exact wording. The default text is `I'm an AI assistant`, not required.
6. **Whether calls are recorded** (by your carrier, proxy or anything else — not by this software), and if so the disclosure wording and whether it is required.
7. **How long a do-not-call entry stands.** Entries have no expiry unless one is set (`expires_at`); removing one is a deliberate, named, audited act.
8. **Who may remove an entry.** Removal needs an admin key on the API (`manage`), or the CLI.

Encode 3–6 in `COMPLIANCE_JURISDICTIONS` and per campaign. An example — **illustrative shape only, not a statement of any law**:

```
COMPLIANCE_JURISDICTIONS={"US": {"calling_hours": "08:00-21:00", "calling_days": "mon-sun", "max_attempts": 3, "ai_disclosure_required": true}, "GB": {"calling_hours": "09:00-20:00", "calling_days": "mon-sat", "ai_disclosure_required": true}, "PK": {"calling_hours": "09:00-18:00", "calling_days": "mon-sat"}}
```

`uv run campaign.py compliance` prints the effective policy, per campaign and per jurisdiction, and says again whose decisions they are.

## 3. The settings

Environment (`server/.env`; every one is in `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `CALLING_HOURS`, `CALLING_DAYS`, `CALLING_TIMEZONE`, `ENFORCE_CALLING_HOURS` | `09:00-18:00`, `mon-fri`, the calendar's zone, `true` | Phase 9's window: the base every campaign and jurisdiction overlays. |
| `CAMPAIGN_MAX_ATTEMPTS`, `CAMPAIGN_RETRY_MINUTES` | `3`, `60` | Phase 5's ceiling and general wait. |
| `COMPLIANCE_RETRY_MINUTES_NO_ANSWER`, `_BUSY`, `_VOICEMAIL` | unset (the general wait) | Per-outcome waits. |
| `COMPLIANCE_AI_DISCLOSURE`, `COMPLIANCE_AI_DISCLOSURE_REQUIRED` | `I'm an AI assistant`, `false` | The AI disclosure and whether the opening must include it. |
| `COMPLIANCE_RECORDING_ENABLED`, `COMPLIANCE_RECORDING_DISCLOSURE`, `COMPLIANCE_RECORDING_DISCLOSURE_REQUIRED` | `false`, `this call may be recorded`, = `_ENABLED` | Recording, and its disclosure. |
| `COMPLIANCE_JURISDICTIONS` | unset | JSON object: region code → any of the campaign keys below. Validated at startup. |
| `COMPLIANCE_DEFAULT_JURISDICTION` | unset | A label for numbers whose region is unknown. |
| `COMPLIANCE_AUDIT_ALLOWED` | `true` | Write an audit row for calls the gate allowed, not only refusals. |

Per campaign (`campaign.py compliance <campaign> --set key=value`, or `PUT /api/v1/campaigns/{id}/compliance`), and per jurisdiction (the same keys inside `COMPLIANCE_JURISDICTIONS`):

`jurisdiction`, `calling_hours`, `calling_days`, `timezone`, `enforce_calling_hours`, `max_attempts` (1–20), `retry_minutes`, `retry_minutes_no_answer`, `retry_minutes_busy`, `retry_minutes_voicemail`, `ai_disclosure`, `ai_disclosure_required`, `recording_enabled`, `recording_disclosure`, `recording_disclosure_required`.

**Precedence:** environment < campaign < jurisdiction. The jurisdiction is applied last so that a campaign cannot loosen a rule the operator configured for a country; a campaign may narrow it.

**Not configurable:** honouring the do-not-call list and the `DO_NOT_CALL` status. There is no setting under which a listed number is dialled.

## 4. The do-not-call list

```bash
uv run campaign.py dnc 42 --reason "asked by email"           # a prospect: status + list row
uv run campaign.py dnc --number +923001234567 --reason "…"     # a bare number, prospect row or not
uv run campaign.py dnc-import registry.csv --source registry   # a suppression file
uv run campaign.py dnc-apply                                   # mark prospects for a list loaded after them
uv run campaign.py dnc-list [--number …] [--source …] [--all]  # newest first; --all includes revoked
uv run campaign.py dnc-remove +923001234567 --reason "…" [--reinstate]
```

API (roles from Phase 18): `POST /api/v1/dnc` (write), `GET /api/v1/dnc` (read_pii — it is a list of numbers), `GET /api/v1/dnc/check?phone=` (read; answers only blocked yes/no), `DELETE /api/v1/dnc/{phone}` (manage), `POST /api/v1/prospects/{id}/do-not-call?reason=` (write). n8n's workflows use an operator key; see `n8n/README.md`.

Sources recorded: `verbal` (asked during a call), `api`, `cli`, `import`, `registry`, `manual`. A second request for a listed number keeps the first record.

**Where it is enforced**, each independently of the others: the reservation SQL (`_reserve`), the queue outlook, the gate before every dial, `create_prospect`, `import_csv`, `add_prospects`, `POST /api/v1/calls` and `/callbacks` (refused with 409 `do_not_call` and audited), and the prospect's own status, which the list writes.

**A database that predates Phase 19** keeps dialling on the prospect's status alone until `uv run campaign.py init` adds the table; the store and the gate warn once each.

## 5. What is recorded, and where to read it

```bash
uv run campaign.py compliance-log                     # every compliance.* row, newest first
uv run campaign.py compliance-log --action compliance.blocked --since-hours 24
uv run campaign.py audit --actor dialer               # what the dialer decided
```

Each `compliance.allowed` / `compliance.blocked` row carries the prospect, the attempt, the campaign, the region the number resolved to, the jurisdiction label, the policy in one line (window, ceiling, waits, disclosures, sources), the decision code (`dnc_list`, `dnc_status`, `window_closed`, `attempt_limit`, `retry_wait`, `live_call`, `campaign_inactive`, `membership_closed`, `not_dialable`) and, for a refusal, the reason and the retry-after. `compliance.disclosure` rows say which sentences the opening was instructed to include, per call. Phone numbers in audit detail are masked; the prospect id is the key.

The dispositions on `call_results` (and in the CRM, and in the `call.completed` event) tell opt-outs from list refusals: `OPTED_OUT` means the person said so on that call; `DO_NOT_CALL` means the number was already known and the call was not placed.

## 6. What this software does not do

* It does not know any law, and does not check consent, relationship, industry or number-type rules.
* It does not contact any registry; it loads the files you give it.
* It does not record audio; the recording flag only governs the disclosure.
* It does not verify that the agent actually spoke the disclosure. The instruction is recorded; the transcript on the call result is where to confirm it, and the eval suite (`server/evals/`) is where to test it.
* Its calling-window enforcement is only as good as the timezone it is given. A number is never turned into a timezone.
* Its rate and concurrency limits are per process (Phase 9, Phase 11).
