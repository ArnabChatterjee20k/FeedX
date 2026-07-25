import os
from ..feed.feed import get_interaction_matrix, select_feed, mark_shown
from ..feed.github import publish_feed
from .worker import Worker


class FeedWorker(Worker):
    def __init__(self, id):
        super().__init__(id)

    def start(self):
        self._logger.info(f"Worker Started {self._id}", tag="START")
        scored = get_interaction_matrix()
        if scored:
            limit = int(os.environ.get("FEED_LIMIT", 10))
            contents = select_feed(scored, limit)

            published, err = publish_feed(contents)
            if not published:
                self._logger.error("Failed to publish feed", tag="PUBLISH", error=err)
                return

            ok, err = mark_shown(contents)
            if not ok:
                self._logger.error("Failed to mark shown", tag="SHOWN", error=err)
            else:
                self._logger.info(f"Marked {len(contents)} shown", tag="SHOWN")

    def stop(self):
        return super().stop()

    def complete(self, *args, **kwargs):
        return super().complete(*args, **kwargs)

    def error(self, *args, **kwargs):
        return super().error(*args, **kwargs)

    def cancel(self):
        return super().cancel()
