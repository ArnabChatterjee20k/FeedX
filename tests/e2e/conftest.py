"""
End-to-end test harness for the FeedX API — **Appwrite only**.

The hard part of an e2e test against Appwrite is bootstrapping the account /
project / API key / database, because those are *Console* operations (they
authenticate as a Console user, not with a project API key). We solve that with
the Appwrite **Console** Python SDK (`appwrite-console`, imported as
`appwrite_console`): log in a console user with email+password, create a project,
mint a scoped API key, then hand those credentials to the regular server SDK and
FeedX's own `init_database()` to build the schema.

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


# --- console bootstrap ------------------------------------------------------


def _console_bootstrap() -> dict[str, str]:
    """Create (or reuse) a project + API key + database via the Console SDK.

    Returns a dict with endpoint / project_id / api_key / database_id.
    """
    from appwrite_console.client import Client
    from appwrite_console.services.account import Account
    from appwrite_console.services.organizations import Organizations
    from appwrite_console.services.organization import Organization
    from appwrite_console.services.project import Project

    def _attr(obj, name, default=None):
        """Read a field off a pydantic model *or* a plain dict response."""
        if isinstance(obj, dict):
            return obj.get(name, obj.get(f"${name}", default))
        return getattr(obj, name, default)

    client = Client().set_endpoint(CONSOLE_ENDPOINT).set_project("console")
    if CONSOLE_SELF_SIGNED:
        client.set_self_signed(True)

    account = Account(client)

    # 1. ensure the console user exists (409 if already there -> ignore), then log in.
    try:
        account.create(_short_id("u"), CONSOLE_EMAIL, CONSOLE_PASSWORD, "E2E Runner")
    except Exception:
        pass
    session = account.create_email_password_session(CONSOLE_EMAIL, CONSOLE_PASSWORD)
    client.set_session(_attr(session, "secret"))

    # 2. reuse the first organization, or create one (self-hosted free tier).
    orgs = Organizations(client)
    listing = orgs.list()
    org_items = _attr(listing, "organizations") or _attr(listing, "teams") or []
    if org_items:
        org_id = _attr(org_items[0], "id") or _attr(org_items[0], "$id")
    else:
        org_id = _short_id("org")
        orgs.create(org_id, "E2E Org", billing_plan="tier-0")

    # 3. create a fresh project inside that org (org scoped via header).
    client.set_organization(org_id)
    org = Organization(client)
    project_id = _short_id("prj")
    project = org.create_project(project_id, "feedx-e2e")
    project_id = _attr(project, "id") or project_id

    # 4. mint an API key — Project.create_key is scoped by X-Appwrite-Project,
    #    so point the client at the new project first.
    client.set_project(project_id)
    project_service = Project(client)
    key = project_service.create_key(_short_id("key"), "feedx-e2e-key", DB_KEY_SCOPES)
    api_key = _attr(key, "secret")
    if not api_key:
        raise RuntimeError("console create_key returned no secret")

    return {
        "endpoint": CONSOLE_ENDPOINT,
        "project_id": project_id,
        "api_key": api_key,
        "database_id": _short_id("db"),
    }


def _delete_project(project_id: str) -> None:
    from appwrite_console.client import Client
    from appwrite_console.services.account import Account
    from appwrite_console.services.project import Project

    try:
        client = Client().set_endpoint(CONSOLE_ENDPOINT).set_project("console")
        if CONSOLE_SELF_SIGNED:
            client.set_self_signed(True)
        session = Account(client).create_email_password_session(
            CONSOLE_EMAIL, CONSOLE_PASSWORD
        )
        secret = session.secret if hasattr(session, "secret") else session["secret"]
        client.set_session(secret).set_project(project_id)
        Project(client).delete()
    except Exception as exc:  # teardown is best-effort
        print(f"[e2e] project teardown failed for {project_id}: {exc}")


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
                if columns and all(s == "available" for s in statuses):
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
    try:
        import appwrite_console  # noqa: F401
    except ImportError:
        pytest.skip("appwrite-console not installed (uv sync --group dev)")

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
            _delete_project(cfg["project_id"])


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
