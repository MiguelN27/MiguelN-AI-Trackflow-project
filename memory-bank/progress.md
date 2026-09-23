# Development Progress

Last updated: 2026-09-23

## Update 2026-09-23 (later) - Centralized Incident Manager, stage 4 (frontend)

Goals accomplished:
- Built the incident manager's screens in **`uis/backoffice`**, resolving the open question from this morning's entry. The incident manager is internal - warehouse floor terminals, the CTO and the CEO - so it belongs in the backoffice console next to the supplier directory, not in the public-facing `uis/website`. Both screens sit in the `(protected)` route group. Full write-up in `docs/INCIDENTS.md`.
- **Registration form** (`/incidents/new`, `components/incidents/IncidentForm.tsx`): every model field, with `id`/`created_at`/`updated_at` stated as generated rather than faked as disabled inputs. `branch` is always visible and always required for every origin, with all five CONTEXT display names including "Central". When `origin` is `branch` the field takes an accent ring, a tinted background and the reminder "You are reporting from a specific location", wired into `aria-describedby` so the emphasis never rides on colour alone. Submitting shows a spinner, disables the button and sets `aria-busy`; the handler also returns early while a request is open, so a double tap cannot fire a second one. Success clears the fields and confirms with the title, status, branch and generated reference.
- **Incident list** (`/incidents`): filters by `status`, `origin` and `branch` held in the query string, loading and error states, a **Try again** button, and an empty state that always says why it is empty - the no-data case links to the form, the no-results case names the filters that produced it and offers to clear them.
- **Status editable from the row**, offering only the transitions the lifecycle allows and showing "Final state" where there are none. The change is optimistic so a terminal responds to the tap; a failure reapplies the status captured *before* the request and shows the reason next to the control. A confirmed change refreshes the summary.
- **Summary panel** with its own request and its own states, on the same page as the list, so neither can take the other down. Verified: killing the summary endpoint leaves the list fully working, and vice versa.
- **Nav**: added "Incidents" and "Report incident". Also fixed `isActiveLink`, which marked both as current on `/incidents/new` because one href is a prefix of the other - it now picks the longest match, so only one link ever carries `aria-current="page"`.
- **79 browser assertions** across five Playwright runs against real Chrome, the dev server and the API with the 95 seeded incidents: list + summary, interactions, form, failure modes, and the empty database. All passing. 101 backend tests still pass; typecheck, lint and production build all clean.

Decisions worth recording:
- **`lib/friendly-error.ts` is the only place that decides what a user reads about a failure.** A message is surfaced only when it arrived in the API's documented `{field, message}` shape *and* the status is 400 or 404 - the two the API writes plain language for. A 5xx, an unreadable body, an HTML error page from a proxy, a `TypeError: Failed to fetch`, or a thrown non-Error all get text written in the frontend. Without that rule `ApiError`'s own fallback ("Request failed with status 500") and the browser's network errors go straight to the screen, which is exactly what "never the raw server error text" forbids. There is also a guard rejecting any message containing a traceback, a stack frame, a `file:line`, an HTML tag or more than 300 characters.
- **The summary panel's form was chosen before its colour**, following the `dataviz` skill. `total` is the hero figure and the only one on the page. Category has nine classes - past the point where more colours help - so category, origin and branch are all a table with single-hue bars, every label and count written out, no tooltip because nothing is reachable only by hovering. Status is the one breakdown that earns colour, because its classes are states rather than identities.
- **The status palette was validated, not eyeballed, and the first attempt failed.** Mapping open to `serious` put `warning` and `serious` in adjacent tiles at normal-vision ΔE 13.6, below the 15 gate - hard to tell apart even with full colour vision. Moving open to `critical` clears both gates (CVD ΔE 11.3, normal-vision ΔE 27.6). `warning` stays 1.83:1 on white, a documented property of the fixed status palette, so it appears only as a filled mark beside dark text with an icon and a label. The numbers are recorded beside the tokens in `app/globals.css`.
- **Incident calls use `requestApi`, not `requestAuthenticatedApi`.** The API's incident routes carry no token requirement. Sending a bearer token the API does not read, through a helper whose whole job is handling a 401 that cannot happen, would describe a contract that does not exist. `services/incidents-service.ts` is the single file that changes if those routes are ever put behind auth.
- **Rows stay put when a status change makes them stop matching an active filter.** Removing the row the reader just acted on, out from under the pointer, is worse than showing it until the next load. Same behaviour as the supplier directory.

