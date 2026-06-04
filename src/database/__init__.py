from enum import Enum
from typing import Any, get_args, get_origin

from .models import Sources, Hostnames, Urls, Content, CrawlRun
from .db_builder import AppwriteSchemaBuilder
from appwrite.client import Client
from pydantic_core import PydanticUndefined


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


appwrite_client = (
    Client()
    .set_endpoint("https://fra.stage.cloud.appwrite.io/v1")
    .set_project("6a21bbda001db9ed5e4b")
    .set_key(
        "standard_93ccf0bd056bb9f88b7317523df35016b48d4d0ae61abbfedb39a6a9919e2c06d2a9dd22015af33b9c064273a94e0c4e0bbe232b21f37ca61aa9ff4c88a53b17864dcc4541a7141e05f7f870688299a1b97a8750a5e3987d5fef15a1cc6b4e97d250c47e881284f4e292cb6a494d8f88a785daca0b301cc8fb89883e5d62ca23"
    )
)


def init_database():
    db = AppwriteSchemaBuilder(appwrite_client, database_id="feedx")
    for model in [Sources, Hostnames, Urls, Content, CrawlRun]:
        db.create_collection_from_dict(_model_to_collection_schema(model))


def get_database():
    return AppwriteSchemaBuilder(appwrite_client, database_id="feedx").get_database()
