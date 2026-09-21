# Development Progress

Last updated: 2026-09-20

## Update 2026-09-20

Goals accomplished:
- Ran the mandatory memory-bank reading sequence before work: `context.md`, `projectbrief.md`, `techContext.md`, `progress.md`.
- Implemented AUTH-01: stateless JWT authentication and route protection across the FastAPI service. No session or cookie authentication anywhere; the bearer token is the only credential.
- Restructured the API into a composition root plus domain modules, following `docs/ARCHITECTURE_PROPOSAL.md`:
	- `services/main.py` now owns the FastAPI app and CORS, and mounts every domain router. `services/suppliers/main.py` became `services/suppliers/router.py`.
	- New `services/core/`: `config.py` (pydantic-settings, validated at startup), `db.py` (shared TinyDB handle, one table per domain), `errors.py` (domain errors the routers translate to HTTP), `security.py` (bcrypt + JWT).
	- New `services/users/`, `services/profiles/`, `services/auth/`, each with models, service and router layers.
	- `services/suppliers/db.py` now delegates to `services/core/db.py` and keeps owning the `suppliers` table name, so `seed.py` was unchanged.
- `User` and `Profile` are stored in TinyDB only, in the `users` and `profiles` tables of the existing database file. The `id` is a UUID4 string rather than a TinyDB `doc_id`, because PostgreSQL tables will reference it as `user_uuid`.
- `User` holds credentials only (`id`, `email`, `hashed_password`, `is_active`, `role`, `created_at`); display name and contact data live on the one-to-one `Profile` (`id`, `user_id`, `name`, `phone`, `address`). `Role` is an `Enum` of `admin|manager|user`, so anything else is rejected with 422.
- Endpoints added: `POST/GET/PUT/DELETE /users`, `GET/PUT /profiles/me`, `POST /auth/login`, `POST /auth/token` (OAuth2 form variant that powers the Authorize button in `/docs`), `GET /auth/me`.
- `POST /users` hashes the password, accepts optional `name`/`phone`/`address` and creates the linked profile in the same operation, rolling the user row back if the profile insert fails. It always creates role `user`, so nobody can self-grant privileges at signup. `DELETE /users/{id}` removes the linked profile too.
- Added a reusable `get_current_user` dependency that reads `Authorization: Bearer <token>` via `OAuth2PasswordBearer`, decodes and validates the JWT with `python-jose`, loads the user from TinyDB, and raises 401 on any failure. A valid token for a deactivated account is 403, not 401.
- Protected 13 of 16 routes. Only `POST /users`, `POST /auth/login` and `POST /auth/token` are public. All six existing `/suppliers` routes are now token-protected, which exceeds the minimum of five.
- Authorization rules: `PUT`/`DELETE /users/{id}` are self-or-admin (403 otherwise), `role` and `is_active` are admin-only, and `/profiles/me` is scoped to the caller by construction.
- Passwords use `libpass[bcrypt]` at cost 12, capped at 72 bytes. A login against an unknown email still performs one bcrypt verification so a missing account is not faster to probe.
- Secrets moved to a git-ignored `.env` with a committed `.env.example`: `JWT_SECRET_KEY` (validated at minimum 32 chars), `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`. Nothing is hardcoded.
- Added `uv run seed-user` to bootstrap or promote an admin, since `POST /users` deliberately cannot create one. Renamed the `api:suppliers` npm script to `api`, pointing at `services.main:app`.
- Dependencies added via `uv add`: `libpass[bcrypt]`, `python-jose[cryptography]`, `pydantic-settings`, `python-multipart`.
- Verified with a 74-check end-to-end suite against a running server on an isolated database: registration and duplicate/validation failures, login success and failure, `/profiles/me` scoping and partial updates, ownership and admin rules, deactivated accounts, all six supplier routes rejecting unauthenticated calls, and cascade deletion leaving no orphaned profile rows. Confirmed directly against the stored records that no plain-text password is persisted, that hashes are bcrypt `$2b$` cost 12, and that `User` carries no profile fields.
- Verified 401 on every token failure mode: absent header, malformed token, garbage string, wrong scheme, missing `Bearer` prefix, empty bearer, tampered payload, stripped signature, `alg=none` forgery, a `role` claim escalated to `admin`, a token signed with a different secret, an expired token, and a token whose user has been deleted.
- Confirmed `ACCESS_TOKEN_EXPIRE_MINUTES` is honoured end to end by issuing a token with a one-minute lifetime, checking `exp - iat = 60`, and re-calling the route after it lapsed.
- The production database was left untouched: 15 suppliers, 0 users, 0 profiles. All verification ran against a scratch database.
- Added `docs/AUTHENTICATION.md` covering module layout, endpoint table, status-code contract, configuration, and the manual `/docs` walkthrough.

