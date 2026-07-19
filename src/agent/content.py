import os
import re
from typing import Any

from pydantic import BaseModel, Field, create_model, field_validator
from pydantic_ai import Agent
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.output import NativeOutput
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.settings import ModelSettings

# Small local models have a tight context window, so cap how much text we feed
# in. The chunker already trims boilerplate; this is just a safety bound.
MAX_INPUT_CHARS = 8_000

DEFAULT_MODEL = "llama3.2:1b"
DEFAULT_BASE_URL = (
    "http://localhost:11434/v1"  # OpenAI-compatible endpoint (note the /v1)
)

# Env var holding the controlled tag vocabulary, e.g.
#   CONTENT_TAGS="python,web crawling,machine learning,databases"
TAGS_ENV_VAR = "CONTENT_TAGS"

MAX_TAGS = 8

# A page must have some substance before it is worth an LLM call. Reject content
# that is a single line or just a few characters (nav text, titles, boilerplate).
MIN_CONTENT_LINES = 3
MIN_CONTENT_CHARS = 200


class ThinContentError(ValueError):
    """Raised when page content is too short/sparse to analyse.

    Callers (e.g. the content worker) should catch this and skip the page or
    mark it as a pipeline error rather than store a meaningless summary.
    """


def normalize_tags(tags: list[str]) -> list[str]:
    """Lowercase, strip, drop empties, and dedupe while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def load_allowed_tags(env_var: str = TAGS_ENV_VAR) -> list[str]:
    """Read the controlled tag vocabulary from an env var.

    Accepts comma- or newline-separated values. Returns an empty list when the
    var is unset, which means "no allowlist" (free-form tags).
    """
    raw = os.environ.get(env_var, "")
    parts = raw.replace("\n", ",").split(",")
    return normalize_tags(parts)


class ContentAnalysis(BaseModel):
    """Structured result of analysing one page.

    Field order here is the order the model is asked to produce them in and the
    order they are validated in — keep `summary` first so the model reasons
    about the gist before committing to tags.
    """

    summary: str = Field(
        description=(
            "A faithful, self-contained summary of the page in 2-4 sentences. "
            "Only use information present in the provided content; never invent "
            "facts, names, or numbers."
        )
    )
    tags: list[str] = Field(
        description=(
            "Topical tags chosen only from the allowed list. "
            "Lowercase, no duplicates."
        ),
        max_length=MAX_TAGS,
    )

    @field_validator("tags")
    @classmethod
    def _normalize(cls, tags: list[str]) -> list[str]:
        return normalize_tags(tags)


def _tag_field_name(tag: str, idx: int) -> str:
    """A stable, valid Python identifier for a tag's boolean field.

    The index keeps names unique even if two tags slugify to the same thing.
    """
    slug = re.sub(r"[^0-9a-z]+", "_", tag.lower()).strip("_")
    return f"tag_{idx}_{slug}" if slug else f"tag_{idx}"


def _build_output_model(
    allowed_tags: list[str],
) -> tuple[type[BaseModel], dict[str, str] | None]:
    """Return the structured-output schema (and, for allowlists, its field map).

    Free-form mode -> plain `ContentAnalysis` with a `list[str]` of tags, and a
    ``None`` field map.

    Allowlist mode -> instead of asking for a *subset list* (which small models
    answer by dumping the whole vocabulary), we expose ONE boolean field per
    allowed tag. Each tag then becomes an independent, grounded yes/no decision
    with ``false`` as the natural default, so the model has to justify every tag
    it keeps. The returned field map (``field_name -> original_tag``) is used to
    decode the booleans back into a tag list.
    """
    if not allowed_tags:
        return ContentAnalysis, None

    fields: dict[str, tuple[type, Any]] = {
        "summary": (
            str,
            Field(
                description=(
                    "A faithful, self-contained summary of the page in 2-4 "
                    "sentences. Only use information present in the content; "
                    "never invent facts, names, or numbers."
                )
            ),
        ),
    }
    field_map: dict[str, str] = {}
    for idx, tag in enumerate(allowed_tags):
        fname = _tag_field_name(tag, idx)
        field_map[fname] = tag
        fields[fname] = (
            bool,
            Field(
                default=False,
                description=(
                    f'Set true ONLY if the page is substantially about "{tag}" '
                    f'— i.e. "{tag}" is one of its main subjects. Set false if '
                    "the topic is merely mentioned in passing, only loosely "
                    "related, from a neighbouring field, or absent."
                ),
            ),
        )

    model = create_model("ConstrainedContentAnalysis", **fields)
    return model, field_map


def _build_system_prompt(allowed_tags: list[str]) -> str:
    base = """\
