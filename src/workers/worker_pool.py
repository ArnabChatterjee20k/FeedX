from scout.scout import Scout, BrowserManagerConfig
from ..queue.scheduler_queue import SchedulerQueue, SchedulerQueueItem
from ..queue.back_queue import BackQueue
from scout.logger import get_logger
import asyncio
from .worker import Worker


class WorkerPool:
    def __init__(
        self, back_queue: BackQueue, scheduler_queue: SchedulerQueue, workers=1
    ):
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
        for worker, task in self._worker_tasks:
            await worker.stop()
            task.cancel()
