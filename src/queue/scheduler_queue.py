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

                if self._queue[0].next_allowed_at <= datetime.now(timezone.utc):
                    return self.pop()

                # not using the condition notify here cause what if notify in that case isn't called? better to use sleep since its totally based on duration
                # wait_timeout = min(
                #     timeout,
                #     (
                #         self._queue[0].next_allowed_at - datetime.now(timezone.utc)
                #     ).total_seconds(),
                # )
                # await asyncio.wait_for(
                #     self._hostname_available_condition.wait_for(
                #         lambda: self._queue[0].next_allowed_at
                #         <= datetime.now(timezone.utc)
                #     ),
                #     timeout=wait_timeout,
                # )
                # return self.pop()

                # popping it so that its not get used by the other worker
                # if any issues happened then the database already has the item in current state and can be peaked
                item_for_the_current_worker = self.pop()
                if not self._queue:
                    return item_for_the_current_worker
                delay = (
                    self._queue[0].next_allowed_at - datetime.now(timezone.utc)
                ).total_seconds()

            # releasing lock so that the item isn't blocked
            if delay > timeout:
                return None
            await asyncio.sleep(delay)
            return item_for_the_current_worker

        except asyncio.TimeoutError:
            return None

    def _get_hostname_items(self, hostnames: list[str]):
        database = get_database()
        queries = [
            Query.equal("name", hostnames),
            Query.less_than_equal(
                "next_allowed_at", datetime.now(timezone.utc).isoformat()
            ),
            Query.select(["name", "next_allowed_at"]),
        ]
        rows = database.list_rows(APPWRITE_DATABASE_ID, Hostname.__name__, queries)
        return [
            SchedulerQueueItem(
                id=row.id,
                hostname=row.data.get("name"),
                next_allowed_at=datetime.fromisoformat(row.data.get("next_allowed_at")),
            )
            for row in rows.rows
        ]
