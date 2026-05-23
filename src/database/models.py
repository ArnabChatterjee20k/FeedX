from enum import Enum
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field

def DBField(
    *,
    indexed: bool = False,
    unique: bool = False,
    required: bool = True,
    default: Any = ...,
):
    return Field(
        default=default,
        json_schema_extra={
            "indexed": indexed,
            "unique": unique,
            "required": required,
        },
    )

class CrawlState(Enum):
    QUEUED = 1
    FETCHING = 2
    SUCCESS = 3
    RETRY = 4
    FAILED = 5
    BLOCKED = 6

class ContentPipelineState(Enum):
    PENDING = 1
    EXTRACTING = 2
    SUMMARIZING = 3
    TAGGING = 4
    COMPLETED = 5
    FAILED = 6

class Sources(BaseModel):
    url: str = DBField(indexed=True, unique=True)
    is_active: bool = DBField(default=True, indexed=True)


class Hostnames(BaseModel):
    name: str = DBField(indexed=True, unique=True)

    last_crawled_at: datetime | None = DBField(
        default=None,
        indexed=True
    )

    crawl_count: int = DBField(default=0)

    crawl_delay_seconds: float = DBField(default=10)

    next_allowed_at: datetime | None = DBField(
        default=None,
        indexed=True
    )

    failure_count: int = DBField(default=0)
    success_count: int = DBField(default=0)


class Urls(BaseModel):
    url: str = DBField(indexed=True, unique=True)

    hostname: str = DBField(indexed=True)

    crawl_state: CrawlState = DBField(indexed=True)

    retry_count: int = DBField(default=0)

    priority_score: float = DBField(
        default=0,
        indexed=True
    )

    depth: int = DBField(default=0)

    next_crawl_at: datetime | None = DBField(
        default=None,
        indexed=True
    )

    last_crawl_at: datetime | None = DBField(
        default=None
    )

    crawl_run_id: str | None = DBField(
        default=None,
        indexed=True
    )


class Content(BaseModel):
    url: str = DBField(indexed=True, unique=True)

    hostname: str = DBField(indexed=True)

    hash: str = DBField(indexed=True)

    summary: str = DBField()

    tags: list[str] = DBField(default=[])

    score: float = DBField(
        default=0,
        indexed=True
    )

    scraped_at: datetime = DBField(
        indexed=True
    )

    crawl_run_id: str | None = DBField(
        default=None,
        indexed=True
    )
    pipeline_state: ContentPipelineState = DBField(
        indexed=True
    )
    pipeline_error: str | None = DBField(
        default=None
    )


class CrawlRun(BaseModel):
    started_at: datetime = DBField(indexed=True)

    finished_at: datetime | None = DBField(
        default=None,
        indexed=True
    )

    urls_attempted: int = DBField(default=0)
    urls_success: int = DBField(default=0)
    urls_failed: int = DBField(default=0)

    github_action_run_id: str | None = DBField(
        default=None,
        indexed=True
    )

def resolve_type(annotation):
    annotation_str = str(annotation)

    if annotation in [str]:
        return "string"

    if annotation in [int]:
        return "integer"

    if annotation in [float]:
        return "float"

    if annotation in [bool]:
        return "boolean"

    if annotation in [datetime]:
        return "datetime"

    if "list" in annotation_str.lower():
        return "array"

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "enum"

    return "unknown"


def get_model_schema(model: type[BaseModel]):
    schema = []

    for field_name, field_info in model.model_fields.items():
        meta = field_info.json_schema_extra or {}

        schema.append(
            {
                "name": field_name,
                "type": resolve_type(field_info.annotation),
                "indexed": meta.get("indexed", False),
                "unique": meta.get("unique", False),
                "required": meta.get("required", True),
                "default": field_info.default,
            }
        )

    return schema