from ..database.models import URL, Hostname, Content
from datetime import datetime, timezone, timedelta


class URLRow(URL):
    id: str
    sequence: int


class HostnameRow(Hostname):
    id: str
    sequence: int


class ContentRow(Content):
    id: str
    sequence: int


class SchedulerQueueItem:
    def __init__(
        self,
        id: str,
        hostname: str,
        next_allowed_at: datetime | None,
    ):
        self.id = id
        self.hostname = hostname
        self.next_allowed_at = (
            next_allowed_at
            if next_allowed_at is not None
            else datetime.now(timezone.utc)
        )

    def __lt__(self, other: "SchedulerQueueItem") -> bool:
        return self.next_allowed_at < other.next_allowed_at

    def __repr__(self) -> str:
        return (
            "SchedulerQueueItem("
            f"hostname={self.hostname!r}, "
            f"next_allowed_at={self.next_allowed_at!r}"
            ")"
        )

    def add_seconds(self, seconds: int):
        self.next_allowed_at += timedelta(seconds=seconds)
