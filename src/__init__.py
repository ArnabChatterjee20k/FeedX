from .queue import init_queues
from .workers.worker_pool import WorkerPool


def get_worker_pool():
    _, back_queue, scheduler_queue = init_queues()
    pool = WorkerPool(back_queue, scheduler_queue, 2)
    return pool
