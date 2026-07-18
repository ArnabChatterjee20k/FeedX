from .queue import init_queues
from .workers.worker_pool import WorkerPool
from .workers.crawl_worker import CrawlWorker


def get_worker_pool(workers: int = 1):
    _, back_queue, scheduler_queue = init_queues()
    return WorkerPool(lambda id: CrawlWorker(id, back_queue, scheduler_queue), workers)


def get_content_worker_pool(workers: int = 1):
    from .queue.content_queue import ContentQueue
    from .workers.content_worker import ContentWorker

    content_queue = ContentQueue()
    content_queue.init()
    return WorkerPool(lambda id: ContentWorker(id, content_queue), workers)
