from collections import deque
import os
import asyncio
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import Content, ContentPipelineState
from .models import ContentRow
from scout.logger import get_logger
from appwrite.query import Query
from . import Queue


class ContentQueue(Queue):
    def __init__(self):
        self._queue: deque[ContentRow] = deque()
        self._logger = get_logger("CONTENT_QUEUE")
        # since the workers will start with pulling the same item so a single global lock will work
        self._refill_lock = asyncio.Lock()

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

    async def pop_async(self) -> ContentRow | None:
        while True:
            if not self._queue:
                async with self._refill_lock:
                    # re-check under the lock; another worker may have refilled
                    if not self._queue:
                        await asyncio.to_thread(self._refill)
                if not self._queue:
                    return None

            item = self._queue.popleft()
            claimed, err = await asyncio.to_thread(self._claim, item.id)
            if err:
                self._logger.error(
                    f"Failed to claim content {item.id}", tag="CLAIM", error=err
                )
                continue
            if claimed:
                self._logger.info(f"Claimed content {item.id}", tag="CLAIM")
                return item
            # already claimed by another worker/process -> try the next candidate

    def _claim(self, content_id) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            result = database.update_rows(
                APPWRITE_DATABASE_ID,
                Content.__name__,
                data={"pipeline_state": str(ContentPipelineState.SUMMARIZING.value)},
                queries=[
                    Query.equal("$id", [content_id]),
                    Query.equal(
                        "pipeline_state", [str(ContentPipelineState.PENDING.value)]
                    ),
                ],
            )
            return len(result.rows) == 1, None
        except Exception as e:
            return False, e

    def _refill(self) -> None:
        self._queue.extend(self._get_all_contents())

    def _get_all_contents(self) -> list[ContentRow]:
        database = get_database()
        limit = os.environ.get("FRONT_QUEUE_INIT_LIMIT", 1000)
        queries = [
            Query.equal("pipeline_state", [str(ContentPipelineState.PENDING.value)]),
            Query.limit(limit),
            Query.order_asc("scraped_at"),
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
