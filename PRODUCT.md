# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One technical owner-operator, today. The same person builds the system, configures it, launches campaigns, watches them run and follows up on what the calls produced. They know the pipeline and its vocabulary; they do not need the interface to explain what a campaign or a disposition is.

The application already has three roles (admin, operator, viewer), a registration page and an admin approval step for operator and admin sign-ups. Those exist for later users. Nobody other than the owner uses the app yet, and no second audience is confirmed.

The people the agent phones are prospects. They hear the product; they never see this interface.

## Product Purpose

Ai-Voice-Agent is an AI cold-calling sales agent. It phones prospects, runs discovery, qualifies them, handles objections, books meetings on a real calendar, schedules callbacks, honours do-not-call requests and can hand a live call to a person. Every finished call attempt, answered or not, produces one validated CRM-ready result: a disposition, qualification fields, meeting and callback status, the verbatim transcript and a summary.

The web application is the control room over that system. No single job dominates it; three carry equal weight:

- set up and launch: import contacts, configure a campaign and the agent's knowledge, start calling;
- watch campaigns run: progress, failures, the engine's state, and stepping in when something goes wrong;
- act on outcomes: review qualified leads, booked meetings and callbacks, and follow up.

## Positioning

The agent acts and then reports honestly on what it did. Tool calls go through strict schemas and a validating backend, each action is recorded with whether it succeeded, and the call summary is composed from the record rather than written by a model. Compliance (do-not-call, calling windows, ceilings, disclosures, the AI disclosure in the opening) is enforced in code and audited, not left to the prompt.

## Operating Context

- The application (`server/app.py`, port 7900) serves the single-page app in `server/web/` and mounts the dashboard and the automation API on one origin with one login.
- Pages: dashboard, campaigns and a create-campaign wizard, contacts and CSV import with preview and confirm, calls and call detail with the transcript, the live AI agent, the knowledge base, analytics, settings, login and register.
- The voice bot and the campaign engine run as separate processes on a machine; the app can also be deployed to Vercel without them, sharing the database.
- Around it: PostgreSQL (Supabase, pgvector for the knowledge base), a telephony carrier, HubSpot sync, Cal.com booking and n8n automations.
- The project is built in strict numbered phases; later-phase work is not started early. `HANDOFF.md` is the record of state, decisions and failed approaches.

## Capabilities and Constraints

- Frontend is dependency-free: one `index.html`, `styles.css`, `app.js`, hash routing, Inter bundled locally. No framework, no build step.
- A strict Content-Security-Policy blocks third-party origins; fonts, scripts and styles must be served from the app itself.
- `server/tests/test_app.py` asserts on strings in `app.js`; interface copy and markup changes can break it.
- Frontend phases have been frontend-only: no route, API contract, write or permission check changes as a side effect of design work.
- Roles gate what is shown and allowed; PII (transcripts, contact details) sits behind a `read_pii` permission and is masked otherwise.
- Light and dark themes both ship.
- The live agent page frames the bot's own client, which needs microphone permission delegated to the framed origin.
- Terminology in use: campaign, prospect / contact, call attempt, disposition, qualification, callback, do-not-call, opted out, knowledge base, engine.
- Undecided: whether and when the product is offered to users beyond the owner; no accessibility standard has been set.

## Brand Commitments

- The name is **Ai-Voice-Agent**, with the descriptor "AI sales calling". Both are final.
- The interface carries the Ai-Voice-Agent name, not the name of the company the agent sells for. The calling persona, knowledge base and tests are written for Hashmaker Solutions; that is campaign content, not the interface's brand.

## Evidence on Hand

- `HANDOFF.md`: phase-by-phase record of what was built and measured.
- `COMPLIANCE.md`: the calling-compliance policy the code enforces.
- `PRODUCTION_READINESS.md`: the readiness report; it does not declare the system production-ready.
- Real call data in the database: attempts, results, transcripts, campaign progress.
- Measured latency and per-call usage and cost figures recorded in `HANDOFF.md`.
- Not on hand, and not to be invented: customers, testimonials, case studies, logos, pricing, conversion benchmarks, or any claim that the system has run production calling at scale.

## Product Principles

1. Show what happened, not what was intended. Every figure and status traces to a recorded fact; failures carry their reason.
2. The operator stays in control. Anything that places calls or touches a prospect is explicit, visible and stoppable.
3. Compliance is a property of the system. Do-not-call and calling limits are never a setting the interface lets someone bypass casually.
4. One operator, whole loop. Setup, monitoring and follow-up are equal citizens; none is buried behind another.
5. Frontend work stays frontend. Design changes do not alter the pipeline, contracts or permissions.
