import asyncio
from scout.scout import Scout, BrowserManagerConfig
from ..queue.back_queue import BackQueue
from ..queue.scheduler_queue import SchedulerQueue
from scout.logger import get_logger
from scout.core import CrawlConfig, ScrollingRule, VirtualScrollConfig, Document
from ..database import get_database, APPWRITE_DATABASE_ID
from appwrite.query import Query
from ..database.models import CrawlState, URL, Content, ContentPipelineState
from hashlib import md5
import os, random, re
from datetime import datetime

crawl_id = os.environ.get("CRAWL_ID")


class Worker:
    def __init__(
        self, id, scout: Scout, back_queue: BackQueue, scheduler_queue: SchedulerQueue
    ):
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._id = id
        self._scout = scout
        self._logger = get_logger(f"Worker_{id}")
        self._running = False

    async def start(self):
        self._running = True
        self._logger.info(f"Worker Started {self._id}", tag="START")
        while self._running:
            item = await self._scheduler_queue.pop_async()
            if not item:
                continue
            hostname = item.hostname
            url = await self._back_queue.pop_async(hostname)
            if not url:
                continue
            retry = 0
            updated = False
            while not updated and retry < 5:
                updated, err = await asyncio.to_thread(
                    self._update_state(url.id, state=CrawlState.FETCHING)
                )
                if not updated:
                    self._logger.error(
                        f"Failed to update state of url {url.id} to {CrawlState.FETCHING.value}, Retry Count {retry}",
                        tag="UPDATE_STATE",
                        error=err,
                    )
                    retry += 1
            if not updated:
                self._logger.error(
                    f"skipping {url.id} as state not updated",
                    tag="UPDATE_STATE",
                    error=err,
                )
                continue
            depth = 5
            page_limit = 10
            # should exclude these matches but shouldn't ignore if they themselves are a blog like how to signin, better login arch
            # also they should be checked if query params present as well
            exclude = [
                re.compile(
                    r"/(?:login|signin|signup|changelog)/?(?:\?.*)?(?:#.*)?$",
                    re.IGNORECASE,
                )
            ]
            # the url itself should excape the regex
            include = [re.compile(rf"^{re.escape(url.url)}")]
            config = CrawlConfig(
                page_limit=page_limit,
                max_depth=depth,
                concurrency=3,
                include=include,
                exclude=exclude,
                page_transition_delay=random.randint(1, 6),
                scrolling=ScrollingRule(
                    virtual_scroll=VirtualScrollConfig(
                        container_selector="body",
                        scroll_count=12,
                        wait_after_scroll=0.1,
                        scroll_by="container_height",
                    )
                ),
            )
            documents = await self._scout.crawl(url.url, config=config)
            documents: list[Document] = list(
                filter(lambda document: isinstance(document, Document), documents)
            )
            hashes = {
                document.url: md5(document.to_markdown()).hexdigest()
                for document in documents
            }
            result: tuple[dict[str, str], None | Exception] = await asyncio.to_thread(
                self._check_existing_content_hashes(list(hashes.keys()))
            )
            non_existing_url_hashes, err = result
            if err:
                self._logger.error(
                    f"Failed to check contents from url {url.id}, saving to database and depending on the unique index",
                    tag="CHECK_CONTENTS_EXIST",
                    error=err,
                )
            # filtering non existing from the documents
            documents = list(
                filter(
                    lambda document: hashes.get(document.url)
                    not in non_existing_url_hashes,
                    documents,
                )
            )
            # todo: add a global semaphore so that theres a restriction in thread spawning
            results = await asyncio.gather(
                *[
                    asyncio.to_thread(
                        document.get_relevant_sections,
                        query=document.metadata.get("title"),
                    )
                    for document in documents
                ]
            )
            contents = []
            for idx, chunks in enumerate(results):
                document = documents[idx]
                hash = hashes.get(document.url)
                contents.append(
                    Content(
                        url=url,
                        hostname=url.hostname,
                        hash=hash,
                        chunks=chunks,
                        scraped_at=datetime.now(),
                        pipeline_state=ContentPipelineState.PENDING,
                    )
                )
            chunks_created = False
            retry = 0

            while not chunks_created and retry < 5:
                chunks_created = await asyncio.to_thread(self._create_chunks(contents))
                if not chunks_created:
                    self._logger.error(
                        f"Failed to create chunks for url {url.id}, Retry Count {retry}",
                        tag="UPDATE_STATE",
                        error=err,
                    )
                    retry += 1

    async def cancel(self):
        await self._scout.stop()

    async def stop(self):
        self._running = False
        await self._scout.stop()

    def _update_state(self, url_id, state: CrawlState) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            database.update_row(
                database_id=APPWRITE_DATABASE_ID,
                table_id=URL.__name__,
                row_id=url_id,
                data={"crawl_state": state.value},
            )
            return True, None
        except Exception as e:
            return False, e

    def _check_existing_content_hashes(
        self, hashes
    ) -> tuple[dict[str, str], None | Exception]:
        try:
            database = get_database()
            rows = database.list_rows(
                APPWRITE_DATABASE_ID,
                Content.__name__,
                queries=[Query.equal("hash", hashes), Query.select(["hash"])],
                total="false",
            )
            rows = list(map(lambda row: row.model_dump().get("hash"), rows.rows))
            return rows, None
        except Exception as e:
            return [], e

    def _create_chunks(self, contents: list[Content]):
        try:
            database = get_database()
            contents = list(map(lambda content: content.model_dump(), contents))
            database.create_rows(APPWRITE_DATABASE_ID, Content.__name__, rows=contents)
            return True
        except Exception as e:
            return False


class WorkerPool:
    def __init__(
        self, back_queue: BackQueue, scheduler_queue: SchedulerQueue, workers=1
    ):
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._scout = Scout(browser_config=BrowserManagerConfig(headless=True))
        self._workers_count = workers
        self._worker_tasks: list[tuple[Worker, asyncio.Task]] = []
        self._logger = get_logger(f"WorkerPool")

    async def start(self):
        self._logger.info(f"Starting Workers {self._workers_count}", tag="START")
        # sharing the same browser instance so that multiple browsers aren't started
        async with self._scout.start() as scout:
            for i in range(self._workers_count):
                worker = Worker(i + 1, scout, self._back_queue, self._scheduler_queue)
                self._worker_tasks.append((worker, asyncio.create_task(worker.start())))

    async def stop(self):
        for worker, task in self._worker_tasks:
            await worker.stop()
            task.cancel()
