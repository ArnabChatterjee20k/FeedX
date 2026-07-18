import os
import asyncio
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import Content, ContentPipelineState
from ..queue.content_queue import ContentQueue
from ..agent.content import ContentAgent, ContentAnalysis
from .worker import Worker

crawl_id = os.environ.get("CRAWL_ID")

EMPTY_QUEUE_POLL_SECONDS = 2


class ContentWorker(Worker):
    def __init__(self, id, content_queue: ContentQueue):
        super().__init__(id)
        self._queue = content_queue
        self._agent = ContentAgent()
        self._content = None

    async def start(self):
        self._running = True
        self._logger.info(f"Worker Started {self._id}", tag="START")
        while self._running:
            # atomically claims the row (PENDING -> SUMMARIZING) before returning
            item = await self._queue.pop_async()
            if not item:
                await asyncio.sleep(EMPTY_QUEUE_POLL_SECONDS)
                continue

            # track the current item so the terminal hooks can act on it
            self._content = item

            if not item.chunks:
                self._logger.warning(
                    f"Skipped content {item.id}, url = {item.url}, empty chunks",
                    tag="CONTENT_WORKER_ITEM",
                )
                await self.error("Empty chunks")
                continue

            content_analysis_result = None
            analysis_error = ""
            retry = 0
            while not content_analysis_result and retry < 5:
                content = "\n\n".join(item.chunks)
                try:
                    content_analysis_result = await self._agent.analyze_async(
                        content=content,
                        allowed_tags=[],
                        title=item.title,
                        url=item.url,
                    )
                except Exception as err:
                    self._logger.error(
                        f"Failed to analyze content {item.id}, Retry Count {retry}",
                        tag="ANALYZE_CONTENT",
                        error=err,
                    )
                    analysis_error = str(err)
                    retry += 1
                    await asyncio.sleep(1 * (retry + 1))

            if not content_analysis_result:
                self._logger.info(
                    f"Skipped content {item.id}, url = {item.url}, not able to analyze content",
                    tag="CONTENT_WORKER_ITEM",
                )
                await self.error(analysis_error)
                continue

            updated = False
            err = None
            retry = 0
            while not updated and retry < 5:
                updated, err = await asyncio.to_thread(
                    self._update_content, item.id, content_analysis_result
                )
                if not updated:
                    self._logger.error(
                        f"Failed to update data of content {item.id}, Retry Count {retry}",
                        tag="UPDATE_CONTENT_DATA",
                        error=err,
                    )
                    retry += 1
                    await asyncio.sleep(1 * (retry + 1))

            if not updated:
                self._logger.error(
                    f"skipping {item.id} as data not updated",
                    tag="UPDATE_CONTENT_DATA",
                    error=err,
                )
                await self.error(str(err) if err else "Failed to update content")
                continue

            await self.complete()

    async def stop(self):
        self._running = False

    async def cancel(self):
        self._running = False

    async def complete(self):
        content = self._content
        if content is None:
            return
        # the COMPLETED state + summary/tags are written by _update_content; this
        # is the terminal success hook for logging / future side effects.
        self._logger.info(
            f"Completed content {content.id}, url = {content.url}",
            tag="COMPLETE",
        )

    async def error(self, error=""):
        content = self._content
        if content is None:
            return
        updated = False
        retry = 0
        while not updated and retry < 5:
            updated, err = await asyncio.to_thread(
                self._update_state,
                content.id,
                ContentPipelineState.FAILED,
                error=str(error),
            )
            if not updated:
                self._logger.error(
                    f"Failed to mark content {content.id} as FAILED, Retry Count {retry}",
                    tag="ERROR",
                    error=err,
                )
                retry += 1
                await asyncio.sleep(1 * (retry + 1))

    def _update_state(
        self, content_id, state: ContentPipelineState, error=None
    ) -> tuple[bool, None | Exception]:
        try:
            data = {"pipeline_state": str(state.value)}
            if error:
                data["pipeline_error"] = str(error)
            database = get_database()
            database.update_row(
                database_id=APPWRITE_DATABASE_ID,
                table_id=Content.__name__,
                row_id=content_id,
                data=data,
            )
            return True, None
        except Exception as e:
            return False, e

    def _update_content(
        self, content_id, content_analysis: ContentAnalysis
    ) -> tuple[bool, None | Exception]:
        try:
            data = {
                "pipeline_state": str(ContentPipelineState.COMPLETED.value),
                "summary": content_analysis.summary,
                "tags": content_analysis.tags,
            }
            database = get_database()
            database.update_row(
                database_id=APPWRITE_DATABASE_ID,
                table_id=Content.__name__,
                row_id=content_id,
                data=data,
            )
            return True, None
        except Exception as e:
            return False, e