Future goals / still missing:
- Update `uis/backoffice` to log in and send `Authorization: Bearer <token>`. Its `/suppliers` calls now return 401, which is the expected outcome of this stage and the next piece of work.
- Decide whether `GET /users` and `GET /users/{id}` should be narrowed to admin and manager. They currently require a token only, which is what the ticket specified, but listing every account's email is broad for a plain `user`.
- Add refresh tokens or a token-revocation story. Today a token stays valid until it expires; deactivating a user is caught because `get_current_user` reloads the record on every request, but there is no way to revoke a single token.
- Define and standardize test scripts (`test`) across root and UI packages, and promote the end-to-end auth checks into a committed pytest suite. Still outstanding from the 2026-08-23 and 2026-09-19 entries.
- Refresh `techContext.md`, partially addressed in this update for the auth layer.

## Update 2026-09-19

Goals accomplished:
- Ran the mandatory memory-bank reading sequence before work: `context.md`, `projectbrief.md`, `techContext.md`, `progress.md`.
- Built the Supplier Directory frontend in `uis/backoffice`, consuming the FastAPI service in `services/suppliers` at `http://localhost:8000`:
	- Foundation: `types/supplier.ts` (unions, `SUPPLIER_CATEGORIES`, `CURRENCY_BY_COUNTRY`), `types/async-state.ts`, `lib/api-client.ts`, `lib/supplier.ts` (labels, rate formatting, `validateSupplierForm`, `buildSupplierPayload`, payload normalization), `services/suppliers-service.ts` (list, detail, create, rate patch, status patch).
	- Shared UI and navigation: `components/common/StateMessage.tsx`, new `components/common/BackofficeNav.tsx` (Dashboard / Suppliers with `usePathname` active-link state) wired into `app/layout.tsx`, and `components/suppliers/SupplierStatusBadge.tsx`.
	- List and filters: `app/suppliers/page.tsx` plus `SuppliersListPage`, with server-side country/category filtering persisted to the URL via `router.replace(..., { scroll: false })`, and inline `SupplierRateEditor` / `SupplierStatusToggle` that swap the edited row in state from the API response instead of refetching the list.
	- Create form: `SupplierCreateForm` with client validation first and currency auto-derived from country as a read-only field.
	- Detail view: `app/suppliers/[id]/page.tsx` (Next.js 16 async `params`) plus `SupplierDetailPage`, reusing the same rate editor and status toggle.
	- Configuration: `.env.local` and `.env.example` with `NEXT_PUBLIC_API_URL`, `next dev -p 3001` so the backoffice does not collide with `uis/website` on port 3000, and a new `typecheck` script.
- Centralized FastAPI error handling in `lib/api-client.ts`, covering both response shapes: `{ detail: string }` for 404 and the `{ detail: [{ loc, msg, type }] }` validation array for 422, which is flattened into a readable `field: message` string.
- Executed formatting with auto-fix: `npm run lint -- --fix` in `uis/backoffice` (clean).
- Executed typechecking: `npm run typecheck` in `uis/backoffice` (clean), and `npm run build` succeeded with `/suppliers` prerendered and `/suppliers/[id]` server-rendered on demand.
- Attempted the test suite; `uis/backoffice` still has no `test` script, unchanged from the 2026-08-23 entry.
- Verified the feature end to end against the running API and a headless browser: 15 rows listed, Laser Ship and SAP WM Cloud rendered as suspended, `country=Spain` returning 6 rows and `category=carrier_international` returning 2 without a page reload, supplier creation succeeding and a 422 surfacing in the form, and rate/status edits updating the row immediately and persisting across a refresh. Data created during verification was removed afterwards, leaving the seeded directory at 15 suppliers.

