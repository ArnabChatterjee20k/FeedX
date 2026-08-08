from pydantic import BaseModel
from datetime import datetime
from typing import Literal
from ..database.models import InteractionType

type URLKind = Literal["source", "url"]


class LoginRequest(BaseModel):
    password: str


class SourceRequest(BaseModel):
    url: str
    kind: URLKind | None = "source"
    source: str | None = "api"


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
    source: list[str] | None = None
    limit: int = 20
    kind: URLKind | None = "source"


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


class ContentInteractionRequest(BaseModel):
    id: str
    interaction: InteractionType
    tags: list[str]


class ContentInteractionResponse(BaseModel):
    interaction_ids: list[str]


class ContentListRequest(BaseModel):
    id: str | None = None
    url: str | None = None
    hostname: str | None = None
    tag: str | None = None
    pipeline_state: str | None = None
    after_id: str | None = None
    before_id: str | None = None
    limit: int = 20


class ContentResponse(BaseModel):
    id: str
    url: str
    hostname: str
    title: str | None = None
    summary: str | None = None
    tags: list[str] = []
    score: float = 0
    pipeline_state: str
    pipeline_error: str | None = None
    scraped_at: datetime | None = None
    last_shown_at: datetime | None = None
    last_seen_at: datetime | None = None


class ContentListResponse(BaseModel):
    data: list[ContentResponse]
