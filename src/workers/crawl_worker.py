import asyncio
from scout.scout import Scout, BrowserManagerConfig
from ..queue import BackQueue, SchedulerQueue
from scout.logger import get_logger
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import CrawlState, URL
import os

crawl_id = os.environ.get("CRAWL_ID")


class Worker:
    def __init__(
        self, id, scout: Scout, back_queue: BackQueue, scheduler_queue: SchedulerQueue
    ):
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._id = id
        self._scout = scout
        self._logger = get_logger(f"Worker_{id}")
        self._running = False

    async def start(self):
        # TODO: adding loggings
        self._running = True
        self._logger.info(f"Worker Started {self._id}", tag="START")
        while self._running:
            item = await self._scheduler_queue.pop_async()
            if not item:
                continue
            hostname = item.hostname
            url = await self._back_queue.pop_async(hostname)
            if not url:
                continue
            retry = 1
            updated = False
            while retry < 6:
                updated = await asyncio.to_thread(
                    self._update_state(url.id, state=CrawlState.FETCHING)
                )
                if not updated:
                    retry += 1
            if not updated:
                continue
            await self._scout.crawl(url.url)

    async def cancel(self):
        pass

    async def complete(self):
        pass

    async def error(self):
        pass

    async def stop(self):
        self._running = False

    def _update_state(self, url_id, state: CrawlState):
        try:
            database = get_database()
            database.update_row(
                database_id=APPWRITE_DATABASE_ID,
                table_id=URL.__name__,
                row_id=url_id,
                data={"crawl_state": state.value},
            )
            return True
        except:
            return False


class WorkerPool:
    def __init__(self, back_queue: Queue, scheduler_queue: Queue, workers=1):
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._scout = Scout(browser_config=BrowserManagerConfig(headless=True))
        self._workers_count = workers
        self._worker_tasks: list[tuple[Worker, asyncio.Task]] = []
        self._logger = get_logger(f"WorkerPool")

    async def start(self):
        self._logger.info(f"Starting Workers {self._workers_count}", tag="START")
        # sharing the same browser instance so that multiple browsers aren't started
        async with self._scout.start() as scout:
            for i in range(self._workers_count):
                worker = Worker(i + 1, scout, self._back_queue, self._scheduler_queue)
                self._worker_tasks.append((worker, asyncio.create_task(worker.start())))

    async def stop(self):
        pass
