"""Offline comparison of suggestion rankers: algorithm vs. tev1 vs. laya.

Read-only. Pulls the last N days of COMPLETED content and the interaction log
from Appwrite, then ranks the same candidate set three ways:

* ``algorithm`` — FeedX's current interest x freshness blend (the exact scoring
  from ``src/feed/feed.py``, recomputed here WITHOUT the TagScore upsert so the
  eval writes nothing to the DB).
* ``tev1`` — rank by an Ollama ``tev1`` cross-encoder's relevance of each post to
  a synthesized interest profile, gated by the same freshness term.
* ``laya`` — same, using the in-process laya model.

"Better" is scored against **implicit labels**: the content a user actually
engaged with positively (open/read/like/bookmark/share) in the window, with HIDE
as a negative. This is an exposure-biased proxy (the current algorithm chose what
was shown), so treat the metrics as directional and read the side-by-side lists
too. Nothing is written back.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..database.models import (
    ContentWithId,
    InteractionType,
    InteractionWithTime,
    TagScore,
)
from ..feed.feed import (
    CONTENT_HALF_LIFE_DAYS,
    FEED_CONTINUITY_RATIO,
    FEED_NOVELTY_RATIO,
    FEED_RELEVANCE_RATIO,
    LONG_HALF_LIFE_DAYS,
    RECENT_HALF_LIFE_DAYS,
    _as_utc,
    get_content,
    get_decay,
    get_interactions,
)

POSITIVE_TYPES = {
    InteractionType.OPEN,
    InteractionType.READ,
    InteractionType.LIKE,
    InteractionType.BOOKMARK,
    InteractionType.SHARE,
}


@dataclass
class RankedItem:
    content: ContentWithId
    relevance: float  # method's relevance signal in [0, 1]
    freshness: float
    score: float  # relevance-gated-by-freshness, what we rank on


@dataclass
class MethodResult:
    name: str
    ranking: list[RankedItem]
    seconds: float
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Read-only re-implementation of feed.py's tag matrix + freshness.
# --------------------------------------------------------------------------- #
def build_tag_matrix(
    interactions: list[InteractionWithTime],
) -> tuple[dict[str, TagScore], set[str]]:
    """Decayed, [0,1]-normalized per-tag weights. Mirrors feed.get_interaction_matrix
    but performs NO upsert."""
    grouped: dict[str, list[InteractionWithTime]] = {}
    hidden: set[str] = set()
    for interaction in interactions:
        grouped.setdefault(interaction.tag, []).append(interaction)
        if interaction.type == InteractionType.HIDE:
            hidden.add(interaction.tag)

    matrix: dict[str, TagScore] = {}
    for tag, rows in grouped.items():
        recent = sum(
            get_decay(r.weight, event_age=r.age, half_life_value=RECENT_HALF_LIFE_DAYS)
            for r in rows
        )
        long = sum(
            get_decay(r.weight, event_age=r.age, half_life_value=LONG_HALF_LIFE_DAYS)
            for r in rows
        )
        matrix[tag] = TagScore(tag=tag, recent_weight=recent, long_weight=long)

    max_recent = max((t.recent_weight for t in matrix.values()), default=1) or 1
    max_long = max((t.long_weight for t in matrix.values()), default=1) or 1
    max_recent = max_recent if max_recent > 0 else 1
    max_long = max_long if max_long > 0 else 1
    for t in matrix.values():
        t.recent_weight /= max_recent
        t.long_weight /= max_long
    return matrix, hidden


def freshness_of(content: ContentWithId, now: datetime, hidden: set[str]) -> float:
    """The exact freshness term feed.py gates every content with."""
    suppressed = 0.1 if any(tag in hidden for tag in content.tags) else 1.0
    content_age = (now - _as_utc(content.scraped_at)).days
    age_factor = get_decay(event_age=content_age, half_life_value=CONTENT_HALF_LIFE_DAYS)
    shown_factor = 1.0
    if content.last_shown_at:
        days_since = (now - _as_utc(content.last_shown_at)).days
        shown_factor = min(max(days_since / 3, 0.15), 1)
    return age_factor * shown_factor * suppressed


def rank_algorithm(
    contents: list[ContentWithId],
    matrix: dict[str, TagScore],
    hidden: set[str],
    now: datetime,
) -> list[RankedItem]:
    items: list[RankedItem] = []
    for content in contents:
        continuity = 0.0
        relevance = 0.0
        for tag in content.tags:
            ts = matrix.get(tag, TagScore(tag=tag))
            continuity = max(continuity, ts.recent_weight)
            relevance += ts.long_weight
        if content.tags:
            relevance /= len(content.tags)
        novelty = 1 - relevance
        interest = (
            FEED_CONTINUITY_RATIO * continuity
            + FEED_RELEVANCE_RATIO * relevance
            + FEED_NOVELTY_RATIO * novelty
        )
        freshness = freshness_of(content, now, hidden)
        items.append(
            RankedItem(
                content=content,
                relevance=interest,
                freshness=freshness,
                score=interest * freshness,
            )
        )
    items.sort(key=lambda i: i.score, reverse=True)
    return items


# --------------------------------------------------------------------------- #
# Reranker-based ranking (tev1 / laya).
# --------------------------------------------------------------------------- #
def build_interest_query(matrix: dict[str, TagScore], hidden: set[str], top_n: int) -> str:
    ranked_tags = sorted(
        (t for t in matrix.values() if t.tag not in hidden),
        key=lambda t: t.long_weight,
        reverse=True,
    )
    top = [t.tag for t in ranked_tags[:top_n] if t.long_weight > 0]
    if not top:
        top = [t.tag for t in ranked_tags[:top_n]]
    return ", ".join(top)


def _content_text(content: ContentWithId) -> str:
    parts = [content.title or "", content.summary or ""]
    if not content.summary and content.chunks:
        parts.append(" ".join(content.chunks)[:1500])
    return "\n".join(p for p in parts if p).strip() or (content.url or "")


def rank_with_reranker(
    reranker,
    query: str,
    contents: list[ContentWithId],
    hidden: set[str],
    now: datetime,
) -> list[RankedItem]:
    texts = [_content_text(c) for c in contents]
    scores = reranker(query, "", texts)  # one P(true) per content
    items: list[RankedItem] = []
    for content, relevance in zip(contents, scores):
        freshness = freshness_of(content, now, hidden)
        items.append(
            RankedItem(
                content=content,
                relevance=float(relevance),
                freshness=freshness,
                score=float(relevance) * freshness,
            )
        )
    items.sort(key=lambda i: i.score, reverse=True)
    return items


# --------------------------------------------------------------------------- #
# Labels + metrics.
# --------------------------------------------------------------------------- #
def build_gains(interactions: list[InteractionWithTime]) -> dict[str, float]:
    """content_id -> net engagement gain (positive types add weight, HIDE subtracts)."""
    gains: dict[str, float] = {}
    for interaction in interactions:
        if interaction.type in POSITIVE_TYPES:
            gains[interaction.content_id] = gains.get(interaction.content_id, 0.0) + abs(
                interaction.weight
            )
        elif interaction.type == InteractionType.HIDE:
            gains[interaction.content_id] = gains.get(interaction.content_id, 0.0) - abs(
                interaction.weight
            )
    return gains


def _dcg(gain_sequence: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gain_sequence))


def metrics_for(
    ranking: list[RankedItem], gains: dict[str, float], k: int
) -> dict[str, float]:
    top = ranking[:k]
    pos_gains = {cid: g for cid, g in gains.items() if g > 0}
    total_positive = len(pos_gains)

    hits = [1 for item in top if gains.get(item.content.id, 0.0) > 0]
    precision = sum(hits) / len(top) if top else 0.0
    recall = (sum(hits) / total_positive) if total_positive else 0.0

    gseq = [max(gains.get(item.content.id, 0.0), 0.0) for item in top]
    ideal = sorted((g for g in pos_gains.values()), reverse=True)[:k]
    idcg = _dcg(ideal)
    ndcg = (_dcg(gseq) / idcg) if idcg > 0 else 0.0
    return {
        "precision@k": precision,
        "recall@k": recall,
        "ndcg@k": ndcg,
        "positive_labels": float(total_positive),
    }


def overlap_at_k(a: list[RankedItem], b: list[RankedItem], k: int) -> float:
    ids_a = {i.content.id for i in a[:k]}
    ids_b = {i.content.id for i in b[:k]}
    union = ids_a | ids_b
    return (len(ids_a & ids_b) / len(union)) if union else 0.0


# --------------------------------------------------------------------------- #
# Orchestration + report.
# --------------------------------------------------------------------------- #
def _ollama_host() -> str:
    host = os.environ.get("SUGGESTION_EVAL_OLLAMA_HOST")
    if host:
        return host
    url = os.environ.get("OLLAMA_URL")
    if url:
        # strip an OpenAI-style /v1 suffix; tev1 uses /v1/systemone off the root
        return url.rstrip("/").removesuffix("/v1")
    return "http://localhost:11434"


def run_eval(
    *,
    methods: list[str],
    window_days: int,
    top_k: int,
    interest_tags: int,
    max_candidates: int,
    tev1_model: str,
) -> dict:
    now = datetime.now(timezone.utc)
    interactions = get_interactions()
    matrix, hidden = build_tag_matrix(interactions)
    gains = build_gains(interactions)

    contents = get_content(window_days=window_days)
    # Keep the candidate set bounded (rerankers do one model call per candidate).
    if max_candidates and len(contents) > max_candidates:
        contents = contents[:max_candidates]

    interest_query = build_interest_query(matrix, hidden, interest_tags)

    results: list[MethodResult] = []
    for method in methods:
        started = time.perf_counter()
        notes: list[str] = []
        try:
            if method == "algorithm":
                ranking = rank_algorithm(contents, matrix, hidden, now)
            elif method == "tev1":
                from domdistill import Tev1Reranker

                reranker = Tev1Reranker(model=tev1_model, host=_ollama_host())
                ranking = rank_with_reranker(
                    reranker, interest_query, contents, hidden, now
                )
            elif method == "laya":
                from domdistill import LayaReranker

                ranking = rank_with_reranker(
                    LayaReranker(), interest_query, contents, hidden, now
                )
            else:
                raise ValueError(f"unknown method: {method!r}")
        except Exception as exc:
            # One reranker failing (e.g. laya/torch unavailable, Ollama down)
            # must not sink the whole comparison.
            notes.append(f"skipped: {type(exc).__name__}: {exc}")
            ranking = []
        results.append(
            MethodResult(
                name=method,
                ranking=ranking,
                seconds=time.perf_counter() - started,
                notes=notes,
            )
        )

    return {
        "now": now.isoformat(),
        "window_days": window_days,
        "top_k": top_k,
        "candidates": len(contents),
        "interest_query": interest_query,
        "results": results,
        "gains": gains,
    }


def render_report(outcome: dict) -> str:
    k = outcome["top_k"]
    gains = outcome["gains"]
    results: list[MethodResult] = outcome["results"]
    succeeded = [r for r in results if r.ranking]
    reference = succeeded[0] if succeeded else None
    positive_in_window = sum(
        1
        for item in (reference.ranking if reference else [])
        if gains.get(item.content.id, 0.0) > 0
    )

    lines: list[str] = []
    lines.append("# Suggestion ranker comparison")
    lines.append("")
    lines.append(
        f"- window: last **{outcome['window_days']} days** | candidates: "
        f"**{outcome['candidates']}** | top_k: **{k}**"
    )
    lines.append(f"- positive engagement labels among candidates: **{positive_in_window}**")
    lines.append(f"- interest profile (top tags): _{outcome['interest_query'] or 'n/a'}_")
    lines.append("")

    lines.append(f"## Quality vs. engagement labels (@{k})")
    lines.append("")
    lines.append("| Method | Precision | Recall | NDCG | Latency (s) | Note |")
    lines.append("|---|---|---|---|---|---|")
    scored = []
    for res in results:
        if not res.ranking:
            note = res.notes[0] if res.notes else "no ranking"
            lines.append(f"| {res.name} | — | — | — | {res.seconds:.1f} | {note} |")
            continue
        m = metrics_for(res.ranking, gains, k)
        scored.append((res.name, m["ndcg@k"]))
        lines.append(
            f"| {res.name} | {m['precision@k']:.3f} | {m['recall@k']:.3f} | "
            f"{m['ndcg@k']:.3f} | {res.seconds:.1f} | |"
        )
    lines.append("")
    if positive_in_window == 0:
        lines.append(
            "> ⚠️ No positive engagement labels in the window — metrics are all "
            "zero and not meaningful. Compare the side-by-side lists and latency "
            "instead, and/or widen the window."
        )
    else:
        best = max(scored, key=lambda x: x[1])
        lines.append(
            f"> **Best by NDCG@{k}: `{best[0]}` ({best[1]:.3f}).** "
            "Note: engagement is exposure-biased toward whatever the live "
            "algorithm already showed — read this as directional."
        )
    lines.append("")

    if len(succeeded) > 1:
        lines.append(f"## Cross-method agreement (overlap@{k})")
        lines.append("")
        for i in range(len(succeeded)):
            for j in range(i + 1, len(succeeded)):
                ov = overlap_at_k(succeeded[i].ranking, succeeded[j].ranking, k)
                lines.append(
                    f"- {succeeded[i].name} vs {succeeded[j].name}: **{ov:.2f}**"
                )
        lines.append("")

    if reference is None:
        lines.append("_No method produced a ranking; see notes above._")
        return "\n".join(lines)

    # Side-by-side top-N (by the reference method's order).
    show_n = min(k, 10)
    lines.append(f"## Top {show_n} (ordered by `{reference.name}`)")
    lines.append("")
    rank_maps = {
        res.name: {item.content.id: idx + 1 for idx, item in enumerate(res.ranking)}
        for res in succeeded
    }
    header = "| # | Title | Tags | " + " | ".join(f"{r.name} rank" for r in succeeded) + " | engaged |"
    lines.append(header)
    lines.append("|" + "---|" * (4 + len(succeeded)))
    for idx, item in enumerate(reference.ranking[:show_n], start=1):
        c = item.content
        title = (c.title or c.url or c.id)[:60].replace("|", "/")
        tags = ", ".join(c.tags[:4])[:50].replace("|", "/")
        ranks = " | ".join(str(rank_maps[r.name].get(c.id, "-")) for r in succeeded)
        engaged = "✅" if gains.get(c.id, 0.0) > 0 else ("🚫" if gains.get(c.id, 0.0) < 0 else "")
        lines.append(f"| {idx} | {title} | {tags} | {ranks} | {engaged} |")
    lines.append("")
    return "\n".join(lines)