Future goals / still missing:
- Define and standardize test scripts (`test`) across root and UI packages so pre-commit test validation can run consistently.
- Add baseline automated tests for `lib/api-client.ts` error extraction, `lib/supplier.ts` validation and payload building, and the suppliers service layer.
- Refresh `techContext.md`, whose "Current consumed endpoints" section still describes only the `uis/website` `/records` API and does not yet mention the suppliers service or the backoffice UI.
- Decide whether the backoffice needs pagination, supplier deletion, editing of name/country/categories, and authentication, all of which were deliberately excluded from this iteration.

## Update 2026-08-23

Goals accomplished:
- Ran the mandatory memory-bank reading sequence before work: `context.md`, `projectbrief.md`, `techContext.md`, `progress.md`.
- Executed formatting with auto-fix in both UI apps:
	- `npm --prefix uis/website run lint -- --fix`
	- `npm --prefix uis/backoffice run lint -- --fix`
- Executed typechecking in both UI apps:
	- `npx tsc --noEmit` in `uis/website`
	- `npx tsc --noEmit` in `uis/backoffice`
- Attempted to run test suites at root and UI-app levels; no `test` script is currently defined in these `package.json` files.

Future goals / still missing:
- Define and standardize test scripts (`test`) across root and UI packages so pre-commit test validation can run consistently.
- Add at least baseline automated tests (unit/integration) for critical UI service and normalization layers.

## Executive Summary

The project has a solid foundation on the frontend side, with two clear deliverables already implemented:

1. A branded static marketing website in `apps/website` (homepage + signup flow + client-side validation).
2. A functional Next.js operations UI in `uis/website` with candidate and notes CRUD interactions against an external API.

This means the project is already demonstrating product thinking, UI execution, and service-layer integration patterns. However, compared to TrackFlow's business objectives, most logistics-core capabilities (warehouse unification, carrier intelligence, returns automation, CX automation, telemetry, and executive dashboards) are still pending and currently represented mostly as narrative/UX framing rather than implemented business systems.

## Current State by Project Objective

### 1) Unified operational backbone (Mexico + Spain)

Status: In progress (early stage)

What exists:
- A typed frontend architecture that can consume external operational records (`/records` endpoints).
- Normalization utilities in `uis/website/lib/candidate.ts` and `uis/website/lib/notes.ts` that tolerate schema variations.

What's missing:
- No in-repo unified inventory model/API.
- No warehouse, carrier, returns, or ERP integration implementation.
- No shared domain model yet for TrackFlow logistics entities in `packages/shared/types/index.ts` (currently placeholder types only).

### 2) Reduce manual work through automation

Status: Not started for core logistics automation

What exists:
- Form validation and UI workflows (candidate management, notes).
- Signup form validation in `apps/website/signup.js`.

What's missing:
- No implemented automation workflows for order ingestion, carrier selection, tracking updates, return approvals, or weekly executive reports.
- `workflows/` exists structurally but automation pipelines are not implemented.

### 3) Improve decision-making with real-time dashboards

Status: Early UI groundwork

What exists:
- Tracker UI includes list/search/filter and detail views that simulate dashboard-like workflow for candidate operations.
- Branded visual system and reusable status UI component (`StateMessage`).

What's missing:
- No real-time operational KPIs (shipments, delivery performance, returns, CSAT, costs).
- No telemetry/data pipeline feeding dashboards.
- No executive dashboard implementation.

### 4) Increase service quality with proactive support

Status: Not started (business capability)

What exists:
- Branded web presence and contact/signup pathways.

What's missing:
- No bilingual support agent.
- No RAG knowledge base.
- No unified ticketing model.
- No proactive alerting or sentiment analysis.

### 5) Strengthen scalability and resilience

Status: Foundation only

What exists:
- Separation of concerns: static site, Next.js UI, service layer, normalization helpers.
- Strict TypeScript setup in the Next.js UI and clear API abstraction via `api-client.ts`.

What's missing:
- No centralized logging/monitoring.
- No health checks, no alerting, no incident workflow automation.
- No deployment pipeline or runtime observability standards documented.

## Implemented Assets (Verified)

### Product/UI Deliverables

- `apps/website/index.html`
	- Corporate landing page aligned to TrackFlow narrative.
	- SEO/social metadata + structured data.
	- Responsive layout and dark mode toggle.

