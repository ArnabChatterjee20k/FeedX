import heapq
from .models import SchedulerQueueItem
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import Hostname
from appwrite.query import Query
from scout.logger import get_logger
from datetime import datetime, timezone
from . import Queue
import asyncio


class SchedulerQueue(Queue):
    def __init__(self):
        self._queue: list[SchedulerQueueItem] = []
        self._logger = get_logger("SchedulerQueue")
        self._hostname_available_condition = asyncio.Condition()

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

    async def push_async(
        self, item: SchedulerQueueItem
    ) -> tuple[bool, None | Exception]:
        try:
            async with self._hostname_available_condition:
                self.push(item)
                self._hostname_available_condition.notify(1)
            return True, None
        except Exception as e:
            return False, e

    def pop(self):
        if not self._queue:
            return None
        return heapq.heappop(self._queue)

    async def pop_async(self, timeout=5):
        try:
            async with self._hostname_available_condition:
                await asyncio.wait_for(
                    self._hostname_available_condition.wait_for(
                        lambda: len(self._queue) > 0
                    ),
                    timeout=timeout,
                )

                # not using the condition notify here cause what if notify in that case isn't called? better to use sleep since its totally based on duration
                delay = (
                    self._queue[0].next_allowed_at - datetime.now(timezone.utc)
                ).total_seconds()

                # popping it so that its not get used by the other worker, but
                # only once we know we can wait it out here — a pop we then drop
                # loses the hostname from the heap for the rest of the run
                item = self.pop() if delay <= timeout else None

            # releasing lock so that the item isn't blocked
            if item is None:
                await asyncio.sleep(timeout)
                return None

            if delay > 0:
                await asyncio.sleep(delay)
            return item

        except asyncio.TimeoutError:
            return None

    def _get_hostname_items(self, hostnames: list[str]):
        database = get_database()
        now = datetime.now(timezone.utc).isoformat()
        # appwrite caps how many values a single equal() may carry and defaults an
        # unbounded list_rows to 25 rows — without both the chunking and the explicit
        # limit below, hosts past the 25th silently never get scheduled at all.
        batch_size = 50
        batches = [
            hostnames[i : i + batch_size]
            for i in range(0, len(hostnames), batch_size)
        ]
        by_hostname: dict[str, SchedulerQueueItem] = {}
        for batch in batches:
            queries = [
                Query.equal("name", batch),
                Query.less_than_equal("next_allowed_at", now),
                Query.select(["name", "next_allowed_at"]),
                Query.order_asc("next_allowed_at"),
                Query.limit(batch_size),
            ]
            rows = database.list_rows(
                APPWRITE_DATABASE_ID, Hostname.__name__, queries, total="false"
            )
            for row in rows.rows:
                name = row.data.get("name")
                item = SchedulerQueueItem(
                    id=row.id,
                    hostname=name,
                    next_allowed_at=datetime.fromisoformat(
                        row.data.get("next_allowed_at")
                    ),
                )
                existing = by_hostname.get(name)
                if existing is None:
                    by_hostname[name] = item
                    continue
                self._logger.info(
                    f"Duplicate hostname row for {name}, keeping the later cooldown",
                    tag="DUPLICATE_HOSTNAME",
                )
                if item.next_allowed_at > existing.next_allowed_at:
                    by_hostname[name] = item

        return list(by_hostname.values())
