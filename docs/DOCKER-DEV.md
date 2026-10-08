# Docker Development

## First run

Docker Desktop with Compose is required. If the repository's root `.env` does not exist, create it from the safe local template:

```sh
cp docker-compose.env.example .env
```

If `.env` already exists, keep it and add the Compose variables from `docker-compose.env.example`. The sample values are for local development only. Do not put production credentials in the sample or commit `.env`.

The `NEXT_PUBLIC_API_URL` value is the browser-reachable URL of the separate candidate-records API. The sample assumes that API is running on the host at `http://localhost:4000`.

Start the development stack from the repository root:

```sh
docker compose up --build
```

## Services

| Service | URL | Purpose |
| --- | --- | --- |
| Website | http://localhost:3000 | Next.js public site |
| Backoffice | http://localhost:3001 | Next.js internal console |
| FastAPI | http://localhost:8000/docs | API and OpenAPI docs |
| PostgreSQL | Docker network only | Local inventory database |

Both Next.js apps run in the single `ui` container. Their `/backend/...` requests are rewritten server-side to the `api` service; browser code does not need to resolve Docker service names. The website's candidate-records API remains external and uses the browser-reachable `NEXT_PUBLIC_API_URL` from `.env`.

The named `trackflow-development` network provides service-name DNS. PostgreSQL data and TinyDB data survive container recreation in named volumes. Source directories are bind-mounted for reload; dependency and Next.js build directories use separate volumes so host mounts do not hide container-installed files.

The API's local PostgreSQL URL uses `sslmode=disable` only with `APP_ENV=development`. Other environments continue to require PostgreSQL SSL. The local sample JWT secret and database password are placeholders for this development stack, not production credentials.

Stop the containers with Ctrl+C. `docker compose down` preserves named data volumes; removing volumes deletes local database and API data.