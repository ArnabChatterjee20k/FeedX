from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from appwrite.query import Query

from . import get_database, APPWRITE_DATABASE_ID
from .models import URL, Hostname, Content, CrawlState, ContentPipelineState

PAGE_SIZE = 100

_URL_STATE_RANK = {
    CrawlState.SUCCESS.value: 5,
    CrawlState.RETRY.value: 4,
    CrawlState.FETCHING.value: 3,
    CrawlState.BLOCKED.value: 2,
    CrawlState.FAILED.value: 1,
    CrawlState.QUEUED.value: 0,
}

_CONTENT_STATE_RANK = {
    ContentPipelineState.COMPLETED.value: 4,
    ContentPipelineState.SUMMARIZING.value: 3,
    ContentPipelineState.EXTRACTING.value: 3,
    ContentPipelineState.TAGGING.value: 3,
    ContentPipelineState.PENDING.value: 2,
    ContentPipelineState.FAILED.value: 1,
}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _created(row: dict) -> datetime:
    raw = row.get("$createdAt")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _url_rank(row: dict) -> tuple:
    return (
        1 if row.get("kind") == "source" else 0,
        _URL_STATE_RANK.get(_as_int(row.get("crawl_state")), 0),
        -_created(row).timestamp(),
    )


def _hostname_rank(row: dict) -> tuple:
    return (
        _as_int(row.get("crawl_count")),
        _as_int(row.get("success_count")),
        -_created(row).timestamp(),
    )


def _content_rank(row: dict) -> tuple:
    return (
        _CONTENT_STATE_RANK.get(_as_int(row.get("pipeline_state")), 0),
        1 if row.get("summary") else 0,
        -_created(row).timestamp(),
    )


def _fetch_all(table: str, columns: list[str]) -> list[dict]:
    database = get_database()
    rows: list[dict] = []
    cursor = None
    while True:
        queries = [Query.select(columns), Query.limit(PAGE_SIZE)]
        if cursor:
            queries.append(Query.cursor_after(cursor))
        page = database.list_rows(
            APPWRITE_DATABASE_ID, table, queries, total="false"
        ).rows
        if not page:
            break
        for row in page:
            data = dict(row.data)
            data["$id"] = row.id
            data["$createdAt"] = row.createdat
            rows.append(data)
        if len(page) < PAGE_SIZE:
            break
        cursor = page[-1].id
    return rows


def _plan(
    rows: list[dict], key: str, rank: Callable[[dict], tuple]
) -> tuple[dict[str, list[dict]], list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)

    duplicated = {k: v for k, v in grouped.items() if len(v) > 1}
    doomed: list[dict] = []
    for group in duplicated.values():
        group.sort(key=rank, reverse=True)
        doomed.extend(group[1:])
    return duplicated, doomed


DELETE_BATCH = 50


def _delete(table: str, rows: Iterable[dict]) -> tuple[int, list[str]]:
    database = get_database()
    ids = [row["$id"] for row in rows]
    deleted = 0
    errors: list[str] = []

    for start in range(0, len(ids), DELETE_BATCH):
        batch = ids[start : start + DELETE_BATCH]
        if not batch:
            # delete_rows with no query deletes the whole table
            continue
        try:
            database.delete_rows(
                APPWRITE_DATABASE_ID, table, queries=[Query.equal("$id", batch)]
            )
            deleted += len(batch)
        except Exception as e:
            errors.append(f"{table} batch at {start}: {e}")
    return deleted, errors


TABLES: list[tuple[str, str, list[str], Callable[[dict], tuple]]] = [
    (URL.__name__, "url", ["url", "hostname", "kind", "crawl_state"], _url_rank),
    (
        Hostname.__name__,
        "name",
        ["name", "crawl_count", "success_count", "last_crawled_at"],
        _hostname_rank,
    ),
    (
        Content.__name__,
        "url",
        ["url", "hostname", "pipeline_state", "summary"],
        _content_rank,
    ),
]


def dedupe(apply: bool = False, tables: list[str] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"apply": apply, "tables": {}}

    for table, key, columns, rank in TABLES:
        if tables and table not in tables:
            continue

        rows = _fetch_all(table, columns)
        duplicated, doomed = _plan(rows, key, rank)

        entry = {
            "rows": len(rows),
            "distinct": len(rows) - len(doomed),
            "duplicated_values": len(duplicated),
            "to_delete": len(doomed),
            "worst": sorted(
                ((len(g), v) for v, g in duplicated.items()), reverse=True
            )[:5],
            "deleted": 0,
            "errors": [],
        }

        if apply and doomed:
            deleted, errors = _delete(table, doomed)
            entry["deleted"] = deleted
            entry["errors"] = errors

        report["tables"][table] = entry

    return report
