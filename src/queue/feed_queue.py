from collections import deque
import os
import asyncio
from datetime import datetime, timezone, timedelta
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import Content, ContentPipelineState
from .models import ContentRow
from scout.logger import get_logger
from appwrite.query import Query
from . import Queue

# no concurrency to fight since a feed will be for a single user only
# so its ok to use a single worker only to build the feed
class FeedQueue(Queue):
    def __init__(self):
        self._queue: deque[ContentRow] = deque()
        self._logger = get_logger("FEED_QUEUE")
    
    def init(self) -> None:
        try:
            self._queue.extend(self._get_all_contents())
            self._logger.info(f"Init queue of {len(self._queue)}", tag="INIT")
        except Exception as e:
            self._logger.error("Error during init", tag="INIT", error=e)

    def push(self, contents: list[Content]) -> bool:
        try:
            database = get_database()
            contents = list(
                map(
                    lambda content: {
                        **content.model_dump(),
                        "pipeline_state": content.pipeline_state.value,
                    },
                    contents,
                )
            )
            result = database.create_rows(
                APPWRITE_DATABASE_ID, Content.__name__, rows=contents
            )
            self._logger.info(f"Pushed {result.total} items", tag="PUSH")
            self._queue.extend(contents)
            return True
        except Exception as e:
            self._logger.error("Error during push", tag="PUSH", error=e)
            return False

    def pop(self):
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._logger.info(f"Popped content {item.id}", tag="POP")
        return item

    def _get_all_contents(self) -> list[ContentRow]:
        database = get_database()
        limit = os.environ.get("QUEUE_INIT_LIMIT", 1000)
        window = os.environ.get("FEED_WINDOW_DAY", 30)
        queries = [
            Query.equal("pipeline_state", [str(ContentPipelineState.COMPLETED.value)]),
            Query.limit(limit),
            Query.order_asc("scraped_at"),
            Query.less_than_equal(
                "scraped_at", datetime.now(timezone.utc).isoformat()
            ),
            Query.greater_than_equal(
                "scraped_at", (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
            ),
        ]
        rows = database.list_rows(
            database_id=APPWRITE_DATABASE_ID,
            table_id=Content.__name__,
            queries=queries,
            total="false",
            model_type=Content,
        )
        return [
            ContentRow(**row.data.model_dump(), id=row.id, sequence=row.sequence)
            for row in rows.rows
        ]