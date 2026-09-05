"""
End-to-end test harness for the FeedX API — **Appwrite only**.

The hard part of an e2e test against Appwrite is bootstrapping the account /
project / API key / database, because those are *Console* operations (they
authenticate as a Console user, not with a project API key). We do this over raw
Console HTTP (`requests`): sign up + log in a console user (reading the session
secret from the `X-Appwrite-Session` response header), create a project (teams +
`/v1/projects` on self-hosted, organizations on Cloud — auto-detected), mint a
scoped API key, then hand those credentials to the server SDK and FeedX's own
`init_database()` to build the schema. (The appwrite-console SDK is Cloud-only:
its project routes 404 on self-hosted and it hides the login response headers.)

Everything is driven through the real FastAPI app via `TestClient`, so this is a
true simulation: HTTP in, Appwrite rows out — no mocks.

Required environment (see `tests/e2e/.env.e2e.example`):

    APPWRITE_CONSOLE_ENDPOINT   e.g. http://localhost/v1  (defaults to APPWRITE_ENDPOINT)
    APPWRITE_CONSOLE_EMAIL      a console user email (created if missing)
    APPWRITE_CONSOLE_PASSWORD   that user's password (>= 8 chars)

Optional:

    APPWRITE_CONSOLE_SELF_SIGNED   "true" to allow a self-signed TLS cert
    E2E_ADMIN_PASSWORD             admin password the API checks (default: e2e-admin-pass)
    E2E_API_SECRET                 JWT signing secret          (default: e2e-secret-key)
    E2E_KEEP_PROJECT               "true" to skip project teardown (debugging)

If the console email/password are absent the whole e2e module is **skipped**, so a
plain `pytest` run on a machine without an Appwrite instance stays green.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

# Load tests/e2e/.env.e2e (gitignored) if present, so console creds can live in a
# file instead of the shell. Real environment variables still take precedence.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env.e2e", override=False)
except ImportError:
    pass


# --- config pulled from the environment -----------------------------------

CONSOLE_ENDPOINT = os.environ.get("APPWRITE_CONSOLE_ENDPOINT") or os.environ.get(
    "APPWRITE_ENDPOINT"
)
CONSOLE_EMAIL = os.environ.get("APPWRITE_CONSOLE_EMAIL")
CONSOLE_PASSWORD = os.environ.get("APPWRITE_CONSOLE_PASSWORD")
CONSOLE_SELF_SIGNED = os.environ.get(
    "APPWRITE_CONSOLE_SELF_SIGNED", "false"
).lower() == ("true")

ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "e2e-admin-pass")
API_SECRET = os.environ.get("E2E_API_SECRET", "e2e-secret-key")
KEEP_PROJECT = os.environ.get("E2E_KEEP_PROJECT", "false").lower() == "true"

# Broad DB scopes so init_database() can build tables/columns/indexes and the API
# can read/write rows. Raw strings are passed straight through by the SDK.
DB_KEY_SCOPES = [
    "databases.read",
    "databases.write",
    "collections.read",
    "collections.write",
    "attributes.read",
    "attributes.write",
    "indexes.read",
    "indexes.write",
    "documents.read",
    "documents.write",
    "tables.read",
    "tables.write",
    "rows.read",
    "rows.write",
]

# FeedX table names == pydantic model __name__.
_TABLES = ["Hostname", "URL", "Content", "CrawlRun", "Interaction", "TagScore"]


def _short_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


# --- console bootstrap (raw HTTP) -------------------------------------------
# We hit the Console API directly with `requests` instead of the appwrite-console
# SDK because that SDK targets Appwrite Cloud: it creates projects via the
# `/v1/organization[s]` routes (Cloud-only — they 404 on a self-hosted build) and it
# doesn't surface the login response headers where the session secret actually lives.
# Raw HTTP lets us read `X-Appwrite-Session` and auto-detect the project model.

APPWRITE_RESPONSE_FORMAT = "1.9.6"


def _api(method: str, url: str, headers: dict, body: dict | None = None):
    import requests

    return requests.request(
        method,
        url,
        headers=headers,
        json=body,
        verify=not CONSOLE_SELF_SIGNED,
        allow_redirects=False,
        timeout=30,
    )


def _ok(resp) -> bool:
    return 200 <= resp.status_code < 300


def _session_secret(resp) -> str:
    """Appwrite returns the session secret via header/cookie, not the JSON body."""
    header = resp.headers.get("x-appwrite-session")
    if header:
        return header
    for name, value in resp.cookies.items():
        if name.startswith("a_session_") and not name.endswith("_legacy"):
            return value
    fallback = resp.headers.get("x-fallback-cookies")
    if fallback:
        try:
            for name, value in json.loads(fallback).items():
                if name.startswith("a_session_"):
                    return value
        except Exception:
            pass
    return ""


def _console_bootstrap() -> dict[str, str]:
    """Create a console user, project, and API key over raw Console HTTP.

    Works on self-hosted (teams + `/v1/projects`) and Cloud (organizations +
    `/v1/organization/projects`) by probing for the organizations route.
    Returns a dict with endpoint / project_id / api_key / database_id / session.
    """
    endpoint = CONSOLE_ENDPOINT.rstrip("/")
    base = {
        "x-appwrite-project": "console",
        "x-appwrite-response-format": APPWRITE_RESPONSE_FORMAT,
        "content-type": "application/json",
        "accept": "application/json",
    }

    # 1. ensure a console user (first signup becomes the console owner; 409 == exists).
    signup = _api(
        "post",
        f"{endpoint}/account",
        base,
        {
            "userId": _short_id("u"),
            "email": CONSOLE_EMAIL,
            "password": CONSOLE_PASSWORD,
            "name": "E2E Runner",
        },
    )
    if not _ok(signup) and signup.status_code != 409:
        raise RuntimeError(f"account.create failed: HTTP {signup.status_code} {signup.text[:400]}")

    # 2. log in and capture the session secret from the response header/cookie.
    login = _api(
        "post",
        f"{endpoint}/account/sessions/email",
        base,
        {"email": CONSOLE_EMAIL, "password": CONSOLE_PASSWORD},
    )
    if not _ok(login):
        raise RuntimeError(f"login failed: HTTP {login.status_code} {login.text[:400]}")
    secret = _session_secret(login)
    if not secret:
        raise RuntimeError("could not read session secret from login headers/cookies")
    auth = {**base, "x-appwrite-session": secret, "cookie": f"a_session_console={secret}"}

    # 3. create a project, detecting the server's model.
    project_id = _short_id("prj")
    orgs = _api("get", f"{endpoint}/organizations", auth)
    if _ok(orgs):
        # Cloud-style organizations.
        items = orgs.json().get("organizations") or orgs.json().get("teams") or []
        if items:
            org_id = items[0]["$id"]
        else:
            org_id = _short_id("org")
            r = _api(
                "post",
                f"{endpoint}/organizations",
                auth,
                {"organizationId": org_id, "name": "E2E Org", "billingPlan": "tier-0"},
            )
            if not _ok(r):
                raise RuntimeError(f"organizations.create failed: HTTP {r.status_code} {r.text[:400]}")
        proj = _api(
            "post",
            f"{endpoint}/organization/projects",
            {**auth, "x-appwrite-organization": org_id},
            {"projectId": project_id, "name": "feedx-e2e"},
        )
        if not _ok(proj):
            raise RuntimeError(f"organization.create_project failed: HTTP {proj.status_code} {proj.text[:400]}")
    else:
        # Self-hosted: projects belong to a team, created via /v1/projects.
        team = _api("post", f"{endpoint}/teams", auth, {"teamId": _short_id("team"), "name": "E2E Team"})
        if not _ok(team):
            raise RuntimeError(f"teams.create failed: HTTP {team.status_code} {team.text[:400]}")
        team_id = team.json()["$id"]
        proj = None
        errors = []
        for body in (
            {"projectId": project_id, "name": "feedx-e2e", "teamId": team_id, "region": "default"},
            {"projectId": project_id, "name": "feedx-e2e", "teamId": team_id},
        ):
            r = _api("post", f"{endpoint}/projects", auth, body)
            if _ok(r):
                proj = r
                break
            errors.append(f"HTTP {r.status_code} {r.text[:200]}")
        if proj is None:
            raise RuntimeError("projects.create failed:\n" + "\n".join(errors))
    project_id = proj.json().get("$id", project_id)

    # 4. mint an API key (project-scoped route, with a project-header fallback).
    # keyId is required, the same way projectId and teamId are above.
    key_body = {
        "keyId": _short_id("key"),
        "name": "feedx-e2e-key",
        "scopes": DB_KEY_SCOPES,
    }
    key = None
    errors = []
    for path, headers in (
        (f"/projects/{project_id}/keys", auth),
        ("/project/keys", {**auth, "x-appwrite-project": project_id}),
    ):
        r = _api("post", f"{endpoint}{path}", headers, key_body)
        if _ok(r):
            key = r
            break
        errors.append(f"{path}: HTTP {r.status_code} {r.text[:200]}")
    if key is None:
        raise RuntimeError("keys.create failed:\n" + "\n".join(errors))
    api_key = key.json().get("secret")
    if not api_key:
        raise RuntimeError(f"keys.create returned no secret: {key.text[:300]}")

    return {
        "endpoint": endpoint,
        "project_id": project_id,
        "api_key": api_key,
        "database_id": _short_id("db"),
        "session": secret,
    }


def _delete_project(cfg: dict) -> None:
    """Best-effort project teardown (the whole stack is torn down in CI anyway)."""
    endpoint = cfg["endpoint"]
    auth = {
        "x-appwrite-project": "console",
        "x-appwrite-response-format": APPWRITE_RESPONSE_FORMAT,
        "content-type": "application/json",
        "accept": "application/json",
        "x-appwrite-session": cfg.get("session", ""),
        "cookie": f"a_session_console={cfg.get('session', '')}",
    }
    pid = cfg["project_id"]
    for path in (f"/projects/{pid}", f"/organization/projects/{pid}"):
        try:
            if _ok(_api("delete", f"{endpoint}{path}", auth)):
                return
        except Exception:
            pass


# --- schema readiness -------------------------------------------------------


def _wait_for_tables_ready(
    get_database, database_id: str, timeout: float = 120.0
) -> None:
    """Appwrite creates columns asynchronously. Block until every table's columns
    report status == 'available' so row inserts don't race the schema."""
    database = get_database()
    deadline = time.time() + timeout
    pending = list(_TABLES)
    while pending and time.time() < deadline:
        still_pending = []
        for table in pending:
            try:
                cols = database.list_columns(database_id, table)
                columns = cols["columns"] if isinstance(cols, dict) else cols.columns
                statuses = [
                    (
                        c.get("status")
                        if isinstance(c, dict)
                        else getattr(c, "status", None)
                    )
                    for c in columns
                ]
                # the sdk returns a ColumnStatus enum, not a str, and it is not a
                # str-enum - comparing it directly never matches
                if columns and all(
                    getattr(s, "value", s) == "available" for s in statuses
                ):
                    continue  # this table is ready
            except Exception:
                pass
            still_pending.append(table)
        pending = still_pending
        if pending:
            time.sleep(2)
    if pending:
        raise TimeoutError(f"tables not ready after {timeout}s: {pending}")


