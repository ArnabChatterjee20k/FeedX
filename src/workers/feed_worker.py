import os
from ..feed.feed import get_interaction_matrix, mark_shown
from .worker import Worker


class FeedWorker(Worker):
    def __init__(self, id):
        super().__init__(id)

    async def start(self):
        self._logger.info(f"Worker Started {self._id}", tag="START")
        contents = get_interaction_matrix()
        if contents:
            limit = int(os.environ.get("FEED_LIMIT", 10))
            contents = contents[:limit]
            # TODO: upload to github
            print(contents)

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
