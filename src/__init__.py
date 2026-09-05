import os
from .queue import init_queues
from .workers.worker_pool import WorkerPool
from .workers.crawl_worker import CrawlWorker


def _max_runtime() -> float | None:
    # graceful upper bound for a pool run (seconds). Set below the CI job timeout
    # so the pool stops itself and cleans up instead of being hard-killed.
    value = os.environ.get("WORKER_MAX_RUNTIME_SECONDS")
    return float(value) if value else None


def get_worker_pool(workers: int = 1):
    from .workers.crawl_run import CrawlRunStats

    _, back_queue, scheduler_queue = init_queues()
    crawl_run = CrawlRunStats()
    crawl_run.start()
    return WorkerPool(
        lambda id: CrawlWorker(id, back_queue, scheduler_queue, crawl_run),
        workers,
        max_runtime=_max_runtime(),
        on_stop=crawl_run.finish,
    )


def get_content_worker_pool(workers: int = 1):
    from .queue.content_queue import ContentQueue
    from .workers.content_worker import ContentWorker

    content_queue = ContentQueue()
    content_queue.init()
    return WorkerPool(
        lambda id: ContentWorker(id, content_queue),
        workers,
        max_runtime=_max_runtime(),
    )


def get_feed_worker_pool(workers: int = 1):
    from .workers.feed_worker import FeedWorker

    # no worker pool needed for this as it is a single step process
    return FeedWorker(id=1)