Fixed along the way:
- **A regression this morning's backend work introduced, caught only by running the app.** The `RequestValidationError` handler was registered app-wide, which turned FastAPI's 422 into a 400 on *every* route. `resetPassword` in `uis/*/services/auth-service.ts` reads any 400 as a spent reset token and replaces the form with "request a new link" - so a new password that was merely too short would have told the user their reset link had expired and sent them to restart a recovery that was working. `changePassword` has the same shape, reading a 400 as "your current password is wrong". The handler is now built by `make_validation_error_handler(prefixes)` and scoped to the incident router's own prefix, read off `incidents_router.prefix` so the two cannot drift. `tests/test_error_contract.py` pins both halves: `/api/incidents` answers 400, `/users` and `/auth/reset-password` still answer 422.
- The status control showed its generic fallback instead of the API's explanation of *why* a transition was refused, because that message arrives attached to the `status` field and the control is that field. It now prefers the field message.
- Seeded rows printed the same sentence as both title and description - the CSV had only a description, so short ones became both. The description is now shown only when it differs from the title.

Still missing / next goals:
1. **Bilingual UI.** The CONTEXT asks for the branch dropdown in the user's language, and `BRANCH_LABELS` in `packages/shared/trackflow_shared/incidents.py` already carries the Spanish names. `uis/backoffice` has no i18n layer at all, so the screens are English-only for now; adding one is its own piece of work, not something to fake per-dropdown.
2. **Authentication on the incident routes**, now that it is clear who reaches them and from where. Still a deliberate omission, not an oversight.
3. **An incident detail page.** The list carries everything the model holds, so there is nowhere to go for one incident yet - and no place to record who is handling it.
4. **The SLA quick-filter.** `SLA_CRITICAL_CATEGORIES` exists in shared code and still has no consumer; the list filters by category only through the URL, and there is no category dropdown on the panel (the brief asked for status, origin and branch).
5. **Unresolved for more than 24 hours** - what the CEO actually asked for, still unimplemented on either side. `created_at` and `status` are enough; it needs a metric on `/summary` or a filter.
6. The two pre-existing mypy errors are still open, both in files none of this work touched.

## Update 2026-09-23 - Centralized Incident Manager, stages 1-3

Goals accomplished:
- Built the data model, the historical seed and the backend for the centralized incident manager described in `contexts/centralized-incident.md`. Stages 1-3 of the milestone; the browser form and panel are the next stage. Full write-up in `docs/INCIDENTS.md`.
- **Shared vocabulary first.** `packages/shared/trackflow_shared/` is now a Python package, mapped into the wheel from `pyproject.toml` alongside `services` and `scripts`. It holds `incidents.py` (the four enumerations, `ALLOWED_TRANSITIONS`, bilingual branch labels, `SLA_CRITICAL_CATEGORIES`) and `incidents_csv.py` (the helpdesk-CSV rules and the CSV -> model maps). The seed has to produce exactly the values the API accepts, so a second copy of the category list would have drifted the first time one of them gained an entry.
- **Data model** (`services/incidents/models.py`), TinyDB table `incidents`: UUID4 `id`, required `title` (1-120) and `description` (1-4000) with whitespace stripped *before* the length check so a field of three spaces is a 400, enum `category` / `status` / `origin` / `branch`, and `created_at` / `updated_at` set by the service. `IncidentCreate` has no `id` or timestamps, so a client that sends them has them ignored.
- **Lifecycle** enforced from one adjacency map: `open -> in_progress | discarded`, `in_progress -> resolved | discarded`, `resolved` and `discarded` final. A refused move returns 400 with the reason in plain language and does not touch `updated_at`. A move to the status the incident already holds is refused for the same reason - it would change the modification timestamp without changing the incident.
- **Endpoints** under `/api/incidents`: `POST`, `GET` with `status` / `origin` / `branch` / `category` filters combining with AND, `GET /summary`, `GET /{id}`, `PATCH /{id}/status`. `/summary` is declared before `/{id}` because an incident id is an arbitrary string and the parameterised route would otherwise match the literal path. Left unauthenticated at this stage: the CONTEXT has anyone in the company reporting an incident, and no route exposes the commercially sensitive data that makes `/suppliers` token-only.
- **Empty database reads cleanly.** `GET /api/incidents` returns `[]`; `/summary` returns `total: 0` with every bucket present at zero - four statuses, nine categories, three origins, five branches - so a dashboard can render a fixed set of tiles instead of discovering an empty object.
- **Error handling centralised** in `services/core/http_errors.py`, registered app-wide rather than per router. `RequestValidationError` is remapped from FastAPI's 422-with-pydantic-dictionaries to a 400 naming one field in a sentence (`missing` -> "This field is required.", `enum` -> "Must be one of: ..."), with the `body` / `query` prefix stripped so the field is `title` rather than `body.title`; `detail` holds the first problem and `errors` holds all of them, so a form can focus one input and mark every bad field. A bare `Exception` handler makes any unhandled failure a 500 whose body names nothing about the exception - not its type, module or line - with the traceback going to the `trackflow.errors` logger. Every error response, 400 / 404 / 500 alike, has the same `{detail: {field, message}}` shape.
- **`scripts/seed_incidents.py`** loads the analyser project's CSV (copied to `data/raw/incidents-trackflow.csv`) as `origin: "customer"`, `branch: "central"` records. It reuses `validate_csv_row` rather than reimplementing the rules, so it rejects exactly the rows the analyser counted as invalid. Invalid rows are never inserted and are reported at the end, grouped by rule and then individually by CSV line number and incident id.
- **Verified against the analyser's published figures, exactly:** 100 rows, 95 inserted, 5 rejected with one row per rule; `open` 29 / `resolved` 52 / `discarded` 14; `lost_parcel` 14, `delivery_failure` 19, `returns_issue` 17, `carrier_issue` 45 (`DELAYED_DELIVERY` 38 + `DAMAGE` 7). `GET /api/incidents/summary` returns the same totals after seeding.
- **96 tests** in `tests/`, all passing. `conftest.py` gives each test its own TinyDB file by redirecting `DB_PATH` *and* clearing the `lru_cache` on the TinyDB handle - without the second half, one test's incidents leak into the next and into `data/suppliers.json`. Coverage includes all four legal transitions and all ten illegal ones, a forced unhandled exception asserted to return 500 with no traceback or file path in the body, and that no console line or issue message ever repeats a customer email.
- Added `npm run seed:incidents` and `npm run test:api`. Bumped the API description and version to 0.3.0.

