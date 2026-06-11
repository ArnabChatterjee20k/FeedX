from enum import Enum
import os
from typing import Any, get_args, get_origin

from .models import Hostname, URL, Content, CrawlRun
from .db_builder import AppwriteSchemaBuilder
from appwrite.client import Client
from pydantic_core import PydanticUndefined

APPWRITE_ENDPOINT = os.environ.get("APPWRITE_ENDPOINT")
APPWRITE_PROJECT_ID = os.environ.get("APPWRITE_PROJECT_ID")
APPWRITE_API_KEY = os.environ.get("APPWRITE_API_KEY")
APPWRITE_DATABASE_ID = os.environ.get("APPWRITE_DATABASE_ID")


def _unwrap_optional(annotation):
    args = get_args(annotation)

    if type(None) not in args:
        return annotation

    args = [arg for arg in args if arg is not type(None)]

    if len(args) == 1:
        return args[0]

    return annotation


def _allows_none(annotation) -> bool:
    return type(None) in get_args(annotation)


def _model_to_collection_schema(model) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []

    for field_name, field_info in model.model_fields.items():
        meta = field_info.json_schema_extra or {}
        raw_annotation = field_info.annotation
        annotation = _unwrap_optional(raw_annotation)
        has_default = field_info.default is not PydanticUndefined
        explicit_required = meta.get("required")
        required = (
            bool(explicit_required)
            if explicit_required is not None
            else field_info.is_required() and not _allows_none(raw_annotation)
        )

        field_schema = {
            "name": field_name,
            "type": annotation,
            "required": required,
            "indexed": bool(meta.get("indexed", False)),
            "unique": bool(meta.get("unique", False)),
        }

        if has_default:
            field_schema["default"] = field_info.default

        if isinstance(annotation, type) and issubclass(annotation, Enum):
            field_schema["elements"] = [str(item.value) for item in annotation]

        if get_origin(annotation) is list:
            items = get_args(annotation)
            if items:
                field_schema["items"] = items[0]

        fields.append(field_schema)

    return {
        "id": model.__name__.lower(),
        "name": model.__name__,
        "fields": fields,
    }


def _create_appwrite_client() -> Client:
    return (
        Client()
        .set_endpoint(APPWRITE_ENDPOINT)
        .set_project(APPWRITE_PROJECT_ID)
        .set_key(APPWRITE_API_KEY)
    )


def _create_schema_builder() -> AppwriteSchemaBuilder:
    return AppwriteSchemaBuilder(
        _create_appwrite_client(),
        database_id=APPWRITE_DATABASE_ID,
    )


def init_database():
    db = _create_schema_builder()
    for model in [Hostname, URL, Content, CrawlRun]:
        db.create_collection_from_dict(_model_to_collection_schema(model))


def get_database():
    return _create_schema_builder().get_database()
