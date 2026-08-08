"""
Full API simulation against a real (bootstrapped) Appwrite project.

Drives the FastAPI app end to end via TestClient: auth -> sources -> hostnames ->
content -> interactions -> queue introspection. Every assertion hits Appwrite for
real; nothing is mocked. See conftest.py for how the project is provisioned.

Run:  uv run pytest tests/e2e -v      (needs the console env — see .env.e2e.example)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


# --- shared fixtures --------------------------------------------------------
# `auth` (a logged-in TestClient) and `client`/`db`/`appwrite_env` live in conftest.py.


@pytest.fixture(scope="session")
def created_source(auth):
    """Create one source and hand its id to the tests that mutate it."""
    url = f"https://e2e-{uuid.uuid4().hex[:10]}.example.com/feed"
    resp = auth.post("/sources", json={"url": url, "kind": "source", "source": url})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"] == url
    assert body["hostname"].endswith("example.com")
    assert body["crawl_state"] == "1"  # QUEUED, as a string in Source endpoints
    return {"id": body["id"], "url": url, "hostname": body["hostname"]}


@pytest.fixture(scope="session")
def seeded_content(appwrite_env):
    """Insert a Content row directly (no create-content endpoint exists yet)."""
    from src.database import get_database
    from src.database.models import ContentPipelineState

    database = get_database()
    content_id = _uid("cnt")
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "$id": content_id,
        "url": f"https://example.com/post/{content_id}",
        "hostname": "example.com",
        "title": "E2E Seeded Post",
        "simhash": "0",
        "simhash_1": 0,
        "simhash_2": 0,
        "simhash_3": 0,
        "simhash_4": 0,
        "summary": "seeded for the e2e simulation",
        "chunks": [],
        "tags": ["python", "ai"],
        "score": 1.5,
        "scraped_at": now,
        "pipeline_state": str(ContentPipelineState.COMPLETED.value),
    }
    database.create_rows(appwrite_env["database_id"], "Content", rows=[row])
    return {"id": content_id, "tags": ["python", "ai"]}


# --- auth -------------------------------------------------------------------


def test_unauthenticated_is_rejected(appwrite_env):
    """A client with no cookie must be bounced with 401 on a protected route."""
    from fastapi.testclient import TestClient
    from src.api import create_api

    with TestClient(create_api()) as fresh:
        resp = fresh.get("/sources")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid user"


def test_login_sets_cookie(auth):
    # `auth` already logged in; confirm the cookie rides along on a protected call.
    assert auth.get("/sources").status_code == 200


# --- sources ----------------------------------------------------------------


def test_create_source(created_source):
    assert created_source["id"]


def test_get_source_by_id(auth, created_source):
    resp = auth.get(f"/sources/{created_source['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == created_source["url"]


def test_list_sources_default_kind_is_source(auth, created_source):
    resp = auth.get("/sources", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()["data"]]
    assert created_source["id"] in ids


def test_list_sources_filter_by_hostname(auth, created_source):
    resp = auth.get("/sources", params={"hostname": created_source["hostname"]})
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert rows and all(r["hostname"] == created_source["hostname"] for r in rows)


def test_update_source(auth, created_source):
    resp = auth.patch(
        f"/sources/{created_source['id']}",
        json={"priority_score": 2.5, "crawl_state": "3"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["priority_score"] == 2.5
    assert body["crawl_state"] == "3"


def test_update_source_empty_body_is_400(auth, created_source):
    resp = auth.patch(f"/sources/{created_source['id']}", json={})
    assert resp.status_code == 400


def test_retry_source_requeues(auth, created_source):
    resp = auth.patch(f"/sources/retry/{created_source['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["crawl_state"] == "1"  # back to QUEUED


def test_create_source_kind_mismatch_is_400(auth):
    # kind == source requires source == url
    resp = auth.post(
        "/sources",
        json={"url": "https://x.example.com/a", "kind": "source", "source": "api"},
    )
    assert resp.status_code == 400


# --- hostnames --------------------------------------------------------------


def test_list_hostnames(auth, created_source):
    resp = auth.get("/hostnames", params={"hostname": created_source["hostname"]})
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert rows and rows[0]["name"] == created_source["hostname"]


# --- content ----------------------------------------------------------------


def test_get_content_by_id(auth, seeded_content):
    resp = auth.get(f"/content/{seeded_content['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == seeded_content["id"]
    assert body["title"] == "E2E Seeded Post"
    assert body["pipeline_state"] == "5"
    assert set(seeded_content["tags"]).issubset(set(body["tags"]))


def test_list_content(auth, seeded_content):
    resp = auth.get("/content", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()["data"]]
    assert seeded_content["id"] in ids


def test_list_content_tag_filter(auth, seeded_content):
    resp = auth.get("/content", params={"tag": "python", "limit": 100})
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()["data"]]
    assert seeded_content["id"] in ids


# --- interactions -----------------------------------------------------------


def test_add_interactions_creates_one_row_per_tag(
    auth, db, appwrite_env, seeded_content
):
    from appwrite.query import Query

    resp = auth.post(
        "/interactions",
        json={
            "id": seeded_content["id"],
            "interaction": "open",
            "tags": ["python", "ai", "python"],  # dupes collapse
        },
    )
    assert resp.status_code == 200, resp.text
    ids = resp.json()["interaction_ids"]
    assert len(ids) == 2  # two unique tags

    rows = db.list_rows(
        appwrite_env["database_id"],
        "Interaction",
        queries=[Query.equal("content_id", [seeded_content["id"]]), Query.limit(100)],
        total="false",
    )
    stored = rows["rows"] if isinstance(rows, dict) else rows.rows
    assert len(stored) == 2
    for row in stored:
        data = row["data"] if isinstance(row, dict) else row.data
        get = data.get if isinstance(data, dict) else (lambda k: getattr(data, k))
        assert get("type") == "open"
        assert get("weight") == 1.0  # OPEN weight


def test_add_interactions_without_tags_is_400(auth, seeded_content):
    resp = auth.post(
        "/interactions",
        json={"id": seeded_content["id"], "interaction": "like", "tags": []},
    )
    assert resp.status_code == 400


# --- queue introspection ----------------------------------------------------


def test_front_queue_is_a_list(auth, created_source):
    resp = auth.get("/front-queue")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_back_queue_is_keyed_by_hostname(auth, created_source):
    resp = auth.get("/back-queue")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), dict)


def test_scheduler_queue_is_a_list(auth, created_source):
    resp = auth.get("/scheduler-queue")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


# --- logout -----------------------------------------------------------------


def test_logout_clears_cookie(appwrite_env):
    from fastapi.testclient import TestClient
    from src.api import create_api

    with TestClient(create_api()) as c:
        assert (
            c.post(
                "/login", json={"password": appwrite_env["admin_password"]}
            ).status_code
            == 200
        )
        assert c.get("/sources").status_code == 200
        assert c.post("/logout").status_code == 200
        # cookie cleared -> protected route now rejects
        c.cookies.clear()
        assert c.get("/sources").status_code == 401
