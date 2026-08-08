"""
End-to-end tests for the worker's claim + lease logic (crawl_worker._claim /
_update_state), exercised against the real Appwrite DB.

Both methods are stateless w.r.t. the worker instance (they never touch `self`), so
we call them unbound as `CrawlWorker._claim(None, ...)` — no browser/Scout needed.

All `src.*` imports are lazy (inside the helpers/tests) so they happen only after the
`appwrite_env` fixture has set the APPWRITE_* env and imported src.database first;
importing at module top would bind the DB config to None at collection time.

Covers:
  - a fresh QUEUED url is claimed -> FETCHING with next_crawl_at leased into the future
  - the claim is atomic: a second claim on a live lease returns (False, None)
  - an expired-lease FETCHING url is re-claimable (crash recovery)
  - a live-lease FETCHING url is not claimable
  - url-kind never claims SUCCESS/terminal rows
  - RETRY clears the lease (next_crawl_at -> now) so retry isn't blocked by it
  - source claims recur from any state, still gated by the live lease
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

# CrawlState values.
QUEUED, FETCHING, SUCCESS, RETRY = 1, 2, 3, 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    s = value.replace("Z", "+00:00") if isinstance(value, str) else value
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _claim(url_id, kind):
    from src.workers.crawl_worker import CrawlWorker

    return CrawlWorker._claim(None, url_id, kind)  # self is unused


def _update_state(url_id, state_value):
    from src.workers.crawl_worker import CrawlWorker
    from src.database.models import CrawlState

    return CrawlWorker._update_state(None, url_id, CrawlState(state_value))


def _lease_seconds() -> int:
    from src.workers.crawl_worker import CLAIM_LEASE_SECONDS

    return CLAIM_LEASE_SECONDS


def _create_url(auth, kind="url") -> dict:
    host = f"c-{uuid.uuid4().hex[:12]}.example.com"
    url = f"https://{host}/p"
    payload = {"url": url, "kind": kind, "source": url if kind == "source" else "api"}
    resp = auth.post("/sources", json=payload)
    assert resp.status_code == 200, resp.text
    return {"id": resp.json()["id"], "url": url, "hostname": host}


def _set_url_row(db, database_id, url_id, *, crawl_state, next_crawl_at):
    db.update_row(
        database_id,
        "URL",
        row_id=url_id,
        data={"crawl_state": str(crawl_state), "next_crawl_at": next_crawl_at},
    )


def _row(db, database_id, url_id) -> dict:
    return db.get_row(database_id, "URL", row_id=url_id).data


# --- fresh claim + lease ----------------------------------------------------


def test_claim_fresh_url_leases_and_marks_fetching(auth, db, appwrite_env):
    dbid = appwrite_env["database_id"]
    page = _create_url(auth, "url")

    ok, err = _claim(page["id"], "url")
    assert ok is True and err is None

    row = _row(db, dbid, page["id"])
    assert str(row["crawl_state"]) == str(FETCHING)
    # next_crawl_at pushed roughly a full lease into the future.
    assert _parse(row["next_crawl_at"]) > _now() + timedelta(seconds=_lease_seconds() / 2)


def test_second_claim_on_live_lease_is_rejected(auth):
    """Atomicity: once claimed (live lease), a racing claim gets (False, None) —
    0 rows updated, not an error."""
    page = _create_url(auth, "url")
    ok1, err1 = _claim(page["id"], "url")
    assert ok1 is True and err1 is None

    ok2, err2 = _claim(page["id"], "url")
    assert ok2 is False and err2 is None


# --- stale-FETCHING recovery via the lease ----------------------------------


def test_claim_recovers_expired_lease_fetching(auth, db, appwrite_env):
    dbid = appwrite_env["database_id"]
    page = _create_url(auth, "url")
    past = (_now() - timedelta(minutes=5)).isoformat()
    _set_url_row(db, dbid, page["id"], crawl_state=FETCHING, next_crawl_at=past)

    ok, err = _claim(page["id"], "url")
    assert ok is True and err is None
    assert _parse(_row(db, dbid, page["id"])["next_crawl_at"]) > _now()


def test_claim_blocked_by_live_lease_fetching(auth, db, appwrite_env):
    dbid = appwrite_env["database_id"]
    page = _create_url(auth, "url")
    future = (_now() + timedelta(hours=1)).isoformat()
    _set_url_row(db, dbid, page["id"], crawl_state=FETCHING, next_crawl_at=future)

    ok, err = _claim(page["id"], "url")
    assert ok is False and err is None


def test_url_kind_claim_skips_terminal_state(auth, db, appwrite_env):
    """Even due (next_crawl_at in the past), a SUCCESS url-kind row is never claimed —
    only QUEUED/RETRY/FETCHING are eligible."""
    dbid = appwrite_env["database_id"]
    page = _create_url(auth, "url")
    past = (_now() - timedelta(minutes=5)).isoformat()
    _set_url_row(db, dbid, page["id"], crawl_state=SUCCESS, next_crawl_at=past)

    ok, err = _claim(page["id"], "url")
    assert ok is False and err is None


# --- RETRY clears the lease -------------------------------------------------


def test_retry_state_clears_lease_and_is_reclaimable(auth, db, appwrite_env):
    dbid = appwrite_env["database_id"]
    page = _create_url(auth, "url")

    # claim -> FETCHING with a future lease
    assert _claim(page["id"], "url")[0] is True
    leased = _parse(_row(db, dbid, page["id"])["next_crawl_at"])
    assert leased > _now() + timedelta(seconds=_lease_seconds() / 2)

    # worker error path: RETRY must reset next_crawl_at to ~now, not inherit the lease
    ok, err = _update_state(page["id"], RETRY)
    assert ok is True and err is None
    row = _row(db, dbid, page["id"])
    assert str(row["crawl_state"]) == str(RETRY)
    assert _parse(row["next_crawl_at"]) <= _now() + timedelta(seconds=60)

    # ...so it can be claimed again right away
    ok2, err2 = _claim(page["id"], "url")
    assert ok2 is True and err2 is None


# --- source claim semantics -------------------------------------------------


def test_source_claim_recurs_from_success(auth, db, appwrite_env):
    """A source recurs: once due again (next_crawl_at in the past) it is claimable
    from any state, including SUCCESS."""
    dbid = appwrite_env["database_id"]
    src = _create_url(auth, "source")
    past = (_now() - timedelta(minutes=1)).isoformat()
    _set_url_row(db, dbid, src["id"], crawl_state=SUCCESS, next_crawl_at=past)

    ok, err = _claim(src["id"], "source")
    assert ok is True and err is None
    assert str(_row(db, dbid, src["id"])["crawl_state"]) == str(FETCHING)


def test_source_claim_blocked_by_live_lease(auth, db, appwrite_env):
    dbid = appwrite_env["database_id"]
    src = _create_url(auth, "source")
    future = (_now() + timedelta(hours=1)).isoformat()
    _set_url_row(db, dbid, src["id"], crawl_state=FETCHING, next_crawl_at=future)

    ok, err = _claim(src["id"], "source")
    assert ok is False and err is None
