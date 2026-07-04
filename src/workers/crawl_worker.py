import asyncio
from scout.scout import Scout
from ..queue.back_queue import BackQueue
from ..queue.scheduler_queue import SchedulerQueue
from scout.logger import get_logger
from scout.core import CrawlConfig, ScrollingRule, VirtualScrollConfig, Document
from ..database import get_database, APPWRITE_DATABASE_ID
from appwrite.query import Query
from ..database.models import CrawlState, URL, Content, ContentPipelineState, Hostname
import os, random, re
from datetime import datetime
from appwrite.operator import Operator
from domdistill.chunker import HTMLIntentChunker
from domdistill.simhash import get_similarity

crawl_id = os.environ.get("CRAWL_ID")


class CrawlWorker:
    def __init__(
        self, id, scout: Scout, back_queue: BackQueue, scheduler_queue: SchedulerQueue
    ):
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._id = id
        self._scout = scout
        self._logger = get_logger(f"Worker_{id}")
        self._running = False
        self._url = None
        self._scheduled_item = None

    async def start(self):
        self._running = True
        self._logger.info(f"Worker Started {self._id}", tag="START")
        while self._running:
            item = await self._scheduler_queue.pop_async()
            if not item:
                continue
            hostname = item.hostname
            url = await self._back_queue.pop_async(hostname)
            self._url = url
            self._scheduled_item = item
            retry = 0
            updated = False
            if not url:
                continue
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
                    await asyncio.sleep(1 * (retry + 1))
            if not updated:
                self._logger.error(
                    f"skipping {url.id} as state not updated",
                    tag="UPDATE_STATE",
                    error=err,
                )
                continue
            try:
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
                # updating the global scheduled item for next scheduling
                FIVE_MINUTES = 5 * 60
                OFFSET_SECONDS = 30
                self._scheduled_item.add_seconds(FIVE_MINUTES + OFFSET_SECONDS)
                documents: list[Document] = list(
                    filter(lambda document: isinstance(document, Document), documents)
                )
                hashes = {
                    document.url: HTMLIntentChunker(document.html)
                    .get_fingerprint()
                    .document_hash
                    for document in documents
                }
                result: tuple[list[str], None | Exception] = await asyncio.to_thread(
                    self._check_existing_content_hashes, hashes
                )

                duplicate_urls, err = result
                if err:
                    self._logger.error(
                        f"Failed to check contents from url {url.id}, saving to database and depending on the unique index",
                        tag="CHECK_CONTENTS_EXIST",
                        error=err,
                    )
                # filtering out duplicates, keeping only new documents
                documents = list(
                    filter(
                        lambda document: document.url not in duplicate_urls,
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
                    simhash = hashes.get(document.url)
                    simhash_chunks = self._extract_simhash_chunks(simhash)
                    contents.append(
                        Content(
                            url=document.url,
                            hostname=document.url.split("/")[2],
                            simhash=simhash,
                            simhash_1=simhash_chunks[0],
                            simhash_2=simhash_chunks[1],
                            simhash_3=simhash_chunks[2],
                            simhash_4=simhash_chunks[3],
                            chunks=chunks,
                            scraped_at=datetime.now(),
                            pipeline_state=ContentPipelineState.PENDING,
                        )
                    )
                chunks_created = False
                retry = 0

                while not chunks_created and retry < 5:
                    chunks_created, chunk_err = await asyncio.to_thread(
                        self._create_chunks, contents
                    )
                    if not chunks_created:
                        self._logger.error(
                            f"Failed to create chunks for url {url.id}, Retry Count {retry}",
                            tag="CREATE_CHUNKS",
                            error=chunk_err,
                        )
                        retry += 1
                        await asyncio.sleep(1 * (retry + 1))
                await self.complete()
            except Exception as err:
                self._logger.error(
                    f"Failed to crawl {url.id}",
                    tag="CRAWL",
                    error=err,
                )
                await self.error()

    async def cancel(self):
        await self._scout.stop()

    async def stop(self):
        self._running = False
        await self._scout.stop()

    async def complete(self):
        try:
            tasks = []

            async with asyncio.TaskGroup() as tg:
                t1 = tg.create_task(
                    self._retry(
                        self._scheduler_queue.push_async,
                        "RESCHEDULE_HOSTNAME",
                        "Failed to push item to queue",
                        self._scheduled_item,
                    )
                )
                t2 = tg.create_task(
                    self._retry(
                        self._update_hostname_stats,
                        "UPDATE_HOSTNAME_STATS",
                        "Failed to update hostname stats",
                        True,
                    )
                )
                t3 = tg.create_task(
                    self._retry(
                        self._update_state,
                        "UPDATE_URL_STATE",
                        "Failed to update crawl state",
                        self._url.id,
                        state=CrawlState.SUCCESS,
                    )
                )
                tasks.extend([t1, t2, t3])

        except ExceptionGroup as eg:
            self._logger.error(
                f"Failed to complete crawl for {self._url.id}", tag="COMPLETE", error=eg
            )

    async def error(self):
        try:
            tasks = []
            async with asyncio.TaskGroup() as tg:
                t1 = tg.create_task(
                    self._retry(
                        self._scheduler_queue.push_async,
                        "RESCHEDULE_HOSTNAME",
                        "Failed to push item to queue",
                        self._scheduled_item,
                    )
                )
                t2 = tg.create_task(
                    self._retry(
                        self._update_hostname_stats,
                        "UPDATE_HOSTNAME_STATS",
                        "Failed to update hostname stats",
                        False,
                    )
                )
                t3 = tg.create_task(
                    self._retry(
                        self._update_state,
                        "UPDATE_URL_STATE",
                        "Failed to update crawl state",
                        self._url.id,
                        state=CrawlState.RETRY,
                    )
                )
                tasks.extend([t1, t2, t3])
        except ExceptionGroup as eg:
            self._logger.error(
                f"Failed to save crawl error state for {self._url.id}",
                tag="ERROR",
                error=eg,
            )

    async def _retry(self, coro_fn, tag, error_message, *args, **kwargs):
        max_retries = 5
        delay = 1
        for retry in range(max_retries):
            success, err = await coro_fn(*args, **kwargs)

            if success:
                self._logger.info(f"Success {tag}", tag=tag)
                return True, None

            if retry < max_retries - 1:
                self._logger.error(
                    error_message,
                    tag=tag,
                    error=err,
                )
                await asyncio.sleep(delay * (retry + 1))

        return False, err

    def _update_hostname_stats(self, success: bool) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            data = {
                "last_crawled_at": datetime.now().isoformat(),
                "next_allowed_at": self._scheduled_item.next_allowed_at.isoformat(),
            }

            if success:
                data["crawl_count"] = Operator.increment(1)
                data["success_count"] = Operator.increment(1)
            else:
                data["failure_count"] = Operator.increment(1)

            database.update_row(
                APPWRITE_DATABASE_ID,
                Hostname.__name__,
                self._scheduled_item.id,
                data=data,
            )
            return True, None
        except Exception as e:
            return False, e

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

    def _extract_simhash_chunks(self, simhash: int) -> list[int]:
        return [
            simhash & 0xFFFF,
            (simhash >> 16) & 0xFFFF,
            (simhash >> 32) & 0xFFFF,
            (simhash >> 48) & 0xFFFF,
        ]

    def _check_existing_content_hashes(
        self, hashes: dict[str, int]
    ) -> tuple[list[str], None | Exception]:
        try:
            database = get_database()

            all_simhashes = list(hashes.values())
            all_chunk_1 = []
            all_chunk_2 = []
            all_chunk_3 = []
            all_chunk_4 = []
            for simhash in hashes.values():
                chunks = self._extract_simhash_chunks(simhash)
                all_chunk_1.append(chunks[0])
                all_chunk_2.append(chunks[1])
                all_chunk_3.append(chunks[2])
                all_chunk_4.append(chunks[3])

            rows = database.list_rows(
                APPWRITE_DATABASE_ID,
                Content.__name__,
                queries=[
                    Query.or_queries(
                        [
                            Query.equal("simhash", all_simhashes),
                            Query.equal("simhash_1", all_chunk_1),
                            Query.equal("simhash_2", all_chunk_2),
                            Query.equal("simhash_3", all_chunk_3),
                            Query.equal("simhash_4", all_chunk_4),
                        ]
                    ),
                    Query.select(["simhash", "url"]),
                ],
                total="false",
            )

            existing_simhash_to_urls: dict[int, list[str]] = {}
            for row in rows.rows:
                row_data = row.model_dump()
                existing_simhash = row_data.get("simhash")
                existing_url = row_data.get("url")
                if existing_simhash not in existing_simhash_to_urls:
                    existing_simhash_to_urls[existing_simhash] = []
                existing_simhash_to_urls[existing_simhash].append(existing_url)

            duplicate_urls = []
            for doc_url, doc_simhash in hashes.items():
                for existing_simhash in existing_simhash_to_urls:
                    if doc_simhash == existing_simhash:
                        duplicate_urls.append(doc_url)
                        break
                    similarity = get_similarity(doc_simhash, existing_simhash)
                    if similarity > 0.6:
                        duplicate_urls.append(doc_url)
                        break

            return duplicate_urls, None
        except Exception as e:
            return [], e

    def _create_chunks(self, contents: list[Content]) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            contents = list(map(lambda content: content.model_dump(), contents))
            database.create_rows(APPWRITE_DATABASE_ID, Content.__name__, rows=contents)
            return True, None
        except Exception as e:
            return False, e
