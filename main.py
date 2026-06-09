from dotenv import load_dotenv

# Load env before importing src, because src.database reads env at import time.
load_dotenv(".env")

from src import start_crawler


def main():
    start_crawler()


if __name__ == "__main__":
    main()
