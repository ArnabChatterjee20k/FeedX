import asyncio
from scout.scout import Scout, BrowserManagerConfig
from ..queue.back_queue import BackQueue
from ..queue.scheduler_queue import SchedulerQueue
from scout.core import CrawlConfig, ScrollingRule, VirtualScrollConfig, Document
from .worker import Worker
from ..database import get_database, APPWRITE_DATABASE_ID
from appwrite.query import Query
from appwrite.id import ID
from ..database.models import (
    CrawlState,
    URL,
    Content,
    ContentPipelineState,
    Hostname,
)
from ..discovery import is_ignored
from ..queue.models import URLRow
from .crawl_run import CrawlRunStats
import os, random, re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlsplit, urldefrag
from appwrite.operator import Operator
from domdistill.chunker import HTMLIntentChunker
from domdistill.simhash import get_similarity
from scout.html_parser import HTMLParser
import inspect

crawl_id = os.environ.get("CRAWL_ID")

HOSTNAME_LEASE_SECONDS = 10 * 60
HOST_COOLDOWN_SECONDS = float(os.environ.get("HOST_COOLDOWN_SECONDS", 5 * 60 + 30))
CLAIM_LEASE_SECONDS = int(os.environ.get("CLAIM_LEASE_SECONDS", 10 * 60))
# stop a worker once no hostname has been due for this long. must stay above
# HOST_COOLDOWN_SECONDS or every worker drains during the first cooldown gap,
# when by definition no host is due yet.
CRAWL_IDLE_TIMEOUT_SECONDS = int(
    os.environ.get("CRAWL_IDLE_TIMEOUT_SECONDS", HOST_COOLDOWN_SECONDS + 60)
)
SOURCE_REFRESH_SECONDS = int(os.environ.get("SOURCE_REFRESH_SECONDS", 6 * 60 * 60))
# headless by default so CI runners work; set HEADLESS=false locally to watch.
HEADLESS = os.environ.get("HEADLESS", "true").lower() not in ("false", "0", "no")


