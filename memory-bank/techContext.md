# Technical Context

## Tech Stack (Website)

This repository currently contains two website implementations related to TrackFlow:

- `apps/website`: static marketing pages served by Flask.
- `uis/website`: Next.js app holding the public corporate page plus the session-protected hiring tracker and account views.

### Frontend

- `uis/website`
  - Next.js `16.2.7` (App Router structure under `app/`)
  - React `19.2.4`
  - TypeScript `^5`
  - Tailwind CSS `^4` via PostCSS plugin (`@tailwindcss/postcss`)
  - `next/font` with Plus Jakarta Sans and Space Grotesk for branding consistency
  - `hooks/useLocationSearch.ts` wraps `useSyncExternalStore` to read the query string. `useSearchParams` would force the calling page behind a Suspense boundary at build time, and React 19 rejects setting state from an effect body, so this is the remaining supported route for a browser-only value.
- `apps/website`
  - HTML + vanilla JavaScript
  - Tailwind via CDN (`https://cdn.tailwindcss.com`)
  - No build step required

### Backend

- FastAPI service mounted from `services/main.py`, the composition root. It owns CORS and mounts one router per domain: `auth`, `users`, `profiles`, `suppliers`. Run with `npm run api` (`uvicorn services.main:app --reload --port 8000`).
- Domain modules follow the layering in `docs/ARCHITECTURE_PROPOSAL.md`: `models.py` (Pydantic contracts), `service.py` (business rules and persistence), `router.py` (HTTP only).
- `services/core/` holds shared technical concerns and no business rules: `config.py` (pydantic-settings), `db.py` (TinyDB handle), `email.py` (transactional email transport), `errors.py` (domain errors), `security.py` (bcrypt + JWT).
- Python Flask app (`services/server.py`) remains a separate lightweight static server:
  - Serves `apps/website/index.html` at `/`
  - Serves static files from `apps/website` and fallback static files from repository root

### Authentication