Decisions worth recording:
- **Idempotency keys on the CSV `incident_id`, not on `title + created_at`.** Each seeded incident stores `source_incident_id`, an internal field excluded from `IncidentResponse` so it is not part of the API contract. The CONTEXT offers `title + created_at` as a fallback, but in the supplied export that pair repeats across 7 of the 100 rows, so keying every row on it would silently drop real incidents as duplicates. The fallback is still used for a row with no `incident_id`.
- **`GET /api/incidents/{id}` returns 404 for an unknown id, not the 400 the brief asked for.** Confirmed with the developer before implementing. It matches HTTP semantics and the existing `/suppliers/{id}`; the response body keeps the same shape as a validation error, so a client reads both the same way.
- **`branch` is `central` for every seeded record**, per the CONTEXT's mapping table and its rule that a customer-origin incident with no specific facility uses `central`. The transformation list in the brief mentioned "location -> branch", but the export's only location column is `country` (US / ES), and turning that into `la_office` / `zaragoza_office` would invent facility detail the helpdesk never recorded. Worth revisiting if the country breakdown is wanted for the CEO's Los Angeles vs. Zaragoza view - it would need a model field, not a branch guess.
- **Two CSV categories map onto `carrier_issue`.** Each target is the category whose own CONTEXT definition names the CSV case outright: `carrier_issue` is defined as "delay, damage, SLA breach", which covers both `DELAYED_DELIVERY` and `DAMAGE`. The seed prints its own category breakdown so the totals stay cross-checkable against the analyser.
- `UP042`, `UP017` and `B008` from ruff are left as-is in the new code: all three are existing conventions in `services/suppliers` and `services/users`, and matching the surrounding code beats the modernisation.

