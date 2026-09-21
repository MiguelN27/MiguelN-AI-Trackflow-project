# Authentication and Route Protection (AUTH-01)

Stateless JWT authentication for the TrackFlow API. No sessions, no auth cookies:
the bearer token is the only credential the API accepts.

## Where things live

```text
services/
  main.py                  # app composition root: mounts every domain router
  core/
    config.py              # Settings (pydantic-settings), reads .env at startup
    db.py                  # shared TinyDB handle, one table per domain
    errors.py              # domain errors the routers translate into HTTP
    security.py            # bcrypt hashing + JWT encode/decode
  auth/
    dependencies.py        # get_current_user, is_admin
    models.py              # LoginRequest, TokenResponse, AuthenticatedUserResponse
    router.py              # /auth/login, /auth/token, /auth/me
  users/
    models.py              # Role enum, UserCreate/Update/InDB/Response
    service.py             # create/get/get_by_email/list/update/delete
    router.py              # /users
    seed.py                # CLI to bootstrap an admin
  profiles/
    models.py service.py router.py   # /profiles
  suppliers/
    router.py              # existing routes, now token-protected
```

## Storage

`User` and `Profile` live in **TinyDB only**, in the tables `users` and `profiles`
of the same file the suppliers directory uses (`data/suppliers.json`, override with
`DB_PATH`). They are never mirrored into Supabase/SQLModel.

The `id` is a **UUID4 string**, not a TinyDB `doc_id` integer, because PostgreSQL
tables (inventory and the rest) reference it as `user_uuid`. Those tables store that
id and nothing else about the user.

`User` holds credentials only - `id`, `email`, `hashed_password`, `is_active`,
`role`, `created_at`. Display name and contact data live on `Profile`, linked
one-to-one through `user_id`.

## Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/users` | public | Register. Hashes the password, creates the linked profile in the same operation. Always role `user`. |
| `GET` | `/users` | token | List all users. |
| `GET` | `/users/{id}` | token | Single user. |
| `PUT` | `/users/{id}` | token | Self or admin. `role` and `is_active` are admin-only. |
| `DELETE` | `/users/{id}` | token | Self or admin. Removes the linked profile too. |
| `GET` | `/profiles/me` | token | The caller's profile. |
| `PUT` | `/profiles/me` | token | Update `name`, `phone`, `address`. Owner only by construction. |
| `POST` | `/auth/login` | public | JSON `{email, password}` -> JWT. |
| `POST` | `/auth/token` | public | Same thing, OAuth2 form-encoded. This is what the `/docs` **Authorize** button posts to. |
| `GET` | `/auth/me` | token | Email, role and the linked profile. |
| all 6 | `/suppliers*` | token | Rates and contact emails are commercially sensitive. |

`POST /users`, `POST /auth/login` and `POST /auth/token` are the only public routes.

## Status codes

- `401` - no token, malformed token, bad signature, expired token, or the user
  behind the token no longer exists.
- `403` - the token is valid but the caller may not do this: acting on someone
  else's account, changing `role`/`is_active` without being an admin, or a
  deactivated account.
- `409` - email already registered.
- `422` - payload validation, including a `role` outside `admin|manager|user`.

## Configuration

Copy `.env.example` to `.env` (git-ignored) and set:

| Variable | Meaning |
| --- | --- |
| `JWT_SECRET_KEY` | Signing secret, minimum 32 chars. Never hardcoded, never committed. |
| `JWT_ALGORITHM` | `HS256`. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime. |
| `DB_PATH` | Optional TinyDB file override. |

Settings are validated by `pydantic-settings`, so a missing or too-short secret
fails at startup rather than at the first login.

## Passwords

`libpass` with the bcrypt scheme, cost 12 (`from passlib.hash import bcrypt` -
the import path is unchanged from the unmaintained `passlib`). Passwords are
capped at 72 bytes, the point past which bcrypt silently ignores input, and are
never stored or compared in plain text. A login against an unknown email still
runs one bcrypt verification so a missing account is not faster to probe.

## Running it

```bash
npm run api                              # uvicorn services.main:app --reload --port 8000
uv run seed                              # suppliers directory
uv run seed-user --email you@trackflow.com --role admin
```

`POST /users` deliberately cannot create an admin, so the first admin is created
with `seed-user`. Run it again on an existing email to change that user's role.

## Manual check in /docs

1. `POST /users` - register with an email, a password and optional `name`/`phone`/`address`.
2. **Authorize** (top right) - enter the email in the `username` field and the password.
   Swagger posts to `/auth/token` and stores the token.
3. Call `GET /auth/me`, `GET /profiles/me` or any `/suppliers` route - they work.
4. **Logout** in the Authorize dialog and retry - `401`.
5. Paste a mangled token into Authorize and retry - `401`.

## Known follow-up

The backoffice UI at `uis/backoffice` calls `/suppliers` without a token and will
get `401` until it is updated to log in and send the header. That is expected at
this stage and is the next piece of work.
