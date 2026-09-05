from enum import Enum
from typing import Any, get_args, get_origin
from datetime import datetime
import time

from appwrite.client import Client
from appwrite.enums.tables_db_index_type import TablesDBIndexType
from appwrite.exception import AppwriteException
from appwrite.services.tables_db import TablesDB

# appwrite rejects anything above this: "Index length is longer than the maximum: 767"
MAX_INDEX_PREFIX_LENGTH = 767

_ALREADY_EXISTS = 409


class AppwriteSchemaBuilder:
    def __init__(
        self,
        client: Client,
        database_id: str,
    ):
        self.databases = TablesDB(client)
        self.database_id = database_id
        self.failures: list[str] = []

    def get_database(self):
        return self.databases

    def _record(self, kind: str, name: str, error: Exception) -> None:
        if isinstance(error, AppwriteException) and error.code == _ALREADY_EXISTS:
            print(f"= {kind} exists: {name}")
            return
        print(f"x {kind} FAILED: {name} -> {error}")
        self.failures.append(f"{kind} {name}: {error}")

    def create_collection_from_dict(
        self,
        collection_schema: dict[str, Any],
    ):
        """
        Creates:
        - collection
        - attributes
        - indexes

        Example:
            builder.create_collection_from_dict(
                {
                    "id": "urls",
                    "name": "URL",
                    "fields": [
                        {
                            "name": "url",
                            "type": "string",
                            "indexed": True,
                            "unique": True,
                            "required": True,
                        }
                    ],
                }
            )
        """

        collection_id = collection_schema["id"]
        collection_name = collection_schema.get("name", collection_id)
        fields = collection_schema.get("fields", [])

        print(f"Creating collection: {collection_name}")

        try:
            if not self.databases.get(self.database_id):
                self.databases.create(
                    database_id=self.database_id,
                    name=self.database_id,
                )
        except Exception:
            try:
                self.databases.create(
                    database_id=self.database_id,
                    name=self.database_id,
                )
            except Exception:
                pass

        self._create_collection(
            collection_id=collection_id,
            name=collection_name,
        )

        fields_for_index = []

        for field in fields:
            field_name = field["name"]
            indexed = bool(field.get("indexed", False))
            unique = bool(field.get("unique", False))
            required = bool(field.get("required", True))
            attr_type = self._normalize_type(field.get("type"))

            self._create_attribute(
                collection_id=collection_id,
                field_name=field_name,
                attr_type=attr_type,
                field=field,
                required=required,
            )

            if indexed:
                fields_for_index.append(
                    (
                        field_name,
                        unique,
                        attr_type,
                    )
                )

        # Create indexes
        self._wait_for_columns(collection_id)
        self._drop_failed_indexes(collection_id)
        for field_name, unique, attr_type in fields_for_index:
            self._create_index(
                collection_id=collection_id,
                field_name=field_name,
                unique=unique,
                attr_type=attr_type,
            )

        print(f"Finished collection: {collection_name}")

    def _create_collection(
        self,
        collection_id: str,
        name: str,
    ):
        try:
            self.databases.create_table(
                database_id=self.database_id,
                table_id=collection_id,
                name=name,
                permissions=[],
                row_security=False,
            )

            print(f"+ Collection created: {collection_id}")

        except Exception as e:
            self._record("Collection", collection_id, e)

    def _create_attribute(
        self,
        collection_id: str,
        field_name: str,
        attr_type: str,
        field: dict[str, Any],
        required: bool,
    ):
        try:
            default = self._normalize_default(field.get("default"), attr_type)
            if required:
                default = None

            if attr_type == "string":
                self.databases.create_text_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    required=required,
                    default=default,
                )

            elif attr_type == "integer":
                self.databases.create_integer_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    required=required,
                    default=default,
                )

            elif attr_type == "float":
                self.databases.create_float_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    required=required,
                    default=default,
                )

            elif attr_type == "boolean":
                self.databases.create_boolean_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    required=required,
                    default=default,
                )

            elif attr_type == "datetime":
                self.databases.create_datetime_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    required=required,
                    default=default,
                )

            elif attr_type == "enum":
                enum_values = field.get("elements") or field.get("enum") or []

                if isinstance(enum_values, type) and issubclass(enum_values, Enum):
                    enum_values = [str(item.value) for item in enum_values]

                enum_values = [str(item) for item in enum_values]

                if not enum_values:
                    print(f"Skipping enum field without values: {field_name}")
                    return

                self.databases.create_enum_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    elements=enum_values,
                    required=required,
                    default=default,
                )

            elif attr_type == "array":
                item_type = self._normalize_type(field.get("items", "string"))

                if item_type == "integer":
                    self.databases.create_integer_column(
                        database_id=self.database_id,
                        table_id=collection_id,
                        key=field_name,
                        required=required,
                        default=default,
                        array=True,
                    )
                elif item_type == "float":
                    self.databases.create_float_column(
                        database_id=self.database_id,
                        table_id=collection_id,
                        key=field_name,
                        required=required,
                        default=default,
                        array=True,
                    )
                elif item_type == "boolean":
                    self.databases.create_boolean_column(
                        database_id=self.database_id,
                        table_id=collection_id,
                        key=field_name,
                        required=required,
                        default=default,
                        array=True,
                    )
                elif item_type == "datetime":
                    self.databases.create_datetime_column(
                        database_id=self.database_id,
                        table_id=collection_id,
                        key=field_name,
                        required=required,
                        default=default,
                        array=True,
                    )
                else:
                    self.databases.create_text_column(
                        database_id=self.database_id,
                        table_id=collection_id,
                        key=field_name,
                        required=required,
                        default=default,
                        array=True,
                    )

            elif attr_type == "text":
                self.databases.create_text_column(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=field_name,
                    required=required,
                    default=default,
                )

            else:
                print(f"Skipping unsupported field " f"{field_name}")

            print(f"+ Attribute: {field_name}")

        except Exception as e:
            self._record("Attribute", f"{collection_id}.{field_name}", e)

    # --------------------------------------------------
    # INDEX
    # --------------------------------------------------

    def _create_index(
        self,
        collection_id: str,
        field_name: str,
        unique: bool = False,
        attr_type: str | None = None,
    ):
        try:
            lengths = (
                [MAX_INDEX_PREFIX_LENGTH] if attr_type in {"string", "text"} else None
            )
            self.databases.create_index(
                database_id=self.database_id,
                table_id=collection_id,
                key=f"{field_name}_idx",
                type=TablesDBIndexType.UNIQUE if unique else TablesDBIndexType.KEY,
                columns=[field_name],
                lengths=lengths,
            )

            print(f"+ Index: {field_name}")

        except Exception as e:
            self._record("Index", f"{collection_id}.{field_name}", e)

    def _wait_for_columns(self, collection_id: str, timeout: float = 120) -> None:
        """Columns are provisioned asynchronously; indexing one before it lands fails."""
        deadline = time.monotonic() + timeout
        while True:
            columns = self.databases.list_columns(
                database_id=self.database_id, table_id=collection_id
            ).columns
            pending = [c for c in columns if "processing" in str(c.status).lower()]
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(2)

        for column in columns:
            if "available" in str(column.status).lower():
                continue
            error = getattr(column, "error", "") or f"status={column.status}"
            self._record(
                "Column", f"{collection_id}.{column.key}", Exception(error)
            )

    def _drop_failed_indexes(self, collection_id: str) -> None:
        """A failed index still exists, so create would 409 and never retry it."""
        try:
            indexes = self.databases.list_indexes(
                database_id=self.database_id, table_id=collection_id
            ).indexes
        except Exception:
            return

        for index in indexes:
            if "failed" not in str(index.status).lower():
                continue
            try:
                self.databases.delete_index(
                    database_id=self.database_id,
                    table_id=collection_id,
                    key=index.key,
                )
                print(f"~ dropped failed index: {collection_id}.{index.key}")
            except Exception as e:
                self._record("Index drop", f"{collection_id}.{index.key}", e)

    def verify_indexes(self, collection_id: str, timeout: float = 90) -> None:
        """Indexes build asynchronously; a unique index over duplicate rows fails here."""
        deadline = time.monotonic() + timeout
        while True:
            indexes = self.databases.list_indexes(
                database_id=self.database_id, table_id=collection_id
            ).indexes
            pending = [i for i in indexes if "processing" in str(i.status).lower()]
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(2)

        for index in indexes:
            status = str(index.status).lower()
            if "available" in status:
                continue
            error = getattr(index, "error", "") or f"status={index.status}"
            self._record("Index build", f"{collection_id}.{index.key}", Exception(error))

    # --------------------------------------------------
    # TYPE RESOLUTION
    # --------------------------------------------------

    def _resolve_type(self, annotation):
        """
        Converts:
            str -> string
            int -> integer
            float -> float
            bool -> boolean
            datetime -> datetime
            Enum -> enum
            list[str] -> array
        """

        origin = get_origin(annotation)

        if origin is list:
            return "array"

        if origin is not None:
            args = [arg for arg in get_args(annotation) if arg is not type(None)]

            if len(args) == 1:
                return self._resolve_type(args[0])

        annotation_str = str(annotation).lower()

        if annotation is str:
            return "string"

        if annotation is int:
            return "integer"

        if annotation is float:
            return "float"

        if annotation is bool:
            return "boolean"

        if annotation is datetime:
            return "datetime"

        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return "enum"

        if "list" in annotation_str:
            return "array"

        return "unknown"

    def _normalize_type(self, type_value):
        if isinstance(type_value, str):
            type_str = type_value.strip().lower()
            mapping = {
                "str": "string",
                "string": "string",
                "text": "text",
                "int": "integer",
                "integer": "integer",
                "float": "float",
                "bool": "boolean",
                "boolean": "boolean",
                "datetime": "datetime",
                "date-time": "datetime",
                "enum": "enum",
                "list": "array",
                "array": "array",
            }
            return mapping.get(type_str, type_str)

        if type_value is None:
            return "string"

        annotation = self._unwrap_optional(type_value)
        return self._resolve_type(annotation)

    def _unwrap_optional(self, annotation):
        args = get_args(annotation)

        if type(None) not in args:
            return annotation

        args = [arg for arg in args if arg is not type(None)]

        if len(args) == 1:
            return args[0]

        return annotation

    def _normalize_default(self, value: Any, attr_type: str) -> Any:
        if value is Ellipsis:
            return None

        if isinstance(value, datetime):
            return value.isoformat()

        if isinstance(value, Enum):
            return str(value.value)

        if isinstance(value, list):
            return None

        if attr_type == "enum" and value is not None:
            return str(value)

        return value
