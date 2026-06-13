import heapq
from .models import SchedulerQueueItem
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import Hostname
from appwrite.query import Query
from scout.logger import get_logger
from datetime import datetime, timezone


class SchedulerQueue:
    def __init__(self):
        self._queue: list[SchedulerQueueItem] = []
        self._logger = get_logger("SchedulerQueue")

    def init(self, hostnames: list[str]):
        try:
            hostnames = self._get_hostname_items(hostnames)
            heapq.heapify(hostnames)
            self._queue.extend(hostnames)

            self._logger.info(f"Pushed {len(hostnames)} urls", tag="INIT")
        except Exception as e:
            self._logger.error(f"Error", tag="INIT", error=e)

    def push(self, item: SchedulerQueueItem):
        heapq.heappush(self._queue, item)

    def pop(self):
        if not self._queue:
            return None
        return heapq.heappop(self._queue)

    def _get_hostname_items(self, hostnames: list[str]):
        database = get_database()
        queries = [
            Query.equal("name", hostnames),
            Query.less_than_equal(
                "next_crawl_at", datetime.now(timezone.utc).isoformat()
            ),
            Query.select(["name", "next_allowed_at"]),
        ]
        rows = database.list_rows(APPWRITE_DATABASE_ID, Hostname.__name__, queries)
        return [
            SchedulerQueueItem(
                hostname=row.model_dump().get("name"),
                next_allowed_at=row.model_dump().get("next_allowed_at"),
            )
            for row in rows.rows
        ]
