import asyncio
from typing import Callable
from scout.logger import get_logger
from .worker import Worker


class WorkerPool:
    def __init__(
        self,
        worker_factory: Callable[[int], Worker],
        workers: int = 1,
        max_runtime: float | None = None,
        on_stop: Callable[[], tuple[bool, None | Exception]] | None = None,
    ):
        self._factory = worker_factory
        self._workers_count = workers
        self._worker_tasks: list[tuple[Worker, asyncio.Task]] = []
        self._logger = get_logger("WorkerPool")
        self._stop_event = asyncio.Event()
        # graceful upper bound on a run; None = unbounded (rely on idle-stop)
        self._max_runtime = max_runtime
        self._on_stop = on_stop

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

        # return as soon as ANY of these happens: every worker exits on its own
        # (queue drained / idle), stop() is called, or the max runtime elapses.
        tasks = [task for _, task in self._worker_tasks]
        workers_done = asyncio.gather(*tasks, return_exceptions=True)
        stop_wait = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait(
                {workers_done, stop_wait},
                timeout=self._max_runtime,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not workers_done.done() and not self._stop_event.is_set():
                self._logger.info(
                    f"Max runtime {self._max_runtime}s reached, stopping",
                    tag="MAX_RUNTIME",
                )
            elif workers_done.done():
                self._logger.info("All workers finished, stopping", tag="DRAIN")
        finally:
            stop_wait.cancel()

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

        if self._on_stop:
            stopped, err = await asyncio.to_thread(self._on_stop)
            if not stopped:
                self._logger.error(
                    "Failed to run on_stop", tag="WORKER_POOL", error=err
                )