# --- the session fixture every e2e test depends on --------------------------


@pytest.fixture(scope="session")
def appwrite_env():
    """Bootstrap Appwrite, wire env vars, build the schema, yield the config.

    Yields a dict: {endpoint, project_id, api_key, database_id, admin_password}.
    Tears the project down at the end (unless E2E_KEEP_PROJECT=true).
    """
    if not (CONSOLE_ENDPOINT and CONSOLE_EMAIL and CONSOLE_PASSWORD):
        pytest.skip(
            "console credentials missing — set APPWRITE_CONSOLE_ENDPOINT/EMAIL/PASSWORD"
        )

    cfg = _console_bootstrap()

    # These must be set BEFORE importing src.database / src.api, because those
    # modules read APPWRITE_* / API_SECRET at import time.
    os.environ["APPWRITE_ENDPOINT"] = cfg["endpoint"]
    os.environ["APPWRITE_PROJECT_ID"] = cfg["project_id"]
    os.environ["APPWRITE_API_KEY"] = cfg["api_key"]
    os.environ["APPWRITE_DATABASE_ID"] = cfg["database_id"]
    os.environ["ADMIN_API_PASSWORD"] = ADMIN_PASSWORD
    os.environ["API_SECRET"] = API_SECRET
    # development => the API's 60s list_rows cache is disabled, so reads are fresh.
    os.environ["ENVIRONMENT"] = "development"

    from src.database import init_database, get_database

    init_database()
    _wait_for_tables_ready(get_database, cfg["database_id"])

    cfg["admin_password"] = ADMIN_PASSWORD
    try:
        yield cfg
    finally:
        if not KEEP_PROJECT:
            _delete_project(cfg)


@pytest.fixture(scope="session")
def client(appwrite_env):
    """A logged-out FastAPI TestClient bound to the bootstrapped Appwrite project."""
    from fastapi.testclient import TestClient
    from src.api import create_api

    with TestClient(create_api()) as test_client:
        yield test_client


@pytest.fixture()
def db(appwrite_env):
    """Raw Appwrite TablesDB handle for seeding/asserting rows directly."""
    from src.database import get_database

    return get_database()


@pytest.fixture(scope="session")
def auth(client, appwrite_env):
    """The shared TestClient, logged in as admin (cookie persists on the client)."""
    resp = client.post("/login", json={"password": appwrite_env["admin_password"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"
    return client
