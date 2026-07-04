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
