from collections import deque
from scout.logger import get_logger
from .models import URLRow
from . import Queue
import asyncio


class BackQueue(Queue):
    def __init__(self):
        self._queues: dict[str, deque[URLRow]] = {}
        self._logger = get_logger("BACK_QUEUE")
        self._hostname_lock: dict[str, asyncio.Lock] = {}

    def init(self, urls: list[URLRow]):
        for url in urls:
            self.push(url.hostname, url)

        self._logger.info(f"Pushed {len(urls)} urls", tag="INIT")

    def push(self, hostname: str, url: URLRow):
        if hostname in self._queues:
            self._queues[hostname].append(url)
        else:
            self._queues[hostname] = deque([url])

        if hostname not in self._hostname_lock:
            self._hostname_lock[hostname] = asyncio.Lock()

    def pop(self, hostname):
        if hostname not in self._queues or not self._queues[hostname]:
            return None
        return self._queues[hostname].popleft()

    async def pop_async(self, hostname):
        try:
            if hostname not in self._queues or not self._queues[hostname]:
                return None
            async with self._hostname_lock[hostname]:
                return self.pop(hostname)
        except Exception as e:
            self._logger.error("Error during popping", tag="POP_ASYNC", error=e)
            return None

    def get_hostnames(self) -> list[str]:
        return list(self._queues.keys())
