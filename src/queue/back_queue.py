from collections import deque
from scout.logger import get_logger
from .models import URLRow
from . import Queue


class BackQueue(Queue):
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
        if hostname not in self._queues or not self._queues[hostname]:
            return None
        return self._queues[hostname].popleft()

    def get_hostnames(self) -> list[str]:
        return list(self._queues.keys())
