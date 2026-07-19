"""Evals for `src.agent.content.ContentAgent`.

These are *evals*, not unit tests: they run the real Ollama-backed agent over a
table of realistic pages (different chunks / allowed tags / title / url) and
grade the structured output with a mix of hard-contract graders and fuzzy
quality graders.

  Hard contracts (must always hold):
    - tags stay inside the allowlist when one is given
    - tags are lowercase, deduped, non-empty
    - thin content raises ThinContentError (agent refuses to hallucinate)
    - error expectation matches (raised vs. clean run)

  Fuzzy quality (relevance of a small local model — graded leniently):
    - the summary has real substance (word count)
    - the summary mentions at least one expected keyword (grounding proxy)
    - at least one expected tag is picked
    - no clearly-wrong tag is picked

Run it:
    uv run python -m evals.content_agent_eval
or
    uv run python evals/content_agent_eval.py

Requires a local Ollama serving the model (defaults: llama3.2:1b on
http://localhost:11434). Override with OLLAMA_MODEL / OLLAMA_URL.

Expected result on the default llama3.2:1b: ~96% (2 known misses). All hard
contracts pass; the two failures are documented 1B judgment limits that pull in
opposite directions (see the `async_python_allowlist` and
`offtopic_vocab_returns_empty_tags` cases). A 3B model closes both.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow `python evals/content_agent_eval.py` from anywhere by putting the
# project root on the path (running as `-m` already handles this).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic_evals import Case, Dataset  # noqa: E402
from pydantic_evals.evaluators import Evaluator, EvaluatorContext  # noqa: E402

from src.agent.content import (  # noqa: E402
    ContentAgent,
    ThinContentError,
    normalize_tags,
)


# --------------------------------------------------------------------------- #
# Inputs / output / expectations
# --------------------------------------------------------------------------- #
@dataclass
class Inputs:
    """One page fed to the agent."""

    chunks: list[str]
    allowed_tags: list[str]
    title: str | None = None
    url: str | None = None


@dataclass
class Output:
    """Normalised agent result, so a raised error is just another outcome."""

    summary: str
    tags: list[str]
    error: str | None  # exception class name, or None on a clean run


@dataclass
class Expect:
    """Per-case grading knobs; empty fields mean "grader is n/a → passes"."""

    expect_error: str | None = None
    expected_tags: list[str] = field(default_factory=list)
    forbidden_tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    min_summary_words: int = 8
    expect_empty_tags: bool = False


# One shared agent across all cases (temperature 0 → as deterministic as the
# model gets), mirroring how the content worker reuses a single instance.
_agent = ContentAgent()


async def run_agent(inputs: Inputs) -> Output:
    text = "\n\n".join(inputs.chunks)
    try:
        result = await _agent.analyze_async(
            text,
            allowed_tags=inputs.allowed_tags,
            title=inputs.title,
            url=inputs.url,
        )
        return Output(summary=result.summary, tags=list(result.tags), error=None)
    except ThinContentError:
        return Output(summary="", tags=[], error="ThinContentError")


# --------------------------------------------------------------------------- #
# Graders
# --------------------------------------------------------------------------- #
@dataclass
class ErrorExpectation(Evaluator[Inputs, Output, Expect]):
    """Raised-vs-clean must match the case's expectation."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        return ctx.output.error == ctx.metadata.expect_error


@dataclass
class SummaryHasSubstance(Evaluator[Inputs, Output, Expect]):
    """A clean run must produce a non-trivial summary."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        if ctx.metadata.expect_error:  # n/a when we expect a raise
            return True
        return len(ctx.output.summary.split()) >= ctx.metadata.min_summary_words


@dataclass
class TagsSubsetOfAllowed(Evaluator[Inputs, Output, Expect]):
    """Hard contract: with an allowlist, no tag may fall outside it."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        allowed = set(normalize_tags(ctx.inputs.allowed_tags))
        if not allowed:  # free-form mode → nothing to enforce
            return True
        return all(tag in allowed for tag in ctx.output.tags)


@dataclass
class TagsWellFormed(Evaluator[Inputs, Output, Expect]):
    """Tags are lowercase, stripped, deduped, and non-empty."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        tags = ctx.output.tags
        return tags == normalize_tags(tags)


@dataclass
class ExpectedTagPresent(Evaluator[Inputs, Output, Expect]):
    """At least one of the expected tags should be picked (relevance)."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        expected = normalize_tags(ctx.metadata.expected_tags)
        if not expected:
            return True
        return any(tag in expected for tag in ctx.output.tags)


