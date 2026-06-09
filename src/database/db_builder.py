from enum import Enum
from typing import Any, get_args, get_origin
from datetime import datetime

from appwrite.client import Client
from appwrite.enums.tables_db_index_type import TablesDBIndexType
from appwrite.services.tables_db import TablesDB


class AppwriteSchemaBuilder:
    def __init__(
        self,
        client: Client,
        database_id: str,
    ):
        self.databases = TablesDB(client)
        self.database_id = database_id

    def get_database(self):
        return self.databases

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

            print(f"✓ Collection created: {collection_id}")

        except Exception as e:
            print(f"Collection exists or failed: " f"{collection_id} -> {e}")

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

            print(f"✓ Attribute: {field_name}")

        except Exception as e:
            print(f"Attribute exists or failed " f"{field_name}: {e}")

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
            lengths = [768] if attr_type in {"string", "text"} else None
            self.databases.create_index(
                database_id=self.database_id,
                table_id=collection_id,
                key=f"{field_name}_idx",
                type=TablesDBIndexType.UNIQUE if unique else TablesDBIndexType.KEY,
                columns=[field_name],
                lengths=lengths,
            )

            print(f"✓ Index: {field_name}")

        except Exception as e:
            print(f"Index exists or failed " f"{field_name}: {e}")

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