- `apps/website/signup.html` + `apps/website/signup.js`
	- Multi-section onboarding form mapped to TrackFlow operational realities.
	- Client-side validation with inline feedback and accessibility attributes.

- `uis/website`
	- Candidate list page with load/error/success states.
	- Search + filter by status/stage via query params.
	- Candidate creation modal (POST `/records`).
	- Candidate detail page with:
		- Status/stage update (PATCH `/records/:id`)
		- Full profile update (PUT `/records/:id`)
		- Notes list/create/delete (`/records/:id/notes`)

### Technical Building Blocks

- `uis/website/lib/api-client.ts`
	- Base URL handling and request wrapper.
	- Centralized HTTP error handling.

- `uis/website/lib/candidate.ts`
	- Payload normalization and flexible field mapping.
	- Candidate form validation and data transformation helpers.

- `uis/website/lib/notes.ts`
	- Notes payload normalization from multiple possible API shapes.

- `uis/website/services/*.ts`
	- Service-layer abstraction for API calls.

## Gaps and Risks Right Now

1. Backend dependency risk:
- The tracker requires `NEXT_PUBLIC_API_URL` and external endpoints; without them, flows break at runtime.

2. Domain mismatch risk:
- Current implemented app domain is "talent pipeline" while business objectives focus on logistics operations. The architecture is reusable, but domain alignment work is still needed.

3. Data contract instability risk:
- Extensive field normalization suggests upstream payloads may be inconsistent; this can slow feature velocity and increase bugs.

4. Delivery risk for later milestones:
- No telemetry, pipeline, RAG, agent, or workflow implementations yet, which are required for mid/late milestones.

## Recommended Next Steps

## Phase 1 (Immediate: 3-5 days) - Align scope and unblock end-to-end delivery

1. Define the canonical TrackFlow domain schema in `packages/shared/types/index.ts`.
2. Decide if `uis/website` will be repurposed to logistics entities (recommended) or kept as a separate demo UI.
3. Create a minimal backend contract document for:
	 - inventory
	 - shipments/tracking
	 - carriers
	 - returns
	 - tickets/CX
4. Add `.env.example` and setup docs for `NEXT_PUBLIC_API_URL` and required endpoints.

Deliverable:
- Shared domain types + API contract v1 + environment setup baseline.

## Phase 2 (Near term: 1-2 weeks) - Build first logistics vertical slice

Target one complete workflow first:
- "Shipment tracking and incident visibility"

Scope:
1. Implement backend endpoints (or mock service if backend is out of scope this sprint).
2. Build a Next.js operations page for shipment list/detail and status timeline.
3. Add basic KPIs: on-time %, delayed shipments, incidents by carrier.
4. Add tests for service adapters and normalization logic.

Deliverable:
- One production-like vertical slice that maps directly to TrackFlow operations pain.

## Phase 3 (Following 2-4 weeks) - Add automation + observability

1. Workflow automation (n8n or script-based):
	 - scheduled weekly executive summary generation
	 - alerting for delivery delay thresholds
2. Telemetry baseline:
	 - structured logs
	 - API error-rate tracking
	 - basic health dashboard
3. Returns automation MVP:
	 - rule-based approval engine (start with deterministic rules)

Deliverable:
- First automation and observability capabilities tied to measurable ops impact.

## Phase 4 (After core ops baseline) - AI capabilities

1. RAG knowledge base for CX policies and SOPs (ES/EN).
2. Bilingual support assistant for top repetitive intents (tracking + return status).
3. Carrier recommendation assistant with transparent rule explanations.

Deliverable:
- AI features built on top of stable operational data and workflows.

## Priority Backlog (Ordered)

1. Canonical logistics data model + shared types.
2. Backend/API contract for core entities.
3. First operations dashboard vertical slice.
4. Automated weekly report generation.
5. Telemetry/monitoring foundation.
6. Returns automation MVP.
7. CX RAG + assistant.

## Suggested Definition of "Next Milestone Done"

The next milestone should be considered complete when all of the following are true:

1. At least one logistics workflow is fully functional end-to-end (UI + API + data).
2. KPI widgets are driven by real endpoint data (not static placeholders).
3. Errors and empty states are handled across list/detail views.
4. Minimum test coverage exists for data normalization and service layer logic.
5. A short runbook explains how to run frontend + API + environment locally.
