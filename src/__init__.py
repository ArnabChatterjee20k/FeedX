from .database import init_database
from .queue import init_queues
from uuid import uuid4
from dotenv import load_dotenv


def start_crawler():
    load_dotenv(".env")
    # init_database()
    init_queues()
    crawl_id = str(uuid4())
