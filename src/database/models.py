from enum import Enum
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticUndefined


def DBField(
    *,
    indexed: bool = False,
    unique: bool = False,
    required: bool | None = None,
    default: Any = ...,
):
    extra = {
        "indexed": indexed,
        "unique": unique,
    }

    if required is not None:
        extra["required"] = required

    return Field(
        default=default,
        json_schema_extra=extra,
    )


class CrawlState(Enum):
    QUEUED = 1
    FETCHING = 2
    SUCCESS = 3
    RETRY = 4
    FAILED = 5
    BLOCKED = 6


# TODO: needs a retrying state as well and remove unncessary states
class ContentPipelineState(Enum):
    PENDING = 1
    EXTRACTING = 2
    SUMMARIZING = 3
    TAGGING = 4
    COMPLETED = 5
    FAILED = 6


class Hostname(BaseModel):
    name: str = DBField(indexed=True, unique=True)

    last_crawled_at: datetime | None = DBField(default=None, indexed=True)

    crawl_count: int = DBField(default=0)

    crawl_delay_seconds: float = DBField(default=10)

    next_allowed_at: datetime = DBField(indexed=True)

    failure_count: int = DBField(default=0)
    success_count: int = DBField(default=0)


class URL(BaseModel):
    url: str = DBField(indexed=True, unique=True)

    hostname: str = DBField(indexed=True)

    crawl_state: CrawlState = DBField(indexed=True)

    retry_count: int = DBField(default=0)

    priority_score: float = DBField(default=0, indexed=True)

    depth: int = DBField(default=0)

    # will not be None and will be set to a date due to db optimisations
    next_crawl_at: datetime = DBField(indexed=True)

    last_crawl_at: datetime | None = DBField(default=None)

    crawl_run_id: str | None = DBField(default=None, indexed=True)

    @field_validator("crawl_state", mode="before")
    @classmethod
    def _coerce_crawl_state(cls, value):
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value


class Content(BaseModel):
    url: str = DBField(indexed=True, unique=True)

    hostname: str = DBField(indexed=True)

    title: str | None = DBField(default=None)

    # exact similarity matching
    # hash: str = DBField(indexed=True)

    # direct simhash match -> a string for storing 64 bit integer
    simhash: str = DBField(indexed=True)

    # dividing the 64 bits simhash into 4 * 16 bits to query by or and then doing the hamming distance on the application level
    simhash_1: int = DBField(indexed=True)
    simhash_2: int = DBField(indexed=True)
    simhash_3: int = DBField(indexed=True)
    simhash_4: int = DBField(indexed=True)

    summary: str | None = DBField(default=None)

    chunks: list[str] = DBField(default=[])

    tags: list[str] = DBField(default=[])

    score: float = DBField(default=0, indexed=True)

    scraped_at: datetime = DBField(indexed=True)


    last_shown_at: datetime | None = DBField(default=None, indexed=True)
    last_seen_at: datetime | None = DBField(default=None, indexed=True)

    crawl_run_id: str | None = DBField(default=None, indexed=True)
    pipeline_state: ContentPipelineState = DBField(indexed=True)
    pipeline_error: str | None = DBField(default=None)

    @field_validator("pipeline_state", mode="before")
    @classmethod
    def _coerce_pipeline_state(cls, value):
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

    @field_validator("simhash", mode="before")
    @classmethod
    def _coerce_simhash(cls, value):
        if not isinstance(value, str):
            return str(value)
        return value


class CrawlRun(BaseModel):
    started_at: datetime = DBField(indexed=True)

    finished_at: datetime | None = DBField(default=None, indexed=True)

    urls_attempted: int = DBField(default=0)
    urls_success: int = DBField(default=0)
    urls_failed: int = DBField(default=0)

    github_action_run_id: str | None = DBField(default=None, indexed=True)


class InteractionType(str, Enum):
    IMPRESSION = "impression"
    OPEN = "open"
    READ = "read"
    LIKE = "like"
    BOOKMARK = "bookmark"
    SHARE = "share"
    HIDE = "hide"

INTERACTION_WEIGHTS: dict[InteractionType, float] = {
    InteractionType.IMPRESSION: 0.1,
    InteractionType.OPEN: 1.0,
    InteractionType.READ: 2.0,
    InteractionType.LIKE: 3.0,
    InteractionType.BOOKMARK: 3.0,
    InteractionType.SHARE: 3.0,
    InteractionType.HIDE: -3.0,
}


def get_weight(interaction_type: InteractionType) -> float:
    return INTERACTION_WEIGHTS.get(interaction_type, 0.0)

class Interaction(BaseModel):
    content_id: str = DBField(indexed=True)
    type: InteractionType = DBField(indexed=True)
    weight: float = DBField(default=0)
    # for multiple tags we can add multiple tags
    # tags are non repeatable so we have the id as tag name only
    # upsert tags in bulk with atomic increment
    tag: str = DBField(required=True, indexed=True)
    created_at: datetime = DBField(indexed=True)

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
        field_schema = {
            "name": field_name,
            "type": resolve_type(field_info.annotation),
            "indexed": meta.get("indexed", False),
            "unique": meta.get("unique", False),
            "required": meta.get("required", field_info.is_required()),
        }

        if field_info.default is not PydanticUndefined:
            field_schema["default"] = field_info.default

        schema.append(field_schema)
    return schema
