from scout.scout import Scout, BrowserManagerConfig
from ..queue.scheduler_queue import SchedulerQueue, SchedulerQueueItem
from ..queue.back_queue import BackQueue
from scout.logger import get_logger
import asyncio
from .crawl_worker import CrawlWorker


class WorkerPool:
    def __init__(
        self, back_queue: BackQueue, scheduler_queue: SchedulerQueue, workers=1
    ):
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._scout = Scout(browser_config=BrowserManagerConfig(headless=False))
        self._workers_count = workers
        self._worker_tasks: list[tuple[CrawlWorker, asyncio.Task]] = []
        self._logger = get_logger(f"WorkerPool")
        self._stop_event = asyncio.Event()

    async def start(self):
        def _worker_done(task: asyncio.Task):
            try:
                task.result()
            except asyncio.CancelledError:
                # Normal shutdown
                return
            except Exception as e:
                self._logger.exception("Worker crashed")
                # for stopping all the workers together
                # self._stop_event.set()
                # self._start_worker(worker_id) if want to restart the worker but can lead to crash loop

        self._logger.info(f"Starting Workers {self._workers_count}", tag="START")
        async with self._scout.start() as scout:
            for i in range(self._workers_count):
                worker = CrawlWorker(
                    i + 1, scout, self._back_queue, self._scheduler_queue
                )
                task = asyncio.create_task(worker.start())
                task.add_done_callback(_worker_done)
                self._worker_tasks.append((worker, task))
            # Keep the scout context alive until stop() is called
            await self._stop_event.wait()

    async def stop(self):
        self._stop_event.set()
        for worker, task in self._worker_tasks:
            await worker.stop()
            task.cancel()

        tasks = [task for _, task in self._worker_tasks]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for worker, task in self._worker_tasks:
            if task.exception():
                self._logger.error(
                    f"Exception in worker {worker._id}",
                    tag="WORKER_POOL",
                    error=task.exception(),
                )
