from .database import init_database
from .queue import init_queues
from dotenv import load_dotenv
from .workers.worker_pool import WorkerPool


async def start_crawler():
    load_dotenv(".env")
    # init_database()
    _, back_queue, scheduler_queue = init_queues()
    pool = WorkerPool(back_queue, scheduler_queue, 2)
    await pool.start()
