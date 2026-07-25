from dataclasses import dataclass
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
from appwrite.query import Query
import os

RECENT_HALF_LIFE_DAYS = 2.0
LONG_HALF_LIFE_DAYS = 14.0
CONTENT_HALF_LIFE_DAYS = 10

PAGE_SIZE = 1000

# feed selection: the "50% keep my threads, 20% core, 30% explore" budget,
# diversified with MMR (Jaccard tag-overlap) so we don't stack near-duplicates.
FEED_CONTINUITY_RATIO = 0.5
FEED_RELEVANCE_RATIO = 0.2
FEED_NOVELTY_RATIO = 0.3
MMR_LAMBDA = 0.5


@dataclass
class ScoredContent:
    content: ContentWithId
    continuity: float
    relevance: float
    novelty: float
    interest: float  # ungated blend; interest × freshness == content.score
    freshness: float


def get_decay(weight=1, event_age=1, half_life_value=1):
    return weight * 0.5 ** (event_age / half_life_value)


def _as_utc(dt: datetime) -> datetime:
    # appwrite datetimes may deserialize tz-naive; treat naive as UTC so
    # subtraction against datetime.now(timezone.utc) never raises.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def get_interactions() -> list[InteractionWithTime]:
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
            total="false",
        )

        if not rows.rows:
            break

        for row in rows.rows:
            age = (current - _as_utc(datetime.fromisoformat(row.createdat))).days
            tags.append(InteractionWithTime(**row.data, age=age))

        last_tag = rows.rows[-1].id
        if len(rows.rows) < PAGE_SIZE:
            break

    return tags


def add_interactions(
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
        database.create_rows(APPWRITE_DATABASE_ID, Interaction.__name__, rows=log_rows)
        return True, None
    except Exception as e:
        return False, e


def get_content(window_days: int | None = None) -> list[ContentWithId]:
    window_days = window_days or int(os.environ.get("FEED_WINDOW_DAY", 30))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    ttl_seconds = 60 if os.environ.get("ENVIRONMENT") != "development" else None
    database = get_database()

    content: list[ContentWithId] = []
    cursor = None
    while True:
        queries = [
            Query.equal("pipeline_state", [str(ContentPipelineState.COMPLETED.value)]),
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
            total="false",
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


def get_interaction_matrix() -> list[ScoredContent]:
    database = get_database()
    interactions = get_interactions()
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

    contents = get_content()
    scored: list[ScoredContent] = []
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

        content_age = (current - _as_utc(content.scraped_at)).days
        age_factor = get_decay(
            event_age=content_age, half_life_value=CONTENT_HALF_LIFE_DAYS
        )
        shown_factor = 1
        if content.last_shown_at:
            days_since_last_shown = (current - _as_utc(content.last_shown_at)).days
            # clamping days_since_last_shown/3 to range 0.15 and 1
            shown_factor = min(max(days_since_last_shown / 3, 0.15), 1)

        freshness = age_factor * shown_factor * supressed_penalty

        # freshness gates the blend; the budget split lives in select_feed
        interest = (
            FEED_CONTINUITY_RATIO * continuity
            + FEED_RELEVANCE_RATIO * relevance
            + FEED_NOVELTY_RATIO * novelty
        )
        content.score = interest * freshness
        scored.append(
            ScoredContent(
                content=content,
                continuity=continuity,
                relevance=relevance,
                novelty=novelty,
                interest=interest,
                freshness=freshness,
            )
        )

    return sorted(scored, key=lambda s: s.content.score, reverse=True)


def _pick_mmr(
    candidates: list[ScoredContent],
    signal: str,
    chosen_ids: set[str],
    chosen_tag_sets: list[set[str]],
) -> ScoredContent | None:
    # MMR: balance the bucket's signal against tag-overlap with what is
    # already chosen, so we favour high-signal-but-diverse content.
    best = None
    best_mmr = None
    for item in candidates:
        if item.content.id in chosen_ids:
            continue
        relevance = getattr(item, signal) * item.freshness
        tag_set = set(item.content.tags)
        sim = max((_jaccard(tag_set, s) for s in chosen_tag_sets), default=0.0)
        mmr = MMR_LAMBDA * relevance - (1 - MMR_LAMBDA) * sim
        if best_mmr is None or mmr > best_mmr:
            best_mmr = mmr
            best = item
    return best


def select_feed(scored: list[ScoredContent], limit: int) -> list[ContentWithId]:
    # split the feed budget across the three intents, then fill each bucket
    # with MMR; a final pass tops up from the leftovers (by score) so rounding
    # or an under-filled bucket never leaves the feed short.
    buckets = [
        ("continuity", round(limit * FEED_CONTINUITY_RATIO)),
        ("relevance", round(limit * FEED_RELEVANCE_RATIO)),
        ("novelty", round(limit * FEED_NOVELTY_RATIO)),
    ]

    chosen: list[ScoredContent] = []
    chosen_ids: set[str] = set()
    chosen_tag_sets: list[set[str]] = []

    def take(signal: str, quota: int):
        for _ in range(quota):
            if len(chosen) >= limit:
                return
            best = _pick_mmr(scored, signal, chosen_ids, chosen_tag_sets)
            if best is None:
                return
            chosen.append(best)
            chosen_ids.add(best.content.id)
            chosen_tag_sets.append(set(best.content.tags))

    # for each signal picking the best content
    for signal, quota in buckets:
        take(signal, quota)

    # top up any remaining slots by overall interest (freshness-gated in
    # _pick_mmr, so equivalent to ranking by score, still MMR-diversified)
    while len(chosen) < limit:
        best = _pick_mmr(scored, "interest", chosen_ids, chosen_tag_sets)
        if best is None:
            break
        chosen.append(best)
        chosen_ids.add(best.content.id)
        chosen_tag_sets.append(set(best.content.tags))

    chosen.sort(key=lambda s: s.content.score, reverse=True)
    return [s.content for s in chosen]


def mark_shown(contents: list[ContentWithId]) -> tuple[bool, Exception | None]:
    if not contents:
        return True, None
    try:
        database = get_database()
        now = datetime.now(timezone.utc)
        ids = [content.id for content in contents]
        database.update_rows(
            APPWRITE_DATABASE_ID,
            Content.__name__,
            data={"last_shown_at": now.isoformat()},
            queries=[Query.equal("$id", ids)],
        )
        return True, None
    except Exception as e:
        return False, e
