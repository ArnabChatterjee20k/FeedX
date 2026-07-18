import asyncio
from typing import Callable
from scout.logger import get_logger
from .worker import Worker


class WorkerPool:
    def __init__(self, worker_factory: Callable[[int], Worker], workers: int = 1):
        self._factory = worker_factory
        self._workers_count = workers
        self._worker_tasks: list[tuple[Worker, asyncio.Task]] = []
        self._logger = get_logger("WorkerPool")
        self._stop_event = asyncio.Event()

    def _worker_done(self, task: asyncio.Task):
        try:
            task.result()
        except asyncio.CancelledError:
            # normal shutdown
            return
        except Exception:
            self._logger.exception("Worker crashed")
            # for stopping all the workers together
            # self._stop_event.set()
            # self._start_worker(worker_id) if want to restart the worker but can lead to crash loop

    async def start(self):
        self._logger.info(f"Starting Workers {self._workers_count}", tag="START")
        for i in range(self._workers_count):
            worker = self._factory(i + 1)
            task = asyncio.create_task(worker.start())
            task.add_done_callback(self._worker_done)
            self._worker_tasks.append((worker, task))
        # keep the pool coroutine alive until stop() is called
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
            if not task.cancelled() and task.exception():
                self._logger.error(
                    f"Exception in worker {worker._id}",
                    tag="WORKER_POOL",
                    error=task.exception(),
                )
