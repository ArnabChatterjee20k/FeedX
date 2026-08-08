# FeedX API — End-to-End Tests (Appwrite only)

A true simulation of the admin API: HTTP requests go through the real FastAPI app
(`TestClient`) and land as real rows in a real Appwrite database. Nothing is mocked.

## The bootstrap problem (and how it's solved)

Provisioning a project, an API key, and a database are **Console** operations — a
project API key can't do them. So the harness talks to the Console API directly over
raw HTTP (`requests`, in `conftest.py`) to:

1. sign up + log in a console user (reading the session secret from the
   `X-Appwrite-Session` response header — it isn't in the JSON body),
2. create a fresh throwaway **project** — teams + `/v1/projects` on self-hosted,
   organizations on Cloud (auto-detected by probing `/v1/organizations`),
3. mint a scoped **API key**,
4. hand those to FeedX's own `init_database()` to build the schema.

(The `appwrite-console` SDK is Cloud-only — its project routes 404 on a self-hosted
build and it hides the login response headers — so we don't use it.)

Then the test drives `login → sources → hostnames → content → interactions → queues`
and asserts against Appwrite directly. The project is **deleted on teardown**.

## Prerequisites

- Docker (to run the bundled Appwrite stack), **or** your own self-hosted Appwrite
  (v1.9.6+) you can create/delete projects on.
- Console user credentials (the tests create the console user on first login).

## Bundled Appwrite stack (recommended)

A throwaway Appwrite instance, pinned to the stable **1.9.6** image, is vendored under
[`appwrite/`](./appwrite) as a **trimmed** compose — only the services the API-only
e2e needs (core API, databases worker, MariaDB, PostgreSQL + embedding, Redis,
Traefik). The console SPA, realtime, other workers, and function runtimes are omitted.
A dev `.env` ships non-secret defaults (HTTPS/router-protection/abuse disabled,
`localhost` whitelisted, MariaDB as the metadata store). CI
(`.github/workflows/e2e.yml`) uses exactly this on every push/PR to master.

```bash
# 1. bring it up (first run pulls images; ~2-3 min to healthy)
docker compose -f tests/e2e/appwrite/docker-compose.yml up -d

# 2. wait until the appwrite container is healthy
docker inspect --format '{{.State.Health.Status}}' appwrite   # -> "healthy"

# 3. point the tests at it (the console user is auto-created on first login)
export APPWRITE_CONSOLE_ENDPOINT=http://localhost/v1
export APPWRITE_CONSOLE_EMAIL=e2e@feedx.test
export APPWRITE_CONSOLE_PASSWORD=feedx-e2e-password

# 4. run (see below), then tear down
docker compose -f tests/e2e/appwrite/docker-compose.yml down -v
```

## Setup

```bash
uv sync --group dev                      # installs pytest + appwrite-console
# either export the APPWRITE_CONSOLE_* vars (as above), or:
cp tests/e2e/.env.e2e.example tests/e2e/.env.e2e
# edit tests/e2e/.env.e2e with your console endpoint + email + password
```

Required env (see `.env.e2e.example`): `APPWRITE_CONSOLE_ENDPOINT`,
`APPWRITE_CONSOLE_EMAIL`, `APPWRITE_CONSOLE_PASSWORD`. If these are absent the whole
e2e module **skips** (so a plain `pytest` stays green on machines with no Appwrite).

## Run

```bash
uv run pytest tests/e2e -v
```

Keep the provisioned project around for inspection with `E2E_KEEP_PROJECT=true`.

## Notes

- Appwrite creates columns asynchronously; the harness polls each table until its
  columns report `available` before inserting rows, so schema creation never races.
- `ENVIRONMENT=development` is forced during the run to disable the API's 60s
  `list_rows` cache, keeping reads immediately consistent with writes.