@dataclass
class ForbiddenTagAbsent(Evaluator[Inputs, Output, Expect]):
    """Clearly-irrelevant tags should not be picked."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        forbidden = set(normalize_tags(ctx.metadata.forbidden_tags))
        if not forbidden:
            return True
        return not any(tag in forbidden for tag in ctx.output.tags)


@dataclass
class SummaryMentionsKeyword(Evaluator[Inputs, Output, Expect]):
    """Grounding proxy: the summary references the page's subject matter."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        keywords = [k.lower() for k in ctx.metadata.keywords]
        if not keywords:
            return True
        summary = ctx.output.summary.lower()
        return any(k in summary for k in keywords)


@dataclass
class TagsEmptyWhenNothingApplies(Evaluator[Inputs, Output, Expect]):
    """When no vocabulary tag fits the page, the model should return none."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Output, Expect]) -> bool:
        if not ctx.metadata.expect_empty_tags:
            return True
        return len(ctx.output.tags) == 0


# --------------------------------------------------------------------------- #
# Sample pages
# --------------------------------------------------------------------------- #
BROWSER_CHUNKS = [
    "To me, browsers are where algorithms come to life. A browser contains a "
    "rendering engine more complex than any computer game, a full networking "
    "stack, a virtual machine with a just-in-time compiler, and a world-class "
    "security sandbox.",
    "When a user enters a URL, the browser resolves DNS, opens a connection, "
    "downloads the resource, parses the HTML incrementally, discovers more "
    "resources, and begins rendering before the page has finished downloading.",
    "Security is a defining characteristic of browsers. The same-origin policy, "
    "process isolation, site isolation, permission models, and content security "
    "policies work together to protect users from malicious websites.",
    "A useful mental model is a pipeline: network responses become parsed "
    "documents, documents become DOM trees, DOM trees combine with CSS into "
    "render trees, render trees become layouts, and layouts become pixels.",
]

ASYNC_PYTHON_CHUNKS = [
    "asyncio is Python's library for writing concurrent code using the "
    "async/await syntax. It runs a single-threaded event loop that schedules "
    "coroutines cooperatively instead of using OS threads.",
    "An await point yields control back to the event loop so other coroutines "
    "can run while a coroutine waits on I/O. This is why asyncio shines for "
    "network-bound workloads with thousands of concurrent connections.",
    "asyncio.to_thread offloads a blocking call to a thread pool so it does not "
    "stall the event loop, while asyncio.gather runs many awaitables "
    "concurrently and collects their results.",
    "Tasks wrap coroutines so they are scheduled independently, and a TaskGroup "
    "gives structured concurrency where child tasks are awaited and cancelled "
    "together on failure.",
]

DATABASE_CHUNKS = [
    "A B-tree index lets Postgres find rows without scanning the whole table. "
    "The planner chooses an index scan when it estimates that far fewer rows "
    "will be read than a sequential scan would touch.",
    "Transactions give atomicity and isolation. Under MVCC each transaction "
    "sees a consistent snapshot, and writers create new row versions instead of "
    "blocking readers.",
    "Composite indexes are ordered left-to-right, so a query filtering on the "
    "leading column benefits, but one filtering only on a trailing column "
    "usually cannot use the index.",
    "VACUUM reclaims space from dead tuples left behind by updates and deletes, "
    "and keeping statistics fresh helps the query planner pick good plans.",
]

ML_CHUNKS = [
    "A neural network learns by adjusting weights to minimise a loss function. "
    "Training runs forward passes to compute predictions and backpropagation to "
    "compute gradients of the loss with respect to each weight.",
    "Gradient descent nudges the weights in the direction that reduces the loss, "
    "and the learning rate controls the size of each step. Too large diverges, "
    "too small trains slowly.",
    "Overfitting happens when a model memorises the training set instead of "
    "generalising. Regularisation, dropout, and more data all help the model "
    "perform better on unseen examples.",
    "A validation set held out from training is used to tune hyperparameters and "
    "to decide when to stop training before the model starts overfitting.",
]

NETWORKING_CHUNKS = [
    "TCP provides a reliable, ordered byte stream on top of the unreliable IP "
    "layer. It uses sequence numbers, acknowledgements, and retransmission to "
    "recover from lost packets.",
    "The three-way handshake (SYN, SYN-ACK, ACK) establishes a connection and "
    "synchronises sequence numbers before any application data is exchanged.",
    "Congestion control algorithms like slow start and congestion avoidance "
    "adjust the sending rate in response to packet loss so the network is not "
    "overwhelmed.",
    "TLS runs on top of TCP to encrypt the byte stream, authenticate the server "
    "with a certificate, and negotiate keys during its own handshake.",
]

# A vocabulary that clearly does NOT fit a cooking page — used to check the
# model returns an empty tag list rather than forcing an off-topic tag.
COOKING_CHUNKS = [
    "Bring a large pot of well-salted water to a rolling boil before adding the "
    "pasta, and stir in the first minute so the strands do not stick together.",
    "Reserve a cup of the starchy cooking water before draining; it emulsifies "
    "with the fat to bind the sauce to the noodles.",
    "Finish the pasta in the pan with the sauce for the last minute so it "
    "absorbs flavour, then toss with grated cheese off the heat.",
]

TECH_VOCAB = [
    "python",
    "databases",
    "web",
    "machine learning",
    "security",
    "networking",
]


cases: list[Case[Inputs, Output, Expect]] = [
    Case(
        name="browser_engineering_allowlist",
        inputs=Inputs(
            chunks=BROWSER_CHUNKS,
            allowed_tags=TECH_VOCAB,
            title="Browser Engineering",
            url="https://browser.engineering/intro.html",
        ),
        metadata=Expect(
            expected_tags=["web", "security"],
            forbidden_tags=["machine learning"],
            keywords=["browser", "render", "security", "web"],
        ),
    ),
    # KNOWN 1B LIMIT (recall miss): llama3.2:1b latches onto "network-bound",
    # "concurrent connections", "I/O" and marks `networking` instead of
    # `python`, so ExpectedTagPresent fails. A 3B model tags this correctly.
    # Kept as-is to document the small-model ceiling, not to hide it.
    Case(
        name="async_python_allowlist",
        inputs=Inputs(
            chunks=ASYNC_PYTHON_CHUNKS,
            allowed_tags=TECH_VOCAB,
            title="A Tour of asyncio",
            url="https://example.com/asyncio",
        ),
        metadata=Expect(
            expected_tags=["python"],
            keywords=["async", "python", "coroutine", "event loop", "concurren"],
        ),
    ),
    Case(
        name="postgres_indexes_allowlist",
        inputs=Inputs(
            chunks=DATABASE_CHUNKS,
            allowed_tags=TECH_VOCAB,
            title="How Postgres Indexes Work",
            url="https://example.com/pg-indexes",
        ),
        metadata=Expect(
            expected_tags=["databases"],
            keywords=["index", "postgres", "transaction", "query", "table"],
        ),
    ),
    Case(
        name="neural_network_allowlist",
        inputs=Inputs(
            chunks=ML_CHUNKS,
            allowed_tags=TECH_VOCAB,
            title="Training Neural Networks",
            url="https://example.com/nn-training",
        ),
        metadata=Expect(
            expected_tags=["machine learning"],
            keywords=["train", "model", "loss", "gradient", "network"],
        ),
    ),
    Case(
        name="networking_freeform_tags",
        inputs=Inputs(
            chunks=NETWORKING_CHUNKS,
            allowed_tags=[],  # free-form: no allowlist to enforce
            title="TCP in Practice",
            url="https://example.com/tcp",
        ),
        metadata=Expect(
            keywords=["tcp", "packet", "connection", "network", "congestion"],
        ),
    ),
    # KNOWN 1B LIMIT (precision miss): the per-tag boolean schema cut this from
    # "all 3 tags" down to a single spurious `web`, but llama3.2:1b still won't
    # commit to a fully empty set here. A 3B model returns []. Kept to document
    # the ceiling.
    Case(
        name="offtopic_vocab_returns_empty_tags",
        inputs=Inputs(
            chunks=COOKING_CHUNKS,
            allowed_tags=["python", "databases", "web"],
            title="Cooking Pasta",
            url="https://example.com/pasta",
        ),
        metadata=Expect(
            expect_empty_tags=True,
            keywords=["pasta", "water", "sauce", "cook"],
            min_summary_words=6,
        ),
    ),
    Case(
        name="thin_content_is_rejected",
        inputs=Inputs(
            chunks=["Home"],  # far below MIN_CONTENT_CHARS / MIN_CONTENT_LINES
            allowed_tags=TECH_VOCAB,
            title="Nav",
            url="https://example.com/",
        ),
        metadata=Expect(expect_error="ThinContentError"),
    ),
]


dataset = Dataset[Inputs, Output, Expect](
    name="content_agent",
    cases=cases,
    evaluators=[
        ErrorExpectation(),
        SummaryHasSubstance(),
        TagsSubsetOfAllowed(),
        TagsWellFormed(),
        ExpectedTagPresent(),
        ForbiddenTagAbsent(),
        SummaryMentionsKeyword(),
        TagsEmptyWhenNothingApplies(),
    ],
)


def main() -> None:
    # The report uses ✔/✗ glyphs; force UTF-8 so it renders on the Windows
    # cp1252 console instead of raising UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Keep concurrency low: a single local model instance serves every case.
    report = dataset.evaluate_sync(run_agent, max_concurrency=2)
    # Inputs are long chunk lists; show outputs + per-assertion marks only.
    report.print(include_input=False, include_output=True)


if __name__ == "__main__":
    main()