- Stateless JWT only. There is no session store and no auth cookie.
- `OAuth2PasswordBearer` extracts `Authorization: Bearer <token>`; `python-jose` signs and validates it with `HS256`.
- `services/auth/dependencies.py::get_current_user` is the single gate for protected routes: decode, validate, load the user from TinyDB, 401 on any failure.
- Passwords are hashed with `libpass[bcrypt]` at cost 12. The import path stays `from passlib.hash import bcrypt`.
- Every JWT carries a `typ` claim naming what it may do: `access` for a session, `password_reset` for a reset link. Each decoder accepts only its own kind, so a reset link cannot be replayed as a bearer credential and a session cannot reset a password. Tokens minted before the claim existed are read as access tokens.
- Password recovery (`POST /auth/forgot-password`, `/auth/reset-password`, `/auth/change-password`) is in `services/auth/service.py`. Reset tokens are signed JWTs whose `jti` is registered in a TinyDB `password_resets` table so each one works exactly once; the table holds the `jti` and never the token. Setting a password by either route spends every outstanding reset link for that user. See `docs/PASSWORD-RECOVERY.md`.
- Configuration lives in a git-ignored `.env` (`JWT_SECRET_KEY`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`, `RESEND_API_KEY`, `EMAIL_FROM`, `FRONTEND_BASE_URL`), with `.env.example` committed. See `docs/AUTHENTICATION.md`.
- Password recovery screens (`/forgot-password`, `/reset-password`, `/account/change-password`) exist in both Next.js apps. `FRONTEND_BASE_URL` is a single value, so the emailed link points at one of them - `uis/website` on :3000 by default. See `docs/AUTHENTICATION-FRONTEND.md`.
- Frontend side: the token is stored in `localStorage` under `trackflow.access_token` and attached to every protected call. Protected views live in an `app/(protected)/` route group guarded by a client-side `AuthProvider`/`AuthGuard` pair; Next.js middleware is deliberately not used, because it cannot read `localStorage` and there is no auth cookie. A 401 clears the token and redirects to `/login?next=<path>`. See `docs/AUTHENTICATION-FRONTEND.md`.
- The public corporate page at `uis/website/` and the static `apps/website` carry no session logic at all: no token read, no redirect.

### Database

- TinyDB, one JSON file at `data/suppliers.json` (override with `DB_PATH`), with one table per domain: `suppliers`, `users`, `profiles`, `password_resets`.
- `User` and `Profile` are TinyDB-only and are never mirrored into Supabase/SQLModel. Their `id` is a UUID4 string, not a TinyDB `doc_id`, because future PostgreSQL tables reference it as `user_uuid`.
- Suppliers still key on the TinyDB `doc_id` integer, unchanged from the original implementation.

### APIs / Integrations

- External REST API integration in `uis/website` through `NEXT_PUBLIC_API_URL`.
- Endpoints exposed by the in-repo FastAPI service:
  - Public: `POST /users`, `POST /auth/login`, `POST /auth/token`, `POST /auth/forgot-password`, `POST /auth/reset-password`
  - Token-protected: `GET /users`, `GET /users/{id}`, `PUT /users/{id}`, `DELETE /users/{id}`, `GET /auth/me`, `POST /auth/change-password`, `GET /profiles/me`, `PUT /profiles/me`, and all six `/suppliers` routes
- `uis/backoffice` consumes the `/suppliers` routes with `Authorization: Bearer <token>` on every call.
- Both Next.js apps consume `POST /users`, `POST /auth/login`, `GET /auth/me` and `PUT /profiles/me` for their sign-in, registration and profile views.
- Endpoints consumed by `uis/website` from a separate external API:
  - `GET /records`
  - `GET /records/:id`
  - `POST /records`
  - `PATCH /records/:id`
  - `PUT /records/:id`
  - `GET /records/:id/notes`
  - `POST /records/:id/notes`
  - `DELETE /records/:id/notes/:noteId`
- Transactional email through Resend (`resend>=2.0`), used only to carry the password reset link. `services/core/email.py` picks the backend from configuration: Resend when `RESEND_API_KEY` is set, otherwise a console backend that logs the whole message so the recovery flow is walkable locally without an email account. Message bodies live in `services/auth/emails.py`, which knows nothing about the provider.
- Branding assets and web fonts are loaded from external sources (Google Fonts and social metadata targets).

### Language / Type System

- TypeScript in the Next.js tracker app with `strict: true` enabled in `tsconfig.json`.
- JavaScript (vanilla) for `apps/website/signup.js` form behavior.
- Python for Flask static server (`server.py`).

## Architectural Decisions Made

1. Monorepo-style separation by concern.
	- Distinct folders for apps, UIs, shared packages, agents, and skills.
2. Split website strategy.
  - Keep a static marketing site (`apps/website`) separate from the product UI (`uis/website`).
3. Backend decoupling for the tracker.
	- The Next.js tracker delegates data operations to an external API service via environment variable instead of coupling to local API routes.
4. Service-layer API access in the tracker.
	- Dedicated modules (`services/candidates-service.ts`, `services/notes-service.ts`) encapsulate HTTP calls.
5. Payload normalization boundary.
	- Candidate and note payloads are normalized in `lib/` utilities to absorb schema variants (for example `fullName` vs `name`, `status` vs `currentStatus`).
6. Password-recovery forms never ask the API a question they should not.
	- `/forgot-password` renders one confirmation constant for every success, so the registered and unregistered cases are character-identical, and `requestPasswordReset` returns `void` so there is nothing for a caller to inspect.
	- The form locks after a successful submit, and the submit handler returns early once sent, so the disabled attribute is the visible half of the guard rather than the whole of it.
	- Confirmation fields are checked in the browser and never sent; the API has no field to reject them with.
7. Client-rendered interaction model for tracker screens.
	- Main pages use `"use client"` and browser-side state management for filtering, forms, and optimistic-ish refresh behavior.
8. Shared visual identity through design tokens.
	- CSS custom properties and brand fonts in `globals.css` and `layout.tsx` define consistent TrackFlow theming.

## Technical Constraints

1. `NEXT_PUBLIC_API_URL` is mandatory for tracker API communication.
	- Missing configuration causes runtime errors in API client initialization.
	- `uis/website` needs a second variable, `NEXT_PUBLIC_AUTH_API_URL`, because its identity calls go to the in-repo FastAPI service while `NEXT_PUBLIC_API_URL` points at the external records API. It defaults to `http://localhost:8000`.
2. External API contract dependency.
	- UI behavior depends on `/records` and `/notes` endpoints and their HTTP semantics (status codes, JSON content types).
3. No in-repo backend/data source for the tracker.
	- Local development of full tracker workflows requires a separately running API service.
4. Mixed frontend paradigms increase maintenance overhead.
	- Static HTML/JS pages and Next.js/TypeScript app coexist with different tooling and conventions.
5. Schema inconsistency handling is required.
	- Multiple possible field names for candidate data imply unstable upstream payload shapes.
6. Limited server-side rendering usage for data-heavy tracker pages.
	- Current client-side fetching model can impact first-load latency and SEO relevance for authenticated app content.
7. Password reset email delivery depends on external configuration.
	- With `RESEND_API_KEY` unset the API still works, but no mail leaves the machine: the reset link is only logged. This is deliberate for development and wrong for anything else.
	- Resend's shared sender `onboarding@resend.dev` needs no DNS setup but in test mode only delivers to the address owning the Resend account. Mailing real recipients requires verifying a domain.
	- `FRONTEND_BASE_URL` must point at whichever Next.js app serves `/reset-password`, or the emailed link goes nowhere.
8. Root Flask dependency version appears non-standard.
	- Root `package.json` declares `flask` under npm dependencies, while runtime uses Python `flask` in `server.py`; setup consistency depends on developers understanding this split.
