from enum import Enum
from typing import Type, get_args, get_origin
from datetime import datetime

from pydantic import BaseModel
from appwrite.client import Client
from appwrite.services.tables_db import TablesDB
from appwrite.id import ID


class AppwriteSchemaBuilder:
    def __init__(
        self,
        client: Client,
        database_id: str,
    ):
        self.databases = TablesDB(client)
        self.database_id = database_id

    def create_collection_from_model(
        self,
        model: Type[BaseModel],
        collection_id: str | None = None,
        collection_name: str | None = None,
    ):
        """
        Creates:
        - collection
        - attributes
        - indexes

        Example:
            builder.create_collection_from_model(
                Urls,
                collection_id="urls"
            )
        """

        collection_id = collection_id or model.__name__.lower()
        collection_name = collection_name or model.__name__

        print(f"Creating collection: {collection_name}")

        self._create_collection(
            collection_id=collection_id,
            name=collection_name,
        )

        fields_for_index = []

        for field_name, field_info in model.model_fields.items():
            annotation = field_info.annotation
            meta = field_info.json_schema_extra or {}

            indexed = meta.get("indexed", False)
            unique = meta.get("unique", False)
            required = meta.get("required", True)

            attr_type = self._resolve_type(annotation)

            self._create_attribute(
                collection_id=collection_id,
                field_name=field_name,
                attr_type=attr_type,
                annotation=annotation,
                required=required,
            )

            if indexed:
                fields_for_index.append(
                    (
                        field_name,
                        unique
                    )
                )

        # Create indexes
        for field_name, unique in fields_for_index:
            self._create_index(
                collection_id=collection_id,
                field_name=field_name,
                unique=unique,
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
                document_security=False,
            )

            print(f"✓ Collection created: {collection_id}")

        except Exception as e:
            print(
                f"Collection exists or failed: "
                f"{collection_id} -> {e}"
            )

    def _create_attribute(
        self,
        collection_id: str,
        field_name: str,
        attr_type: str,
        annotation,
        required: bool,
    ):
        try:
            if attr_type == "string":
                self.databases.create_string_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    size=5000,
                    required=required,
                )

            elif attr_type == "integer":
                self.databases.create_integer_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    required=required,
                )

            elif attr_type == "float":
                self.databases.create_float_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    required=required,
                )

            elif attr_type == "boolean":
                self.databases.create_boolean_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    required=required,
                )

            elif attr_type == "datetime":
                self.databases.create_datetime_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    required=required,
                )

            elif attr_type == "enum":
                enum_values = [
                    str(item.value)
                    for item in annotation
                ]

                self.databases.create_enum_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    elements=enum_values,
                    required=required,
                )

            elif attr_type == "array":
                self.databases.create_string_attribute(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=field_name,
                    size=1000,
                    array=True,
                    required=required,
                )

            else:
                print(
                    f"Skipping unsupported field "
                    f"{field_name}"
                )

            print(f"✓ Attribute: {field_name}")

        except Exception as e:
            print(
                f"Attribute exists or failed "
                f"{field_name}: {e}"
            )

    # --------------------------------------------------
    # INDEX
    # --------------------------------------------------

    def _create_index(
        self,
        collection_id: str,
        field_name: str,
        unique: bool = False,
    ):
        try:
            self.databases.create_index(
                database_id=self.database_id,
                collection_id=collection_id,
                key=f"{field_name}_idx",
                type="unique" if unique else "key",
                attributes=[field_name],
            )

            print(f"✓ Index: {field_name}")

        except Exception as e:
            print(
                f"Index exists or failed "
                f"{field_name}: {e}"
            )

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

        if origin is not None:
            args = get_args(annotation)

            # Optional[T]
            if origin is type(None):
                annotation = args[0]

            # list[str]
            if origin is list:
                return "array"

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
