from collections import deque
from scout.logger import get_logger
from .models import URLRow


class BackQueue:
    def __init__(self):
        self._queues: dict[str, deque[URLRow]] = {}
        self._logger = get_logger("BACK_QUEUE")

    def init(self, urls: list[URLRow]):
        for url in urls:
            self.push(url.hostname, url)

        self._logger.info(f"Pushed {len(urls)} urls", tag="INIT")

    def push(self, hostname: str, url: URLRow):
        if hostname in self._queues:
            self._queues[hostname].append(url)
        else:
            self._queues[hostname] = deque([url])

    def pop(self, hostname):
        return self._queues[hostname].popleft()
