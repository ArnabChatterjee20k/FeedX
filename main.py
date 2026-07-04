from dotenv import load_dotenv

# Load env before importing src, because src.database reads env at import time.
load_dotenv(".env")

from src.api import create_api

api = create_api()
