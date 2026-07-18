# FeedX
The idea is building feedline for me based on the sources I like. A recurring feed and timeline always prepared for me so that anytime I want to read something they are always ready

Built on top of
1. [Scout](https://github.com/ArnabChatterjee20k/Scout)  
2. [Domdistill](https://github.com/ArnabChatterjee20k/domdistill)
Some of the frameworks I built recently to solve this problem efficiently

# Working

Github actions trigger the jobs automatically or using the cli to run this manually

```mermaid
flowchart TD
    subgraph GHA[GitHub Actions - Scheduled Triggers]
        QB[queue-builder<br/>API]
        SC[scraper.yml]
        CO[content.yml]
    end
    QB --> DB[(Shared Database<br/>Appwrite)]
    SC --> DB
    CO --> DB
    DB --> FB[feed-builder.yml]
    FB --> HTML[Generate HTML]
    HTML --> GP[Push to gh-pages]
```

TODO: Make sure to guard via auth so that randomly requests can't get created
At every run the queues are formed
API -> just for content ingestion and data output
CLI -> to replicate the scheduler environment locally

# Pipeline Flow

The system is a chain of stages that hand work to each other through the shared
Appwrite database. Ingestion writes `URL` rows, the **crawl pipeline** turns URLs
into raw `Content`, and the **content pipeline** enriches that content into a
feed-ready form. Each stage is a pool of self-sufficient workers; every hand-off
is guarded by a DB-level atomic claim so multiple workers/processes never touch
the same row.

## Queue construction

At the start of a crawl run the in-memory queues are (re)built from the DB:

```mermaid
flowchart LR
    DB[(Appwrite)] --> FQ[FrontQueue<br/>URLs where crawl_state in QUEUED/RETRY<br/>and next_crawl_at &lt;= now]
    FQ --> BQ[BackQueue<br/>URLs partitioned per hostname<br/>deque per host]
    BQ --> SQ[SchedulerQueue<br/>min-heap of hostnames<br/>ordered by next_allowed_at]
    SQ --> W[CrawlWorker pool]
```

The `ContentQueue` is built independently from `Content` rows in `PENDING` state
(ordered by `scraped_at`) and lazily refills itself when it drains.

## Crawl pipeline (`workers/crawl_worker.py`)

One iteration of a crawl worker: pick a due hostname, lease it, claim a URL,
crawl it, dedup, and persist new content.

```mermaid
flowchart TD
    A[SchedulerQueue.pop_async<br/>hostname due by next_allowed_at] --> B{Lease hostname<br/>next_allowed_at &lt;= now -&gt; now+10m}
    B -- held elsewhere / error --> A
    B -- won --> C[BackQueue.pop_async<br/>next URL for hostname]
    C -- host queue empty --> A
    C --> D{Claim URL<br/>QUEUED/RETRY -&gt; FETCHING}
    D -- taken by other --> C2[reschedule hostname] --> A
    D -- transient error --> ERR
    D -- won --> E[Scout.crawl<br/>depth=5, page_limit=10<br/>include/exclude + virtual scroll]
    E --> F[fingerprint documents<br/>domdistill simhash]
    F --> G[dedup vs existing Content<br/>simhash OR-query + hamming similarity &gt; 0.6]
    G --> H[get_relevant_sections<br/>chunks per new document]
    H --> I[build Content rows<br/>pipeline_state = PENDING]
    I --> J[create_chunks<br/>bulk insert]
    J --> K[complete<br/>reschedule host +5m30s<br/>host stats++, URL = SUCCESS]
    K --> A
    E -. exception .-> ERR
    J -. insert fails .-> ERR[error<br/>reschedule host<br/>failure++, URL = RETRY]
    ERR --> A
```

## Content pipeline (`workers/content_worker.py`)

One iteration of a content worker: atomically claim a pending item, run it
through the LLM agent, and write back the summary/tags.

```mermaid
flowchart TD
    A[ContentQueue.pop_async] --> B{Atomic claim<br/>PENDING -&gt; SUMMARIZING}
    B -- buffer empty --> R[refill from DB<br/>PENDING, order by scraped_at]
    R -- nothing due --> S[sleep &amp; poll]
    S --> A
    R --> A
    B -- taken by other --> A
    B -- won --> C{chunks present?}
    C -- no --> X[error -&gt; FAILED]
    C -- yes --> D[ContentAgent.analyze_async<br/>Ollama LLM, retry x5]
    D -- all retries fail --> X
    D -- ok --> E[update_content<br/>summary + tags<br/>pipeline_state = COMPLETED]
    E --> F[complete]
```

## State machines

Rows advance through explicit states; the atomic claims are the guarded
transitions (bold arrows below).

```mermaid
stateDiagram-v2
    direction LR
    [*] --> QUEUED
    QUEUED --> FETCHING: claim (atomic)
    RETRY --> FETCHING: claim (atomic)
    FETCHING --> SUCCESS: complete()
    FETCHING --> RETRY: error()
    RETRY --> FAILED: planned (max retries)
    note right of FAILED: FAILED / BLOCKED defined<br/>but not wired yet
```

```mermaid
stateDiagram-v2
    direction LR
    [*] --> PENDING
    PENDING --> SUMMARIZING: claim (atomic)
    SUMMARIZING --> COMPLETED: analyzed
    SUMMARIZING --> FAILED: error / empty chunks
    note right of COMPLETED: EXTRACTING / TAGGING states<br/>reserved for finer granularity
```

## Concurrency — atomic claims

Every stage-to-stage hand-off is a conditional DB update; exactly one
worker/process can win, which makes the pipeline safe to run with multiple
workers and across parallel GitHub Action runners.

| Level | Where | Transition (only if precondition holds) |
|-------|-------|------------------------------------------|
| Hostname | `CrawlWorker._lease_hostname` | `next_allowed_at <= now` → `now + 10m` |
| URL | `CrawlWorker._claim` | `crawl_state in (QUEUED, RETRY)` → `FETCHING` |
| Content | `ContentQueue._claim` | `pipeline_state == PENDING` → `SUMMARIZING` |

> Known gap: a row claimed by a process that then crashes is never retried
> (stale `FETCHING` / `SUMMARIZING`). A `claimed_at` timestamp + a reaper that
> resets stale claims is a planned follow-up (see `plan.md`).