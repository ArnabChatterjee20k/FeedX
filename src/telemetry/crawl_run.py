import os
from datetime import datetime, timezone

from appwrite.id import ID
from scout.logger import get_logger

from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import CrawlRun

crawl_id = os.environ.get("CRAWL_ID")

# counters are buffered, so an abrupt stop loses whatever has not been written
# yet. flushing every N crawls bounds that to N instead of the whole run.
FLUSH_EVERY = int(os.environ.get("CRAWL_RUN_FLUSH_EVERY", 10))


class CrawlRunStats:
    def __init__(self):
        self.id: str | None = None
        self.urls_attempted = 0
        self.urls_success = 0
        self.urls_failed = 0
        self._flushed_at = 0
        self._logger = get_logger("CRAWL_RUN")

    def start(self) -> tuple[bool, None | Exception]:
        try:
            database = get_database()
            row_id = ID.unique()
            data = {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "urls_attempted": 0,
                "urls_success": 0,
                "urls_failed": 0,
            }
            if crawl_id:
                data["github_action_run_id"] = crawl_id

            database.create_row(
                APPWRITE_DATABASE_ID, CrawlRun.__name__, row_id=row_id, data=data
            )
            self.id = row_id
            self._logger.info(f"Started crawl run {row_id}", tag="START")
            return True, None
        except Exception as e:
            self._logger.error("Failed to start crawl run", tag="START", error=e)
            return False, e

    def record(self, success: bool) -> None:
        self.urls_attempted += 1
        if success:
            self.urls_success += 1
        else:
            self.urls_failed += 1

    def flush_if_due(self) -> tuple[bool, None | Exception]:
        if self.urls_attempted - self._flushed_at < FLUSH_EVERY:
            return True, None
        return self._write()

    def _write(self, finished_at: str | None = None) -> tuple[bool, None | Exception]:
        if not self.id:
            return True, None
        data = {
            "urls_attempted": self.urls_attempted,
            "urls_success": self.urls_success,
            "urls_failed": self.urls_failed,
        }
        if finished_at:
            data["finished_at"] = finished_at
        try:
            database = get_database()
            database.update_row(
                APPWRITE_DATABASE_ID, CrawlRun.__name__, self.id, data=data
            )
            # only on success, so a failed flush is retried by the next one
            self._flushed_at = self.urls_attempted
            return True, None
        except Exception as e:
            return False, e

    def finish(self) -> tuple[bool, None | Exception]:
        summary = (
            f"attempted={self.urls_attempted} "
            f"success={self.urls_success} failed={self.urls_failed}"
        )
        if not self.id:
            self._logger.info(f"Crawl run not recorded, {summary}", tag="FINISH")
            return True, None

        written, err = self._write(finished_at=datetime.now(timezone.utc).isoformat())
        if written:
            self._logger.info(f"Finished crawl run {self.id}, {summary}", tag="FINISH")
        else:
            self._logger.error("Failed to finish crawl run", tag="FINISH", error=err)
        return written, err
