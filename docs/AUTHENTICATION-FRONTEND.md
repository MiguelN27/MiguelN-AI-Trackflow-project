# Authentication in the Next.js apps (AUTH-02)

The API rejects every protected route without a bearer token
([AUTHENTICATION.md](./AUTHENTICATION.md)). This document covers the client side
of that contract: where the token is kept, how it is attached, and which views
require a session.

## Which views require a session

| App | Route | Protected | Notes |
| --- | --- | --- | --- |
| `uis/website` (:3000) | `/` | No | Public corporate page, Milestone 1 content |
| | `/login`, `/register` | No | Public by definition |
| | `/candidates`, `/candidates/[id]` | Yes | Talent pipeline tracker |
| | `/account/profile` | Yes | |
| `uis/backoffice` (:3001) | `/login`, `/register` | No | Public by definition |
| | `/`, `/suppliers`, `/suppliers/[id]` | Yes | Internal operations console |
| | `/account/profile` | Yes | |

`apps/website` is the static Flask-served marketing site. It has no session
logic and is untouched by this work.

## How protection is wired

Protected routes live in an `app/(protected)/` route group whose layout renders
`AuthProvider` + `AuthGuard`. The group adds no URL segment, so paths are
unchanged. Anything added under that folder is protected by construction.

```text
app/
  layout.tsx              # shell only
  page.tsx                # public corporate page (website only)
  (auth)/
    layout.tsx            # header bar without the account controls
    login/page.tsx        # public
    register/page.tsx     # public
  (protected)/
    layout.tsx            # AuthProvider > AuthGuard > nav > children
    ...
```

`AuthGuard` renders a placeholder until the session resolves, so protected data
is never painted before the check completes.

The two groups state the app's two modes structurally. In `uis/backoffice` the
header is split so both sides share it: `BackofficeNavShell` holds the wordmark
(linking to the dashboard) and the destinations, and `BackofficeNav` wraps it
with the signed-in email and the Log out button. The auth screens render the
shell alone, since there is no session for those controls to act on; their
destinations still point at protected routes and bounce back to `/login`
through the guard.

`uis/website` needs none of this: its auth screens inherit `TrackFlowNav` from
the root layout, which is public and session-free by design.

### Why not Next.js middleware

The token lives in `localStorage`. Middleware runs on the server, before the
browser hands over anything but cookies, so it cannot read the token. There is
no auth cookie by design, so the check has to be a client guard.

## Token lifecycle

| Event | Behaviour |
| --- | --- |
| Login | `POST /auth/login`, store `access_token` under `trackflow.access_token` |
| Registration | `POST /users`, then `POST /auth/login` with the same credentials, then store |
| Protected call | Read the token, send `Authorization: Bearer <token>` |
| `401` on a protected call | Clear the token, dispatch `trackflow:unauthorized`, redirect to `/login` |
| Logout | Clear the token, redirect to `/login` |

`AuthProvider` listens for `trackflow:unauthorized`, so expiry is handled in one
place rather than at every call site. The redirect carries `?next=<path>`, which
is sanitized to same-origin absolute paths before use, and login returns the
user there.

Every `localStorage` access is wrapped in `try`/`catch`: a browser that blocks
site data falls back to sending the user to `/login` rather than throwing.

## Module layout

Both apps carry the same modules, because `localStorage` is origin-scoped and
the two apps are served from different origins.

```text
types/auth.ts                     # AuthenticatedUser, Profile, form values, FieldErrors
lib/auth-storage.ts               # read/store/clear token, login URL + next sanitizing
lib/auth.ts                       # form validation, payload building, normalizers
services/auth-service.ts          # login, register, fetchCurrentUser, updateMyProfile, logout
components/auth/AuthProvider.tsx  # session state, 401 handling
components/auth/AuthGuard.tsx     # gate for the protected route group
components/auth/AuthField.tsx     # labeled input with its validation message
```

The HTTP layer differs by app:

- `uis/backoffice` — `lib/api-client.ts` already points at this API through
  `NEXT_PUBLIC_API_URL`, so it gained `requestAuthenticatedApi` alongside the
  existing `requestApi`.
- `uis/website` — `NEXT_PUBLIC_API_URL` points at the **external** candidate
  records API, so identity calls go through a separate `lib/auth-api-client.ts`
  using `NEXT_PUBLIC_AUTH_API_URL`. The records API never receives the
  TrackFlow bearer token, which would hand a credential to a third party.

## Error handling in forms

`ApiError` carries the decoded body alongside the flattened message, so forms
can map failures back onto their inputs:

- `422` — the `detail` array is split into `field: message` pairs and rendered
  under the matching input. Anything unmapped falls back to a form-level message.
- `409` on `POST /users` — a plain-string detail, shown on the email field.
- `401` on `POST /auth/login` — the API reports a wrong email and a wrong
  password identically, so the message belongs to the form, not to an input.

## Configuration

```bash
# uis/backoffice/.env.local
NEXT_PUBLIC_API_URL=http://localhost:8000

# uis/website/.env.local
NEXT_PUBLIC_API_URL=http://localhost:4000        # external records API
NEXT_PUBLIC_AUTH_API_URL=http://localhost:8000   # this repo's identity API
```

The API's CORS allowlist already covers `http://localhost:3000` and
`http://localhost:3001`.

## Running it

```bash
npm run api            # FastAPI on :8000
npm run dev:website    # :3000
npm run dev:backoffice # :3001
```

Register at `/register` in either app, or bootstrap an admin with
`uv run seed-user`.