Still missing / next goals:
1. **Stage 4 - the browser form and panel.** An incident form sized for warehouse-floor terminals (large touch targets, minimal free text, branch dropdown labelled in the user's language from `BRANCH_LABELS`), a list with the four filters, and a summary panel. The `/api/incidents` contract and `docs/INCIDENTS.md` are what it builds against.
2. **Decide whether the panel goes in `uis/website` or `uis/backoffice`.** The incident manager is internal, which argues for `backoffice`, but `uis/website` already carries the protected route group and the auth provider.
3. **Authentication on the incident routes**, once the panel exists and it is clear who logs in from the warehouse floor. It is a deliberate omission now, not an oversight.
4. **A quick-filter for `SLA_CRITICAL_CATEGORIES`** in the panel - the set exists in shared code and nothing consumes it yet.
5. **Unresolved-for-more-than-24-hours** is what the CEO asked for and is not implemented anywhere. It needs no new field (`created_at` and `status` are enough) but it does need either a computed metric on `/summary` or a filter.
6. **The two pre-existing mypy errors are still open**, both in files this work did not touch: the pydantic-settings false positive in `services/core/config.py` and the `**dict[str, object]` unpacking in `services/suppliers/seed.py`.

## Update 2026-09-22 (later) - AUTH-03 frontend

Goals accomplished:
- Built the second half of AUTH-03: the three password screens, in **both** Next.js apps, on the API delivered earlier today.
- Routes added, following each app's existing structure rather than inventing a new one:
	- `uis/website`: `app/forgot-password/`, `app/reset-password/`, `app/(protected)/account/change-password/`.
	- `uis/backoffice`: `app/(auth)/forgot-password/`, `app/(auth)/reset-password/`, `app/(protected)/account/change-password/`. The public screens join the existing `(auth)` group, so they inherit the signed-out header; the protected one joins `(protected)` and is guarded by construction.
- `/forgot-password`: email field, one confirmation message, and the form locked afterwards. The email input and the submit button are both disabled and the button reads "Link sent", and the submit handler returns early once sent - so a programmatic `form.requestSubmit()` is refused too, not just a click on a disabled button.
- `/reset-password`: reads `?token=`, takes a new password plus confirmation, and on success redirects to `/login?reset=success`, where `LoginPage` renders a success banner. Two failure kinds are handled differently on purpose: a `400` means the token is dead, so the form is *replaced* by the error and a "Request a new link" button, because resubmitting cannot help; a `422` is mapped back onto the password input and the form stays, because a second attempt can succeed. A missing token shows the same replaced state without any request.
- `/account/change-password`: current password, new password and confirmation, with the match validated before any request. A `400` lands on the current-password field. Fields are cleared on success so the new password is not left sitting in the DOM.
- `/login` gained the "Forgot your password?" link under the password field, and `/account/profile` gained a link to the change-password screen, so neither new route is reachable only by typing the URL.
- Held the line on user enumeration in the client, not just the API: one confirmation string rendered from a constant for every success, so the registered and unregistered cases are character-identical; client validation that only checks the address *looks* valid and never asks the API whether it exists; and `requestPasswordReset` returning `void`, so there is deliberately nothing for a caller to inspect. A failure re-enables the form, which is safe because the route does not fail for an unknown address.
- Added `hooks/useLocationSearch.ts` to both apps, wrapping `useSyncExternalStore`. `useSearchParams` would force the calling page behind a Suspense boundary at build time (the reason `LoginPage` has always read `?next=` off `window`), and reading in an effect trips React 19's `react-hooks/set-state-in-effect`. The hook returns `null` on the server and the real query string on the client, so `/reset-password` shows "Checking your reset link..." instead of flashing a "link is missing" error at someone whose link is fine.
- Shared code rather than copying logic where it mattered:
	- `validateResetPasswordForm` and `validateChangePasswordForm` share one `newPasswordErrors` helper, so the two cannot drift apart.
	- `toFieldErrors` gained an optional alias map, so the API's snake_case field names (`new_password`, `current_password`) land on the camelCase inputs instead of falling through to a form-level message. Existing call sites were untouched.
	- `AuthField` gained `disabled:` styling, so a disabled input reads as disabled in every form, not just the new ones.
- A successful reset clears any stored token before redirecting, since it was issued against the old password. A successful change keeps the session, because the user is already signed in and proved it. Neither is a revocation story, which is still on the backlog.
- Executed formatting with auto-fix: `npm run lint -- --fix` in both apps. The first pass failed with four `react-hooks/set-state-in-effect` errors, which is what prompted the `useSyncExternalStore` hook rather than a suppression. Clean afterwards.
- Executed typechecking: `npm run typecheck` clean in both apps, and `npm run build` succeeded with all four new routes prerendered as static content.
- Attempted the test suite; still no `test` script anywhere, unchanged from every prior entry.
- Verified in a real headless browser against a running API on a scratch database: 102 checks across the two apps, all passing. Per app:
	- The `/login` link exists, points at `/forgot-password` and navigates there.
	- `/forgot-password` catches an empty and a malformed address client-side with no request sent; a registered address shows the confirmation on exactly one request; the input and button are disabled afterwards and the button reads "Link sent"; a programmatic re-submit sends nothing.
	- An unknown address produces a character-identical confirmation, a `200`, and the same locked form - the enumeration check, asserted on the rendered text rather than on the API alone.
	- `/reset-password` with no token shows the error, renders no form, and offers a link to `/forgot-password`; with a valid token it catches a mismatch and a short password client-side without a request, then redirects to `/login?reset=success` and shows the banner, leaving no stale token behind; the new password signs in and the old one does not; replaying the same link shows the error, removes the form, and offers a new link, and changes nothing.
	- `/account/change-password` sends an anonymous visitor to `/login` with the right `next`; is reachable from the profile page; catches a mismatch client-side with no request; reports a wrong current password after a real call; and on success clears the fields, keeps the session, and swaps which password works.
	- Two bugs in the verification script itself were found and fixed before trusting it: it matched the API on `127.0.0.1` while the apps call `localhost`, which made every "no request fired" assertion vacuous, and it waited on text that exists on both the source and destination page, so a navigation assertion passed before the navigation happened.
- Confirmed the disabled state is real rather than assumed: computed `opacity` settles at `0.6` and `cursor` at `not-allowed` on both the input and the button in both apps. The first screenshot was taken mid-transition and looked unstyled, which is what prompted measuring it.
- Reviewed the rendered screens at 1100px in both apps and removed a redundancy found there: the change-password subtitle repeated the signed-in email already shown in the session bar directly above it.
- Documentation: extended `docs/AUTHENTICATION-FRONTEND.md` with the route table, the anti-enumeration measures, the form lock, the query-string hook and why it exists, the recoverable-versus-not failure split, confirmation fields, field-name translation, and what happens to sessions. Updated `docs/PASSWORD-RECOVERY.md` to point at it, and added a section on the single `FRONTEND_BASE_URL` serving two apps.
- The production database was left untouched: 15 suppliers, 1 user, 1 profile. All verification ran against a scratch copy.

Future goals / still missing:
- Promote the 102 browser checks and the 111 backend checks into a committed suite. They are the closest thing this repo has to tests and they still live in a scratchpad.
- The two apps now duplicate five more files each. The case for moving `types/auth.ts`, `lib/auth.ts`, `lib/auth-storage.ts`, `hooks/useLocationSearch.ts` and the auth components into `packages/shared` is stronger than it was, since a change to the recovery rules now has to be made twice.
- Decide whether the backoffice should own the reset link instead of the website, or whether the API should learn which app a request came from.
- Carried over: rate-limiting `POST /auth/forgot-password`, a "your password was changed" notification, a guard against the console email backend running in production, refresh tokens or revocation, admin-scoping `GET /users`, and `uis/website` having no `.env.local`.

## Update 2026-09-22 (later) - AUTH-03 backend

Goals accomplished:
- Ran the mandatory memory-bank reading sequence before work: `context.md`, `projectbrief.md`, `techContext.md`, `progress.md`.
- Implemented the backend half of AUTH-03, password recovery and change. The frontend half (`/forgot-password`, `/reset-password`, `/account/change-password`) is the next stage and is deliberately not started.
- Before writing anything, restored two uncommitted working-tree edits that reverted earlier auth work, with the developer's confirmation:
	- `pyproject.toml` had lost `libpass[bcrypt]`, `python-jose[cryptography]`, `pydantic-settings` and `python-multipart` from its dependencies, plus the `seed-user` script. The venv still had the packages, so the API ran, but a `uv sync` would have pruned them and broken it.
	- `services/suppliers/db.py` had gone back to opening its own TinyDB handle instead of sharing `services/core/db.py`, which would have put two independent write caches on the same JSON file.
- Three new endpoints on the existing `/auth` router:
	- `POST /auth/forgot-password` - `{email}`, always `200` with an identical body whether or not the address is registered. The send is queued as a FastAPI background task so the two cases cannot be told apart by timing either. A deactivated account is treated exactly like a missing one.
	- `POST /auth/reset-password` - `{token, new_password}`. `400` for every way a token can fail, with one generic message; which way it actually failed goes to the log.
	- `POST /auth/change-password` - `{current_password, new_password}`, bearer token required. `400` on a wrong current password, because the session is fine and only the payload is not.
- Reset tokens are signed JWTs carrying `sub`, `jti`, `typ`, `iat` and `exp`. Signature and expiry are self-contained, so a forged or stale token is refused without a storage lookup.
- Single use is enforced by a `jti` registry in a new TinyDB table, `password_resets`. The row holds the `jti` and never the token, so a leaked database file does not hand anyone a working reset link. Rows past their expiry are purged whenever a new token is issued.
- Added a `typ` claim to every JWT: `access` for sessions, `password_reset` for reset links. Each decoder accepts only its own kind, so a reset link cannot be replayed as a bearer credential and a session cannot reset a password. Tokens minted before the claim existed are read as access tokens, so adding it signed nobody out.
- Setting a password by either route spends every outstanding reset link for that user. A reset link requested and then left unused is a live credential for its whole window, so changing the password has to close it.
- Integrated Resend (`uv add resend`, 2.47.0) in two layers that do not know about each other: `services/core/email.py` is transport only, and `services/auth/emails.py` owns the reset message. The backend is chosen by configuration rather than at the call site - Resend when `RESEND_API_KEY` is set, otherwise a console backend that logs the whole message including the link, so the flow is walkable locally without an email account. Delivery failures are caught and logged, never surfaced, since the send runs after the response.
- The email is one fluid column capped at 600px with inline styles, 16px body text, a 48px-tall button and a plain-text alternative, with no images and no external stylesheet. Verified in a real headless browser at 390px and 800px: no horizontal overflow at either width, button 308x48 on the phone viewport.
- Made the password rule one shared `Password` type in `services/users/models.py` (`Annotated[str, Field(min_length=8, max_length=72)]`) instead of a `Field` instance reused across models, so registration, admin edits, reset and change all validate identically.
- Configuration: four new variables, all documented in a reorganised `.env.example` and in the docs. `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES` (default 30) is validated at 15-60 by pydantic-settings, so a value outside the ticket's window fails at startup rather than at the first reset. `RESEND_API_KEY` is never hardcoded; `.env` stays git-ignored.
- Fixed the root `npm run api:suppliers` script, which pointed at `services.suppliers.main:app` - a module that stopped existing when the API gained a composition root. It is now `npm run api` against `services.main:app`, which is what `docs/AUTHENTICATION.md` already told people to run. Added `npm run seed:user`.
- Documentation: new `docs/PASSWORD-RECOVERY.md` (endpoint contract, token design, the Resend integration and its setup, configuration table, a local walkthrough, and an explicit list of what is not covered). Updated `docs/AUTHENTICATION.md` with the three routes, the `400` status code, the new configuration, the `typ` claim, and the module map.
- Executed formatting: `ruff format --line-length 100` reports every file written in this change already clean. The two files it would reformat, `services/server.py` and `services/users/router.py`, were not touched by this work and were left alone.
- Executed linting: `ruff check --select E,F,W,I,B,UP,SIM,C4` finds nothing in the new code beyond two `UP017` suggestions to use `datetime.UTC`; those were declined because the rest of the codebase uses `datetime.now(timezone.utc)` and matching it matters more.
- Executed typechecking: `mypy --ignore-missing-imports` over the changed modules reports one error, `Settings()` missing a named argument in `services/core/config.py`. It is the standard pydantic-settings false positive and predates this work.
- Attempted the test suite; still no `test` script anywhere, unchanged from the 2026-08-23, 2026-09-19, 2026-09-20 and 2026-09-21 entries.
- Verified end to end against a running API on an isolated copy of the database: 111 checks, all passing.
	- HTTP flow (65): registration and sign-in; a known address, an unknown address and a differently-cased address all returning the same `200`; no email composed and no registry row created for an unknown address; the token's subject, type, `jti` and 30-minute lifetime; the registry storing the `jti` and not the token; rejection of a garbage string, a tampered signature, a token signed with another secret, an expired token, an unregistered `jti`, and an access token presented as a reset token, each `400` with the same generic message; a too-short new password `422` without spending the token; the happy path setting the password, the old one ceasing to work and the token being stamped used; a replay `400` that changes nothing; two concurrent links where spending the newer kills the older; change-password rejecting an absent and a malformed bearer token with `401`, a wrong current password with `400`, a short new password with `422`, and succeeding with `200`; a reset link outstanding at change time dead afterwards; reset tokens rejected as sessions by `/auth/me`, `/suppliers` and `/profiles/me`; and, at rest, no plain-text password anywhere, a bcrypt `$2b$12$` hash, user rows carrying no reset state and registry rows carrying no token material.
	- Email integration (46): backend selection for an absent, empty and present API key; the exact Resend payload shape and that the key reaches the SDK; `EmailDeliveryError` wrapping both a provider rejection and an unreachable provider; `send_password_reset_email` returning `False` rather than raising; link construction including the trailing-slash case; the message stating the expiry and single use in both parts; the mobile-readability properties listed above; and the configuration guards at 14, 15, 30, 60 and 61 minutes.
- Audited the two security requirements the developer restated, 26 further checks, all passing:
	- Expiry: a reset token always carries an `exp` 30 minutes out, inside the mandated 15-60 window. Expiry is enforced at two independent gates - the JWT `exp`, and the registry row's `expires_at`. Confirmed a correctly signed but expired token is refused even when its `jti` is live and unused, and that a live JWT whose registry row was forced to look expired is refused too. Neither changes the stored password.
	- Single use: after one successful reset the row is stamped `used_at`, three consecutive replays are refused, and the password is unchanged by all three. The JWT itself is still cryptographically valid at that point, which is the whole reason the registry exists. Siblings die too: with three links outstanding, spending one kills both the older and the newer.
	- Keys: no provider key pattern (`re_`, `SG.`, `sk-`, `AKIA`) appears in any tracked file, and no literal-looking secret assignment does either. `.env` is git-ignored, has never been committed in any branch, and `.env.example` ships `RESEND_API_KEY=` empty. The key is read in exactly one place, as a pydantic-settings field with a `None` default; the only `os.getenv` in the codebase is for `DB_PATH`.
	- Also confirmed that with a provider key present, neither the key, the reset token nor the reset link reaches the log - only an acknowledgement that a send happened. `DUMMY_HASH` in `services/core/security.py` looks like a hardcoded credential but is a fixed bcrypt hash used to equalise login timing; no account uses it.
- The production database was left untouched: 15 suppliers, 1 user, 1 profile. All verification ran against a scratch copy with the developer's account stripped out.

Future goals / still missing:
- Build the AUTH-03 frontend: `/forgot-password`, `/reset-password` reading the token from the query string, and `/account/change-password`. Decide whether it lands in `uis/website`, `uis/backoffice` or both, and point `FRONTEND_BASE_URL` at whichever serves `/reset-password`.
- Set a real `RESEND_API_KEY` and decide the sender. Until a domain is verified with Resend, `onboarding@resend.dev` only delivers to the address that owns the Resend account.
- Guard the console email backend against production. With `RESEND_API_KEY` unset the API logs the full reset link, which is a working credential in the log. That is deliberate and necessary for local development, but nothing currently stops a deployment starting in that mode. An `ENVIRONMENT` setting that refuses to start when it is `production` and no provider key is configured would close it.
- Rate-limit `POST /auth/forgot-password`. Nothing currently stops an attacker filling a real user's inbox with reset links, even though each one is cheap and every previous link dies as soon as one is used.
- Send a "your password was changed" notification, which is the usual way someone discovers an account takeover.
- Consider rejecting a new password identical to the current one, on both routes. Deliberately left out to keep this change on spec.
- Carried over from 2026-09-21 and earlier: no `test` scripts anywhere and the 111 checks above still live in a scratchpad rather than a committed suite; the auth modules are duplicated across the two UIs; there is no refresh-token or revocation story; `GET /users` is still token-only rather than admin-scoped; and `uis/website` has no `.env.local` so `/candidates` cannot load records data locally.

## Update 2026-09-22

Goals accomplished:
- Ran the mandatory memory-bank reading sequence before work: `context.md`, `projectbrief.md`, `techContext.md`, `progress.md`.
- Gave the `uis/backoffice` sign-in and registration screens the same header bar as the signed-in views, so the app no longer changes shape at the door. The wordmark links to `/`, the dashboard.
- Split `BackofficeNav` rather than duplicating it:
	- `components/common/BackofficeNavShell.tsx` holds the bar itself, the wordmark link and the Dashboard / Suppliers / Profile destinations, with an optional `actions` slot. It owns the `usePathname` active-link state and has no session of its own.
	- `components/common/BackofficeNav.tsx` is now a thin session-aware wrapper that fills `actions` with the signed-in email and the Log out button.
	- The auth screens render the shell directly, so they show the destinations without the account controls there is no session to back.
- Added an `app/(auth)/` route group with a layout that renders the shell, and moved `login/` and `register/` into it. Like `(protected)`, the group adds no URL segment, so `/login` and `/register` are unchanged. The two groups now state the app's two modes structurally.
- Removed the centred wordmark `AuthCard` used to draw, since the header bar carries the branding, and tightened the card's vertical padding to suit.
- On the auth screens the destinations still point at protected routes; following one bounces back to `/login` through the existing guard, which is the intended behaviour rather than a dead link.
- `uis/website` needed no change: its sign-in and registration screens already inherit `TrackFlowNav` from the root layout.
- Executed formatting with auto-fix and typechecking in `uis/backoffice`: both clean.
- Attempted the test suite; still no `test` script, unchanged from every prior entry.
- Verified in a real headless browser: 11 checks on the signed-out header (both screens render exactly one header, carrying the wordmark and all three destinations, with no Log out; the wordmark's href is `/` and following it returns to `/login` via the guard) and 6 on the signed-in header against a scratch database (one header, destinations intact, account email and Log out present, suppliers still load with the token, logout still works). All 17 passed.
- The production database was left untouched by this work. It now holds one user, registered by the developer through the UI while reviewing the flows; the verification account went to a scratch copy that was deleted afterwards.

Future goals / still missing:
- Carried over from 2026-09-21: no `test` scripts anywhere, the auth modules are duplicated across the two apps, there is no refresh-token or revocation story, `GET /users` is still token-only rather than admin-scoped, and `uis/website` has no `.env.local` so `/candidates` cannot load records data locally.

## Update 2026-09-21

Goals accomplished:
- Ran the mandatory memory-bank reading sequence before work: `context.md`, `projectbrief.md`, `techContext.md`, `progress.md`.
- Implemented AUTH-02: authentication flows and protected views in both Next.js apps, closing the loop opened by AUTH-01. No separate auth app was created; the flows were integrated into `uis/website` and `uis/backoffice`.
- Identified the views that require a session and protected exactly those:
	- `uis/backoffice`: `/`, `/suppliers`, `/suppliers/[id]`, `/account/profile`.
	- `uis/website`: `/candidates`, `/candidates/[id]`, `/account/profile`.
	- Left public: `uis/website/` (the Milestone 1 corporate page), `/login` and `/register` in both apps, and the static `apps/website`, which is not a Next.js app and was not touched.
- Protection is structural rather than per-page: protected routes moved into an `app/(protected)/` route group whose layout renders `AuthProvider` + `AuthGuard`. Route groups add no URL segment, so every existing path is unchanged, and any route added under that folder is protected by construction.
- The check is client-side on purpose. The token lives in `localStorage` and there is no auth cookie, so Next.js middleware cannot read it. This is recorded in `docs/AUTHENTICATION-FRONTEND.md` so the decision is not re-litigated.
- Authentication views: `/login` (email and password, error message on failure) and `/register` (email, password with confirmation, plus optional `name`/`phone`/`address`). Registration calls `POST /users`, then `POST /auth/login` with the same credentials, stores the token and redirects. A failure between the two steps is reported as "account created, sign in manually" rather than as a registration failure.
- Account view: `/account/profile` shows the email and role from `GET /auth/me` and edits name, phone and address through `PUT /profiles/me`. A cleared input is sent as an explicit null, which is how the API erases a field.
- Token lifecycle centralized: stored under `trackflow.access_token`, attached as `Authorization: Bearer <token>` by `requestAuthenticatedApi`, and cleared on logout. A 401 clears the token and dispatches a `trackflow:unauthorized` event that `AuthProvider` turns into a redirect, so expiry is handled in one place instead of at every call site. The redirect carries a `?next=` path that is sanitized to same-origin absolute paths before use.
- Every `localStorage` access is wrapped in `try`/`catch`, so a browser that blocks site data sends the user to `/login` instead of throwing.
- `uis/backoffice`: all six `/suppliers` calls now go through `requestAuthenticatedApi`. `BackofficeNav` moved into the protected layout and gained the signed-in email, a Profile link and a Log out button, so the sign-in screens render without it.
- `uis/website`: identity calls go through a new `lib/auth-api-client.ts` on `NEXT_PUBLIC_AUTH_API_URL`, separate from `lib/api-client.ts`, because `NEXT_PUBLIC_API_URL` there points at the external records API. Sending the TrackFlow bearer token to that third-party host would leak a credential; verified in the browser that it never receives one.
- The public corporate page stays inert: `TrackFlowNav` remains a server component and gained only a static `Sign in` link. Verified in the browser that loading `/` performs no token read and no identity API call, both anonymously and while signed in.
- `ApiError` now carries the decoded body alongside the flattened message, so forms map failures back onto inputs: a 422 `detail` array becomes per-field messages, a 409 on `POST /users` lands on the email field, and a 401 from login stays a form-level message because the API reports a wrong email and a wrong password identically.
- Executed formatting with auto-fix: `npm run lint -- --fix` in both UIs (clean).
- Executed typechecking: `npm run typecheck` in both UIs (clean). Added the missing `typecheck` script to `uis/website`, which only had `lint`. `npm run build` succeeded for both apps with every route accounted for.
- Attempted the test suite; neither UI has a `test` script, unchanged from the 2026-08-23, 2026-09-19 and 2026-09-20 entries.
- Verified end to end in a real headless browser against a running API on an isolated copy of the database: 57 checks, all passing.
	- Route protection (17): every protected route in both apps redirects an anonymous visitor to `/login` with the correct `next` path, and the public page renders with no token read and no identity call.
	- Token lifecycle in `uis/backoffice` (25): wrong password shows the API message and stores nothing, client-side validation runs before any request, registration validates password length and confirmation, a duplicate email surfaces the 409 on the email field, a real registration creates the account and signs in, the profile loads from `GET /auth/me` and persists through `PUT /profiles/me` across a reload, `/suppliers` and `/profiles/me` carry the bearer header, a tampered token is cleared and redirected, and logout clears the token and closes the protected view again.
	- `uis/website` (15): sign-in honours `?next=`, the session bar shows the signed-in email, the external records API receives no bearer token, the profile view works, and logout clears the token while `/` stays reachable throughout.
- The production database was left untouched: 15 suppliers, 0 users, 0 profiles. All verification ran against a scratch copy.
- Documentation: added `docs/AUTHENTICATION-FRONTEND.md` (protected-view table, route-group layout, the middleware decision, token lifecycle, module layout, form error handling, configuration) and replaced the now-stale "Known follow-up" section in `docs/AUTHENTICATION.md`, which still said the backoffice sent no token.

Future goals / still missing:
- Define and standardize test scripts (`test`) across root and UI packages, and promote the browser verification above into a committed suite. Still outstanding from the 2026-08-23, 2026-09-19 and 2026-09-20 entries.
- The auth modules are duplicated between the two apps because `localStorage` is origin-scoped and `packages/shared` is not wired into either app's build. If a third app appears, move `types/auth.ts`, `lib/auth-storage.ts`, `lib/auth.ts` and the auth components into a shared package.
- Add refresh tokens or a token-revocation story. A token stays valid until it expires, and the UI has no silent-refresh path, so a long session ends with an abrupt redirect to `/login`.
- Decide whether `GET /users` and `GET /users/{id}` should be narrowed to admin and manager, carried over from the 2026-09-20 entry.
- `uis/website` still has no `.env.local`; `NEXT_PUBLIC_AUTH_API_URL` falls back to `http://localhost:8000` and `NEXT_PUBLIC_API_URL` for the records API remains unset, so `/candidates` cannot load data locally.

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