You are a precise content-analysis assistant for a web crawler.

Given the text of a single web page, you produce:
  - summary: a concise, faithful 2-4 sentence summary of what the page is about.
  - tags: topical tags naming the main subjects.

Rules:
  - Ground every statement in the provided text. Do not add outside knowledge,
    speculation, or details that are not present.
  - If the content is thin, low quality, or unclear, summarise only what is
    actually there and keep it short rather than padding.
  - Respond only via the required structured output. Do not add commentary.\
"""
    if allowed_tags:
        tag_list = "\n".join(f"  - {t}" for t in allowed_tags)
        base += (
            "\n\nTopic vocabulary (CLOSED SET). For each of these candidate "
            "topics you must decide INDEPENDENTLY whether the page is "
            f"substantially about it (one boolean per topic):\n{tag_list}"
            "\n\nDecision rule — be strict and selective:\n"
            "  - Mark a topic true ONLY if it is one of the MAIN subjects of the "
            "page.\n"
            "  - Mark it false if the page merely mentions it, is only loosely "
            "related, or is about a neighbouring field.\n"
            "  - Most pages are about only 1-3 of these topics. Marking many — or "
            "all — of them true is almost always wrong.\n"
            "  - It is completely fine, and common, for EVERY topic to be false "
            "when none genuinely fit. Never force a topic true.\n\n"
            "Example (illustrative vocabulary, not the one above):\n"
            "  Candidate topics: cooking, travel, finance, gardening\n"
            "  Page about tax-loss harvesting in a brokerage account\n"
            "    -> finance = true; cooking, travel, gardening = false\n"
            "  Page that is a tutorial on training a neural network\n"
            "    -> all four false (none of these topics fit the page)"
        )
    else:
        base += (
            "\n\nTags must be lowercase, 1-3 words each, deduplicated, and "
            'specific (prefer "async python" over "programming").'
        )
    return base


def _build_config() -> tuple[str, str]:
    """Resolve model name and base URL from the environment, with sane defaults."""
    model_name = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    base_url = (
        os.environ.get("OLLAMA_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or DEFAULT_BASE_URL
    )
    return model_name, base_url


class ContentAgent:
    """Analyses crawled page content into a structured summary + constrained tags.

    The controlled tag vocabulary is supplied *per call* (to `analyze` /
    `analyze_async` / `analyze_chunks`), so a single agent instance can be
    reused across different tag sets — e.g. a different allowlist per hostname
    pulled from the DB. When `allowed_tags` is omitted on a call, it falls back
    to the ``CONTENT_TAGS`` env var; an empty result means free-form tagging.

    Args:
        temperature: Sampling temperature; 0.0 for deterministic extraction.
    """

    def __init__(self, *, temperature: float = 0.0) -> None:
        model_name, base_url = _build_config()
        model = OllamaModel(model_name, provider=OllamaProvider(base_url=base_url))

        # A single base agent; the tag-dependent output schema and system prompt
        # are supplied per run via `output_type` / `instructions`.
        self._agent: Agent[None, str] = Agent(
            model=model,
            model_settings=ModelSettings(temperature=temperature),
            retries=2,
        )

        # Cache the (schema, instructions, allowed_set, field_map) per tag
        # vocabulary so we don't rebuild a dynamic model on every call. The
        # field_map is None in free-form mode and {field_name: tag} for an
        # allowlist (see `_build_output_model`).
        self._cache: dict[
            tuple[str, ...],
            tuple[NativeOutput, str, set[str], dict[str, str] | None],
        ] = {}

    def _prepare(
        self, allowed_tags: list[str] | None
    ) -> tuple[NativeOutput, str, set[str], dict[str, str] | None]:
        allowed = tuple(
            normalize_tags(
                allowed_tags if allowed_tags is not None else load_allowed_tags()
            )
        )
        cached = self._cache.get(allowed)
        if cached is None:
            model, field_map = _build_output_model(list(allowed))
            output = NativeOutput(model)
            instructions = _build_system_prompt(list(allowed))
            cached = (output, instructions, set(allowed), field_map)
            self._cache[allowed] = cached
        return cached

    @staticmethod
    def _validate_content(content: str) -> str:
        """Ensure the page has enough substance to analyse.

        Requires at least a few non-empty lines and a minimum character count,
        so single-line/boilerplate pages are rejected before an LLM call.
        """
        text = content.strip()
        line_count = sum(1 for ln in text.splitlines() if ln.strip())
        if len(text) < MIN_CONTENT_CHARS or line_count < MIN_CONTENT_LINES:
            raise ThinContentError(
                f"content too thin to analyse: {line_count} non-empty line(s), "
                f"{len(text)} char(s) "
                f"(need >= {MIN_CONTENT_LINES} lines and >= {MIN_CONTENT_CHARS} chars)"
            )
        return text

    @staticmethod
    def _build_prompt(content: str, *, title: str | None, url: str | None) -> str:
        text = content.strip()
        if len(text) > MAX_INPUT_CHARS:
            text = text[:MAX_INPUT_CHARS].rstrip() + " …[truncated]"

        parts: list[str] = []
        if title:
            parts.append(f"Title: {title.strip()}")
        if url:
            parts.append(f"URL: {url.strip()}")
        parts.append("Content:\n" + text)
        return "\n".join(parts)

    @staticmethod
    def _to_result(
        output: BaseModel,
        allowed_set: set[str],
        field_map: dict[str, str] | None,
    ) -> ContentAnalysis:
        """Coerce raw model output into a stable `ContentAnalysis`.

        Allowlist mode decodes the per-tag booleans back into a tag list;
        free-form mode reads the `tags` list directly. Either way we filter
        against the allowlist once more as a safety net.
        """
        if field_map is None:
            raw_tags = [str(t) for t in output.tags]  # type: ignore[attr-defined]
        else:
            raw_tags = [
                tag for fname, tag in field_map.items() if getattr(output, fname, False)
            ]

        tags = normalize_tags(raw_tags)
        if allowed_set:
            tags = [t for t in tags if t in allowed_set]
        return ContentAnalysis(summary=output.summary, tags=tags[:MAX_TAGS])  # type: ignore[attr-defined]

    def analyze(
        self,
        content: str,
        *,
        allowed_tags: list[str] | None = None,
        title: str | None = None,
        url: str | None = None,
    ) -> ContentAnalysis:
        """Analyse a page synchronously and return structured output.

        Raises:
            ThinContentError: if the content is too short/sparse to analyse.
        """
        content = self._validate_content(content)
        output_type, instructions, allowed_set, field_map = self._prepare(allowed_tags)
        prompt = self._build_prompt(content, title=title, url=url)
        result = self._agent.run_sync(
            prompt, output_type=output_type, instructions=instructions
        )
        return self._to_result(result.output, allowed_set, field_map)

    async def analyze_async(
        self,
        content: str,
        *,
        allowed_tags: list[str] | None = None,
        title: str | None = None,
        url: str | None = None,
    ) -> ContentAnalysis:
        """Async variant of :meth:`analyze` for use inside an event loop.

        Raises:
            ThinContentError: if the content is too short/sparse to analyse.
        """
        content = self._validate_content(content)
        output_type, instructions, allowed_set, field_map = self._prepare(allowed_tags)
        prompt = self._build_prompt(content, title=title, url=url)
        result = await self._agent.run(
            prompt, output_type=output_type, instructions=instructions
        )
        return self._to_result(result.output, allowed_set, field_map)

    def analyze_chunks(
        self,
        chunks: list[str],
        *,
        allowed_tags: list[str] | None = None,
        title: str | None = None,
        url: str | None = None,
    ) -> ContentAnalysis:
        """Convenience wrapper for the crawler's `Content.chunks` field."""
        return self.analyze(
            "\n\n".join(chunks), allowed_tags=allowed_tags, title=title, url=url
        )