class CrawlWorker(Worker):
    def __init__(
        self,
        id,
        back_queue: BackQueue,
        scheduler_queue: SchedulerQueue,
        crawl_run: CrawlRunStats,
    ):
        super().__init__(id)
        self._back_queue = back_queue
        self._scheduler_queue = scheduler_queue
        self._scout = Scout(browser_config=BrowserManagerConfig(headless=HEADLESS))
        self._url = None
        self._scheduled_item = None
        self._crawl_run = crawl_run

    async def start(self):
        self._running = True
        self._logger.info(f"Worker Started {self._id}", tag="START")
        # the worker owns the browser lifecycle; keep it open for the whole run
        async with self._scout.start() as scout:
            self._scout = scout
            await self._run()

    async def _run(self):
        loop = asyncio.get_running_loop()
        idle_since = None
        while self._running:
            item = await self._scheduler_queue.pop_async()
            if not item:
                # nothing due right now; start (or keep) the idle clock and stop
                # once we've been idle past the threshold.
                if idle_since is None:
                    idle_since = loop.time()
                elif loop.time() - idle_since >= CRAWL_IDLE_TIMEOUT_SECONDS:
                    self._logger.info(
                        f"No due hostnames for {CRAWL_IDLE_TIMEOUT_SECONDS}s, stopping worker",
                        tag="DRAIN",
                    )
                    self._running = False
                    break
                continue
            # got work — reset the idle clock
            idle_since = None
            hostname = item.hostname
            # atomic hostname lease: only the worker/process that pushes
            # next_allowed_at into the future (while it is still due) may crawl
            # this host now. keeps two processes off the same host concurrently.
            leased, lease_err = await asyncio.to_thread(self._lease_hostname, item.id)
            if not leased:
                if lease_err:
                    self._logger.error(
                        f"Failed to lease hostname {hostname}",
                        tag="LEASE_HOSTNAME",
                        error=lease_err,
                    )
                else:
                    self._logger.info(
                        f"Hostname {hostname} leased by another worker/process, skipping",
                        tag="LEASE_HOSTNAME",
                    )
                continue
            url = await self._back_queue.pop_async(hostname)
            self._url = url
            self._scheduled_item = item
            if not url:
                continue
            self._logger.info(
                f"Processing item url = {url.url} | hostname = {url.hostname}",
                tag="CRAWL_WORKER_ITEM",
            )
            # atomic claim: QUEUED/RETRY -> FETCHING. only the worker/process that
            # wins the conditional update crawls the url; others skip it.
            retry = 0
            claimed = False
            taken_by_other = False
            err = None
            while not claimed and retry < 5:
                claimed, err = await asyncio.to_thread(self._claim, url.id, url.kind)
                if claimed:
                    break
                if err is None:
                    # 0 rows updated -> another worker/process already claimed it
                    taken_by_other = True
                    break
                self._logger.error(
                    f"Failed to claim url {url.id} to {CrawlState.FETCHING.value}, Retry Count {retry}",
                    tag="CLAIM_STATE",
                    error=err,
                )
                retry += 1
                await asyncio.sleep(1 * (retry + 1))
            if not claimed:
                if taken_by_other:
                    self._logger.info(
                        f"Skipping {url.id}, already claimed by another worker/process",
                        tag="CLAIM_STATE",
                    )
                    # the url was not claimed so the hostname shouldn't be leased at all
                    # release hostname will make it queue again instead of sitting unavailable being leased
                    released, release_err = await asyncio.to_thread(
                        self._release_hostname, self._scheduled_item.id
                    )
                    if not released and release_err:
                        self._logger.error(
                            f"Failed to release hostname {hostname}",
                            tag="RELEASE_HOSTNAME",
                            error=release_err,
                        )
                    elif not released:
                        # 0 rows -> our lease had already expired and someone else
                        # owns the host now; leaving it alone is the correct outcome
                        self._logger.info(
                            f"Lease on {hostname} already expired, nothing to release",
                            tag="RELEASE_HOSTNAME",
                        )
                    # keep the host scheduled so its other urls still get processed
                    self._scheduled_item.set_cooldown(0)
                    await self._scheduler_queue.push_async(self._scheduled_item)
                    continue
                self._logger.error(
                    f"skipping {url.id} as state not claimed",
                    tag="CLAIM_STATE",
                    error=err,
                )
                await self.error()
                continue
            try:
                # a source page just emits new urls to crawl
                if url.kind == "source":
                    await self._discover_source(url)
                    continue
                # depth of a single graph of pages visiting
                depth = 5
                # total pages limit
                page_limit = 10
                # should exclude these matches but shouldn't ignore if they themselves are a blog like how to signin, better login arch
                # also they should be checked if query params present as well
                exclude = [
                    re.compile(
                        r"/(?:login|signin|signup|changelog)/?(?:\?.*)?(?:#.*)?$",
                        re.IGNORECASE,
                    ),
                    re.compile(
                        rf"^{re.escape(url.url)}(?:#.*)?$",
                        re.IGNORECASE,
                    ),
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
                            scroll_count=1200,
                            wait_after_scroll=0.1,
                            scroll_interval=5,
                            scroll_by="container_height",
                        )
                    ),
                )
                # bump the host cooldown up front so it applies on EVERY exit path
                # (success or error) — the crawl hits the host either way.
                self._scheduled_item.set_cooldown(HOST_COOLDOWN_SECONDS)
                documents = await self._scout.crawl(url.url, config=config)
                documents: list[Document] = list(
                    filter(lambda document: isinstance(document, Document), documents)
                )
                # drop nav/auth/search/etc pages Scout discovered so we never turn
                # junk into Content (same shared blocklist source discovery uses)
                documents = [
                    document for document in documents if not is_ignored(document.url)
                ]
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
                    await self.error()
                    continue
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
                            query=document.metadata.get("title") or "",
                            remove_tags=[],
                        )
                        for document in documents
                    ]
                )
                contents = []
                for idx, chunks in enumerate(results):
                    document = documents[idx]
                    simhash = hashes.get(document.url)
                    simhash_chunks = self._extract_simhash_chunks(simhash)
                    title = (document.metadata or {}).get("title") or None
                    contents.append(
                        Content(
                            url=document.url,
                            hostname=document.url.split("/")[2],
                            title=title,
                            simhash=simhash,
                            simhash_1=simhash_chunks[0],
                            simhash_2=simhash_chunks[1],
                            simhash_3=simhash_chunks[2],
                            simhash_4=simhash_chunks[3],
                            chunks=chunks,
                            scraped_at=datetime.now(timezone.utc),
                            pipeline_state=ContentPipelineState.PENDING,
                            crawl_run_id=self._crawl_run.id,
                        )
                    )
                chunks_created = False
                retry = 0

                if not contents:
                    await self.complete()
                    continue

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
                if not chunks_created:
                    await self.error()
                    continue
                await self.complete()
            except Exception as err:
                self._logger.error(
                    f"Failed to crawl {url.id} {url.url}",
                    tag="CRAWL",
                    error=err,
                )
                await self.error()

    async def cancel(self):
        await self._scout.stop()

    async def stop(self):
        self._running = False
        await self._scout.stop()

    def _update_hostname_cooldown(self):
        if self._scheduled_item.next_allowed_at <= datetime.now(timezone.utc):
            self._scheduled_item.set_cooldown(HOST_COOLDOWN_SECONDS)

    async def complete(self):
        tasks: list[asyncio.Task] = []
        self._update_hostname_cooldown()
        try:
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
                t4 = tg.create_task(
                    self._retry(
                        self._crawl_run.record,
                        "UPDATE_CRAWL_RUN",
                        "Failed to record crawl run stats",
                        True,
                    )
                )
                tasks.extend([t1, t2, t3, t4])

        except ExceptionGroup as eg:
            errors = []
            for task in tasks:
                if task.exception():
                    errors.append(task.exception())
            self._logger.error(
                f"Failed to complete crawl for {self._url.id}",
                tag="COMPLETE",
                error=f"{eg} | task errors = {errors}",
            )

    async def error(self):
        self._update_hostname_cooldown()
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
                t4 = tg.create_task(
                    self._retry(
                        self._crawl_run.record,
                        "UPDATE_CRAWL_RUN",
                        "Failed to record crawl run stats",
                        False,
                    )
                )
                tasks.extend([t1, t2, t3, t4])
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
            result = coro_fn(*args, **kwargs)
            if inspect.isawaitable(result):
                success, err = await result
            else:
                success, err = result

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
                "last_crawled_at": datetime.now(timezone.utc).isoformat(),
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

    def _lease_hostname(self, hostname_id) -> tuple[bool, None | Exception]:
        # atomically "lease" a host: conditionally push its next_allowed_at into
        # the future (now + HOSTNAME_LEASE_SECONDS) ONLY if it's currently due
        # (next_allowed_at <= now). the update is the lock — exactly one
        # worker/process can flip a due host, so only that one gets to crawl it;
        # everyone else sees 0 rows updated and backs off. the future timestamp
        # also means that if this worker crashes mid-crawl, the host stays leased
        # (unavailable) until it expires, instead of being grabbed again at once.
        try:
            database = get_database()
            now = datetime.now(timezone.utc)
            lease_until = (now + timedelta(seconds=HOSTNAME_LEASE_SECONDS)).isoformat()
            result = database.update_rows(
                APPWRITE_DATABASE_ID,
                Hostname.__name__,
                data={"next_allowed_at": lease_until},
                queries=[
                    Query.equal("$id", [hostname_id]),
                    Query.less_than_equal("next_allowed_at", now.isoformat()),
                ],
            )
            return len(result.rows) == 1, None
        except Exception as e:
            return False, e

    def _release_hostname(self, hostname_id) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            now = datetime.now(timezone.utc)
            result = database.update_rows(
                APPWRITE_DATABASE_ID,
                Hostname.__name__,
                data={"next_allowed_at": now.isoformat()},
                queries=[
                    Query.equal("$id", [hostname_id]),
                    Query.greater_than("next_allowed_at", now.isoformat()),
                ],
            )
            return len(result.rows) == 1, None
        except Exception as e:
            return False, e

    def _claim(self, url_id, kind: str | None = None) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            lease_until = (now + timedelta(seconds=CLAIM_LEASE_SECONDS)).isoformat()

            queries = [
                Query.equal("$id", [url_id]),
                Query.less_than_equal("next_crawl_at", now_iso),
            ]
            if kind != "source":
                # url-kind: fresh work (QUEUED/RETRY) or an orphaned FETCHING to
                # recover. SUCCESS/FAILED/BLOCKED are never re-claimed here.
                queries.append(
                    Query.equal(
                        "crawl_state",
                        [
                            str(CrawlState.QUEUED.value),
                            str(CrawlState.RETRY.value),
                            str(CrawlState.FETCHING.value),
                        ],
                    )
                )
            # source: recurs from any state; the next_crawl_at lease guard alone
            # already excludes an in-flight FETCHING, so no crawl_state restriction.

            data = {
                "crawl_state": str(CrawlState.FETCHING.value),
                "next_crawl_at": lease_until,
            }
            if self._crawl_run.id:
                data["crawl_run_id"] = self._crawl_run.id

            result = database.update_rows(
                APPWRITE_DATABASE_ID,
                URL.__name__,
                data=data,
                queries=queries,
            )
            return len(result.rows) == 1, None
        except Exception as e:
            return False, e

    def _update_state(self, url_id, state: CrawlState) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            data = {"crawl_state": str(state.value)}
            # A claim leased next_crawl_at into the future; when we hand the url back
            # for RETRY, clear that lease (set it to now) so the retry is eligible on
            # the next run instead of being blocked until the lease expires. Retry
            # backoff is thus decoupled from the crash-recovery lease.
            if state == CrawlState.RETRY:
                data["next_crawl_at"] = datetime.now(timezone.utc).isoformat()
            database.update_row(
                database_id=APPWRITE_DATABASE_ID,
                table_id=URL.__name__,
                row_id=url_id,
                data=data,
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
        if not hashes:
            return [], None
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

            # unlimited here meant appwrite's default 25, so near-duplicate
            # detection only ever compared against 25 existing contents
            candidates = []
            cursor = None
            while True:
                queries = [
                    Query.or_queries(
                        [
                            Query.equal("simhash", list(map(str, all_simhashes))),
                            Query.equal("simhash_1", all_chunk_1),
                            Query.equal("simhash_2", all_chunk_2),
                            Query.equal("simhash_3", all_chunk_3),
                            Query.equal("simhash_4", all_chunk_4),
                        ]
                    ),
                    Query.select(["simhash", "url"]),
                    Query.limit(100),
                ]
                if cursor:
                    queries.append(Query.cursor_after(cursor))
                page = database.list_rows(
                    APPWRITE_DATABASE_ID,
                    Content.__name__,
                    queries=queries,
                    total="false",
                ).rows
                if not page:
                    break
                candidates.extend(page)
                if len(page) < 100:
                    break
                cursor = page[-1].id

            existing_simhash_to_urls: dict[int, list[str]] = {}
            for row in candidates:
                row_data = row.data
                existing_simhash = row_data.get("simhash")
                if isinstance(existing_simhash, str) and existing_simhash.isdigit():
                    existing_simhash = int(existing_simhash)
                existing_url = row_data.get("url")
                if existing_simhash is None:
                    continue
                if existing_simhash not in existing_simhash_to_urls:
                    existing_simhash_to_urls[existing_simhash] = []
                existing_simhash_to_urls[existing_simhash].append(existing_url)

            duplicate_urls = []
            for doc_url, doc_simhash in hashes.items():
                for existing_simhash in existing_simhash_to_urls:
                    if doc_simhash == existing_simhash:
                        self._logger.info(
                            f"Skipping already existing content for {doc_url} | simhash={doc_simhash}",
                            tag="CHECK_CONTENTS_EXIST",
                        )
                        duplicate_urls.append(doc_url)
                        break
                    similarity = get_similarity(doc_simhash, existing_simhash)
                    if similarity > 0.6:
                        self._logger.info(
                            f"Skipping similar content for {doc_url} | existing_simhash={existing_simhash} | similarity={similarity:.2f}",
                            tag="CHECK_CONTENTS_EXIST",
                        )
                        duplicate_urls.append(doc_url)
                        break

            return duplicate_urls, None
        except Exception as e:
            return [], e

    def _create_chunks(self, contents: list[Content]) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            rows = []
            for content in contents:
                row = content.model_dump()
                row["pipeline_state"] = str(content.pipeline_state.value)
                row["scraped_at"] = content.scraped_at.isoformat()
                row = {k: v for k, v in row.items() if v is not None}
                rows.append(row)
            database.create_rows(APPWRITE_DATABASE_ID, Content.__name__, rows=rows)
            return True, None
        except Exception as e:
            return False, e

    async def _discover_source(self, url):
        try:
            self._scheduled_item.set_cooldown(HOST_COOLDOWN_SECONDS)
            bumped, bump_err = await asyncio.to_thread(
                self._bump_source_next_crawl, url.id
            )
            if not bumped:
                self._logger.error(
                    f"Failed to bump next_crawl_at for source {url.id}",
                    tag="SOURCE_GATE",
                    error=bump_err,
                )
            self._scout.set_scrolling_rule(
                ScrollingRule(
                    virtual_scroll=VirtualScrollConfig(
                        container_selector="body",
                        scroll_count=1200,
                        wait_after_scroll=0.1,
                        scroll_interval=5,
                        scroll_by="container_height",
                    )
                )
            )
            document = await self._scout.scrape(url.url)
            if not isinstance(document, Document):
                self._logger.error(
                    f"Source scrape returned no document for {url.url}",
                    tag="DISCOVER_SOURCE",
                )
                await self.error()
                return
            links = self._extract_links([document], url.hostname, url.url)
            self._logger.info(
                f"Discovered {len(links)} links from source {url.url}",
                tag="DISCOVER_SOURCE",
            )
            url_rows, err = await asyncio.to_thread(
                self._create_source_urls, links, url.url
            )
            if err:
                self._logger.error(
                    f"Failed to create urls from source {url.id}",
                    tag="CREATE_SOURCE_URLS",
                    error=err,
                )
                await self.error()
                return
            self._logger.info(
                f"Created {len(url_rows)} new urls from source {url.url}",
                tag="CREATE_SOURCE_URLS",
            )

            # only back queue as scheduled queue is for the hostname backpressure
            # as the source is already present in the backqueue the hostname of the new urls would be present in the scheduler queue as well
            # so only push to the back queue
            for row in url_rows:
                self._back_queue.push(row.hostname, row)
            await self.complete()
        except Exception as err:
            self._logger.error(
                f"Failed to discover source {url.id} {url.url}",
                tag="DISCOVER_SOURCE",
                error=err,
            )
            await self.error()

    def _bump_source_next_crawl(self, url_id) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            next_crawl_at = (
                datetime.now(timezone.utc) + timedelta(seconds=SOURCE_REFRESH_SECONDS)
            ).isoformat()
            database.update_row(
                database_id=APPWRITE_DATABASE_ID,
                table_id=URL.__name__,
                row_id=url_id,
                data={"next_crawl_at": next_crawl_at},
            )
            return True, None
        except Exception as e:
            return False, e

    def _extract_links(self, documents, hostname: str, source_url: str) -> set[str]:
        links: set[str] = set()
        for document in documents:
            try:
                hrefs = HTMLParser(document.html).from_tag("a", "href")
            except Exception:
                continue
            for href in hrefs:
                if not href:
                    continue
                absolute, _ = urldefrag(urljoin(document.url, href))
                parts = urlsplit(absolute)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    continue
                host = parts.hostname
                # keep only same-host links (tolerating a www. prefix either way)
                if (
                    host not in (hostname, f"www.{hostname}")
                    and hostname != f"www.{host}"
                ):
                    continue
                if absolute.rstrip("/") == source_url.rstrip("/"):
                    continue
                links.add(absolute)
        return links

    def _create_source_urls(
        self, urls: set[str], source: str
    ) -> tuple[list[URLRow], None | Exception]:
        try:
            database = get_database()
            now = datetime.now(timezone.utc).isoformat()
            url_list = list(urls)
            if not url_list:
                return [], None
            batch_size = 30
            batches = [
                url_list[i : i + batch_size]
                for i in range(0, len(url_list), batch_size)
            ]
            seen: set[str] = set()
            for batch in batches:
                present_urls = database.list_rows(
                    APPWRITE_DATABASE_ID,
                    URL.__name__,
                    queries=[
                        Query.equal("url", batch),
                        Query.select(["url"]),
                        # without this appwrite caps the response at 25, so a
                        # 30-url batch re-created the overflow every run
                        Query.limit(len(batch)),
                    ],
                )
                for row in present_urls.rows:
                    seen.add(row.data.get("url"))

            if seen:
                self._logger.info(
                    f"Skipping {len(seen)} duplicate urls already in db: {sorted(seen)}",
                    tag="CREATE_SOURCE_URLS",
                )

            urls_to_add = []
            # for further processing
            url_rows_to_return = []
            for u in url_list:
                if u in seen:
                    continue
                host = urlsplit(u).hostname
                if not host:
                    continue
                row = URL(
                    url=u,
                    hostname=host,
                    crawl_state=CrawlState.QUEUED.value,
                    next_crawl_at=now,
                    kind="url",
                    source=source,
                    crawl_run_id=self._crawl_run.id,
                ).model_dump()
                row["crawl_state"] = str(CrawlState.QUEUED.value)
                row["next_crawl_at"] = now
                row = {k: v for k, v in row.items() if v is not None}
                row_id = ID.unique()
                url_rows_to_return.append(URLRow(**row, id=row_id, sequence=0))
                row["$id"] = row_id
                urls_to_add.append(row)
            if not urls_to_add:
                return [], None
            # if error then the source is skipped for this run and wll be used in the next runs
            database.create_rows(APPWRITE_DATABASE_ID, URL.__name__, rows=urls_to_add)
            return url_rows_to_return, None
        except Exception as e:
            return [], e
