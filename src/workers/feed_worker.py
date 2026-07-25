import os
import asyncio
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import Content, Interaction, InteractionType, get_weight
from ..queue.feed_queue import FeedQueue
from .worker import Worker

EMPTY_QUEUE_POLL_SECONDS = 2


class FeedWorker(Worker):
    def __init__(self, id, feed_queue: FeedQueue):
        super().__init__(id)
        self._queue = feed_queue
        self._content = Content

    async def start(self):
        self._running = True
        self._logger.info(f"Worker Started {self._id}", tag="START")
        while self._running:
            item = self._queue.pop()
            if not item:
                await asyncio.sleep(EMPTY_QUEUE_POLL_SECONDS)
                continue

            self._content = item
            if not item.summary:
                self._logger.warning(
                    f"Skipped content {item.id}, url = {item.url}, empty summary",
                )
                await self.error("Empty summary")
                continue

            if not item.tags:
                self._logger.warning(
                    f"Skipped content {item.id}, url = {item.url}, empty tags",
                )
                await self.error("Empty tags")
                continue

    def stop(self):
        return super().stop()

    def complete(self, *args, **kwargs):
        return super().complete(*args, **kwargs)

    def error(self, *args, **kwargs):
        return super().error(*args, **kwargs)

    def cancel(self):
        return super().cancel()
