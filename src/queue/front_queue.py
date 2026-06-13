from collections import deque
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import URL, Hostname, CrawlState
from appwrite.query import Query
from datetime import datetime, timezone
from scout.logger import get_logger
import os
from .models import URLRow


class FrontQueue:
    def __init__(self):
        # not using priority score for now, so using a normal deque and not a priority queue
        self._queue: deque[URLRow] = deque()
        self._logger = get_logger("FRONT_QUEUE")

    def init(self) -> None:
        try:
            self._queue.extend(self._get_all_urls())
            self._logger.info(f"Init queue of {len(self._queue)}", tag="INIT")
        except Exception as e:
            self._logger.error("Error during init", tag="INIT", error=e)

    def push(self, urls: list[URL]) -> bool:
        try:
            database = get_database()
            urls = list(
                map(
                    lambda url: {
                        **url.model_dump(),
                        "crawl_state": url.crawl_state.value,
                    },
                    urls,
                )
            )
            result = database.create_rows(APPWRITE_DATABASE_ID, URL.__name__, rows=urls)
            self._logger.info(f"Pushed {result.total} items", tag="PUSH")
            self._queue.extend(urls)
            return True
        except Exception as e:
            self._logger.error("Error during push", tag="PUSH", error=e)
            return False

    def pop(self) -> URL | None:
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._logger.info(f"Popped url {item.id}", tag="POP")
        return item

    def _get_all_urls(self) -> list[URL]:
        database = get_database()
        limit = os.environ.get("FRONT_QUEUE_INIT_LIMIT", 1000)
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
            Query.limit(limit),
        ]
        rows = database.list_rows(
            database_id=APPWRITE_DATABASE_ID,
            table_id=URL.__name__,
            queries=queries,
            total="false",
            model_type=URL,
        )
        return [
            URLRow(**row.data.model_dump(), id=row.id, sequence=row.sequence)
            for row in rows.rows
        ]
