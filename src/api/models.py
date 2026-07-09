from pydantic import BaseModel
from datetime import datetime


class SourceRequest(BaseModel):
    url: str


class SourceResponse(BaseModel):
    id: str
    url: str
    hostname: str
    crawl_state: str
    retry_count: int
    priority_score: float
    next_crawl_at: datetime | None
    last_crawl_at: datetime | None


class SourceListRequest(BaseModel):
    id: str | None = None
    url: str | None = None
    hostname: str | None = None
    after_id: str | None = None
    before_id: str | None = None
    limit: int = 20


class SourceListReponse(BaseModel):
    data: list[SourceResponse]


class UpdateSourceRequest(BaseModel):
    priority_score: float | None = None
    crawl_state: str | None = None
    next_crawl_at: datetime | None = None


class HostnameListRequest(BaseModel):
    id: str | None = None
    hostname: str | None = None
    after_id: str | None = None
    before_id: str | None = None
    limit: int = 20


class HostnameResponse(BaseModel):
    name: str
    crawl_count: int = 0
    crawl_delay_seconds: float
    last_crawled_at: datetime | None
    next_allowed_at: datetime | None
    failure_count: int
    success_count: int


class HostnameListResponse(BaseModel):
    data: list[HostnameResponse]
