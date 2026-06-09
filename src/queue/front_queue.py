from collections import deque
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import URL, Hostname, CrawlState
from appwrite.query import Query
from datetime import datetime, timezone


class FrontQueue:
    def __init__(self):
        self._queue = deque()

    def init(self) -> None:
        self._queue.extend(self._get_all_urls())

    def push(self, url:URL) -> None: ...

    def pop(self) -> None: ...

    def _get_all_urls(self) -> list[URL]:
        database = get_database()
        # not using (next_crawl_at <= now() or next_crawl_at is null) as the query as mysql(appwrite) will start doing full table scan for this
        queries = [
            Query.less_than_equal(
                "next_crawl_at", datetime.now(timezone.utc).isoformat()
            ),
            Query.or_queries(
                [
                    Query.equal("crawl_state", str(CrawlState.QUEUED.value)),
                    Query.equal("crawl_state", str(CrawlState.RETRY.value)),
                ]
            ),
        ]
        rows = database.list_rows(
            database_id=APPWRITE_DATABASE_ID,
            table_id=URL.__name__,
            queries=queries,
            total="false",
            model_type=URL,
        )

        return [row.data for row in rows.rows]
