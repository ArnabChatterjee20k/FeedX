from datetime import datetime, timezone, timedelta
from ..database import get_database, APPWRITE_DATABASE_ID
from ..database.models import (
    Interaction,
    InteractionWithTime,
    TagScore,
    InteractionType,
    Content,
    ContentPipelineState,
    get_weight,
    ContentWithId,
)
from appwrite.operator import Operator
from appwrite.query import Query
import os

RECENT_HALF_LIFE_DAYS = 2.0
LONG_HALF_LIFE_DAYS = 14.0
CONTENT_HALF_LIFE_DAYS = 10

PAGE_SIZE = 1000


def get_decay(weight=1, event_age=1, half_life_value=1):
    return weight * 0.5 ** (event_age / half_life_value)


class Feed:
    def get_interactions(self) -> list[InteractionWithTime]:
        database = get_database()
        tags: list[InteractionWithTime] = []
        ttl_seconds = 60 if os.environ.get("ENVIRONMENT") != "development" else None
        last_tag = None
        current = datetime.now(timezone.utc)
        while True:
            queries = [Query.limit(PAGE_SIZE)]
            if last_tag:
                queries.append(Query.cursor_after(last_tag))
            # caching data here
            rows = database.list_rows(
                APPWRITE_DATABASE_ID,
                Interaction.__name__,
                ttl=ttl_seconds,
                queries=queries,
                total=False,
            )

            if not rows.rows:
                break

            for row in rows.rows:
                age = (current - datetime.fromisoformat(row.createdat)).days
                tags.append(InteractionWithTime(**row.data, age=age))

            last_tag = rows.rows[-1].id
            if len(rows.rows) < PAGE_SIZE:
                break

        return tags

    def add_interactions(
        self,
        content_id: str,
        tags: list[str],
        interaction_type: InteractionType = InteractionType.OPEN,
    ) -> tuple[bool, Exception | None]:
        try:
            weight = get_weight(interaction_type)
            database = get_database()
            log_rows = [
                {
                    "content_id": content_id,
                    "tag": tag,
                    "type": interaction_type,
                    "weight": weight,
                }
                for tag in tags
            ]
            database.create_rows(
                APPWRITE_DATABASE_ID, Interaction.__name__, rows=log_rows
            )
            return True, None
        except Exception as e:
            return False, e

    def get_content(self, window_days: int | None = None) -> list[ContentWithId]:
        window_days = window_days or int(os.environ.get("FEED_WINDOW_DAY", 30))
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=window_days)
        ttl_seconds = 60 if os.environ.get("ENVIRONMENT") != "development" else None
        database = get_database()

        content: list[ContentWithId] = []
        cursor = None
        while True:
            queries = [
                Query.equal(
                    "pipeline_state", [str(ContentPipelineState.COMPLETED.value)]
                ),
                Query.greater_than_equal("scraped_at", since.isoformat()),
                Query.less_than_equal("scraped_at", now.isoformat()),
                Query.order_desc("scraped_at"),
                Query.limit(PAGE_SIZE),
            ]
            if cursor:
                queries.append(Query.cursor_after(cursor))

            rows = database.list_rows(
                APPWRITE_DATABASE_ID,
                Content.__name__,
                queries=queries,
                total=False,
                ttl=ttl_seconds,
            )
            if not rows.rows:
                break

            for row in rows.rows:
                content.append(ContentWithId(**row.data, id=row.id))

            cursor = rows.rows[-1].id
            if len(rows.rows) < PAGE_SIZE:
                break

        return content

    def get_interaction_matrix(self):
        database = get_database()
        interactions = self.get_interactions()
        hidden_interaction_tags = set()
        tags: dict[str, list[InteractionWithTime]] = {}
        current = datetime.now(timezone.utc)
        for interaction in interactions:
            if interaction.tag not in tags:
                tags[interaction.tag] = []
            tags[interaction.tag].append(interaction)
            if interaction.type == InteractionType.HIDE:
                hidden_interaction_tags.add(interaction.tag)

        tags_matrix: dict[str, TagScore] = {}

        for tag, interactions in tags.items():
            recent = 0
            long = 0
            for interaction in interactions:
                recent += get_decay(
                    interaction.weight,
                    event_age=interaction.age,
                    half_life_value=RECENT_HALF_LIFE_DAYS,
                )
                long += get_decay(
                    interaction.weight,
                    event_age=interaction.age,
                    half_life_value=LONG_HALF_LIFE_DAYS,
                )
            tags_matrix[tag] = TagScore(tag=tag, recent_weight=recent, long_weight=long)

        # normalize to [0,1] by the max across tags (guard a non-positive max)
        max_recent = max((t.recent_weight for t in tags_matrix.values()), default=1)
        max_long = max((t.long_weight for t in tags_matrix.values()), default=1)
        max_recent = max_recent if max_recent > 0 else 1
        max_long = max_long if max_long > 0 else 1
        for t in tags_matrix.values():
            t.recent_weight /= max_recent
            t.long_weight /= max_long

        database.upsert_rows(
            APPWRITE_DATABASE_ID,
            TagScore.__name__,
            rows=[{"$id": t.tag, **t.model_dump()} for t in tags_matrix.values()],
        )

        content_matrix = {}
        contents = self.get_content()
        for content in contents:
            tags = content.tags
            continuity = 0
            relevance = 0
            supressed_penalty = 1
            for tag in tags:
                required_tag = tags_matrix.get(tag, TagScore(tag=tag))
                continuity = max(continuity, (required_tag.recent_weight))
                relevance += required_tag.long_weight
                if tag in hidden_interaction_tags:
                    supressed_penalty = 0.1
            if len(tags):
                relevance /= len(tags)
            novelty = 1 - relevance

            content_age = (current - content.scraped_at).days
            age_factor = get_decay(
                event_age=content_age, half_life_value=CONTENT_HALF_LIFE_DAYS
            )
            shown_factor = 1
            if content.last_shown_at:
                days_since_last_shown = (current - content.last_shown_at).days
                # clamping days_since_last_shown/3 to range 0.15 and 1
                shown_factor = min(max(days_since_last_shown / 3, 0.15), 1)

            freshness = age_factor * shown_factor * supressed_penalty

            # The 0.5 / 0.2 / 0.3 split is the "50% keep my threads, 20% core, 30% explore" budget
            score = (0.5 * continuity + 0.2 * relevance + 0.3 * novelty) * freshness
            content_matrix[content.id] = score
