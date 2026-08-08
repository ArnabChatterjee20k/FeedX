"""
End-to-end behaviour of the crawl queueing pipeline.

The queue endpoints rebuild the in-memory queues from Appwrite on every call:

    front-queue      due URLs (all `source` rows + `url` rows in QUEUED/RETRY/FETCHING)
    back-queue       those same URLs bucketed by hostname
    scheduler-queue  hostnames whose `next_allowed_at` has passed (politeness heap)

These tests assert the real flow, not just endpoint shapes: a newly created source
must appear across all three views, a discovered page must enter/leave the front
queue as its crawl_state changes, and retry must re-admit a completed page.

Depends on the shared `auth` fixture (logged-in TestClient) from conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


def _set_url_row(db, database_id, url_id, *, crawl_state, next_crawl_at):
    """Poke a URL row's state directly (simulating a worker claim/crash)."""
    db.update_row(
        database_id,
        "URL",
        row_id=url_id,
        data={"crawl_state": str(crawl_state), "next_crawl_at": next_crawl_at},
    )


def _mk_host() -> str:
    return f"q-{uuid.uuid4().hex[:12]}.example.com"


def _create(auth, *, kind: str) -> dict:
    """Create a source/url via the API and return its SourceResponse dict."""
    host = _mk_host()
    url = f"https://{host}/page"
    payload = {"url": url, "kind": kind, "source": url if kind == "source" else "api"}
    resp = auth.post("/sources", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"id": body["id"], "url": url, "hostname": host}


def _front_by_url(auth) -> dict[str, dict]:
    resp = auth.get("/front-queue")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list)
    return {row["url"]: row for row in rows}


def _back_queue(auth) -> dict:
    resp = auth.get("/back-queue")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


def _scheduler_hostnames(auth) -> set[str]:
    resp = auth.get("/scheduler-queue")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list)
    return {row["hostname"] for row in rows}


# --- a fresh source flows through all three queues --------------------------


def test_new_source_enters_front_queue(auth):
    src = _create(auth, kind="source")
    front = _front_by_url(auth)
    assert src["url"] in front, "newly created source should be due in the front queue"
    row = front[src["url"]]
    assert row["hostname"] == src["hostname"]
    assert row["kind"] == "source"
    # In queue payloads crawl_state is a NUMBER (contrast with Source endpoints,
    # where it's a string) — this locks the documented serialization difference.
    assert isinstance(row["crawl_state"], int)
    assert row["crawl_state"] == 1  # QUEUED


def test_source_is_bucketed_by_hostname_in_back_queue(auth):
    src = _create(auth, kind="source")
    back = _back_queue(auth)
    assert src["hostname"] in back, "back queue must bucket the url under its hostname"
    urls = [row["url"] for row in back[src["hostname"]]]
    assert src["url"] in urls


def test_source_hostname_appears_in_scheduler_queue(auth):
    src = _create(auth, kind="source")
    # On creation the hostname's next_allowed_at is set to now, so it is immediately
    # eligible and must show up in the politeness heap.
    assert src["hostname"] in _scheduler_hostnames(auth)


# --- discovered pages enter/leave the front queue by crawl_state ------------


def test_url_kind_page_enters_and_leaves_front_queue_by_state(auth):
    page = _create(auth, kind="url")

    # QUEUED -> present.
    assert page["url"] in _front_by_url(auth)

    # SUCCESS -> a url-kind page is no longer due, so it drops out of the front queue
    # (unlike `source` rows, which are pulled every run regardless of state).
    resp = auth.patch(f"/sources/{page['id']}", json={"crawl_state": "3"})
    assert resp.status_code == 200, resp.text
    assert page["url"] not in _front_by_url(auth)


def test_retry_readmits_completed_page_to_front_queue(auth):
    page = _create(auth, kind="url")

    # Mark it SUCCESS so it leaves the queue...
    assert (
        auth.patch(f"/sources/{page['id']}", json={"crawl_state": "3"}).status_code
        == 200
    )
    assert page["url"] not in _front_by_url(auth)

    # ...then retry: crawl_state -> QUEUED, next_crawl_at -> now, hostname re-armed.
    resp = auth.patch(f"/sources/retry/{page['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["crawl_state"] == "1"

    front = _front_by_url(auth)
    assert page["url"] in front, "retry should re-admit the page to the front queue"
    assert front[page["url"]]["crawl_state"] == 1
    # And its hostname is eligible again in the scheduler queue.
    assert page["hostname"] in _scheduler_hostnames(auth)


def test_source_survives_success_state_in_front_queue(auth):
    """`source` rows are re-crawled every run for discovery, so even a SUCCESS source
    stays due in the front queue (the worker makes the claim source-aware)."""
    src = _create(auth, kind="source")
    assert (
        auth.patch(f"/sources/{src['id']}", json={"crawl_state": "3"}).status_code
        == 200
    )
    assert src["url"] in _front_by_url(auth)


# --- claim lease / stale-FETCHING recovery ----------------------------------
# A claimed url is leased by pushing next_crawl_at into the future. A live claim is
# therefore hidden from the front queue; a crashed worker's claim (expired lease)
# resurfaces so it can be re-claimed. FETCHING == CrawlState value 2.


def test_expired_lease_fetching_is_reclaimable(auth, db, appwrite_env):
    page = _create(auth, kind="url")
    # Simulate a worker that claimed this url then crashed: FETCHING with a lease
    # that already elapsed (next_crawl_at in the past).
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _set_url_row(
        db, appwrite_env["database_id"], page["id"], crawl_state=2, next_crawl_at=past
    )
    front = _front_by_url(auth)
    assert page["url"] in front, "an expired-lease FETCHING url must be reclaimable"
    assert front[page["url"]]["crawl_state"] == 2  # still FETCHING in the payload


def test_live_lease_fetching_is_hidden(auth, db, appwrite_env):
    page = _create(auth, kind="url")
    # Simulate an in-flight claim: FETCHING with a lease still in the future.
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _set_url_row(
        db, appwrite_env["database_id"], page["id"], crawl_state=2, next_crawl_at=future
    )
    assert page["url"] not in _front_by_url(
        auth
    ), "a live-lease FETCHING url must be hidden"
