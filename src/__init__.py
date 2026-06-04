from .database import init_database
from uuid import uuid4
from dotenv import load_dotenv


def start_crawler():
    load_dotenv()
    init_database()
    crawl_id = str(uuid4())
