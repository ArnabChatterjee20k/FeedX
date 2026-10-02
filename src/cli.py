import typer
import asyncio
from dotenv import load_dotenv
from . import get_worker_pool, get_content_worker_pool, get_feed_worker_pool
from .database import init_database

load_dotenv()
cli = typer.Typer()

# for sub commands
crawler = typer.Typer()
content = typer.Typer()
sources = typer.Typer()
hostnames = typer.Typer()
evaluate = typer.Typer()

cli.add_typer(crawler, name="crawler")
cli.add_typer(content, name="content")
cli.add_typer(sources, name="sources")
cli.add_typer(hostnames, name="hostnames")
cli.add_typer(evaluate, name="eval")


@cli.command("init")
def init():
    init_database()


@cli.command("dedupe")
def dedupe_cmd(
    apply: bool = typer.Option(
        False, "--apply", help="actually delete; without it this only reports"
    ),
    table: list[str] = typer.Option(None, help="limit to these tables"),
):
    """Collapse rows duplicating a unique column. Run before `init` rebuilds indexes."""
    from .database.dedupe import dedupe

    report = dedupe(apply=apply, tables=list(table) if table else None)

    for name, entry in report["tables"].items():
        typer.echo(f"\n{name}")
        typer.echo(f"  rows                {entry['rows']}")
        typer.echo(f"  distinct            {entry['distinct']}")
        typer.echo(f"  duplicated values   {entry['duplicated_values']}")
        typer.echo(f"  rows to delete      {entry['to_delete']}")
        for count, value in entry["worst"]:
            typer.echo(f"    {count:>4}x  {value}")
        if entry["deleted"]:
            typer.echo(f"  DELETED             {entry['deleted']}")
        for err in entry["errors"][:10]:
            typer.echo(f"  error: {err}", err=True)

    if not apply:
        typer.echo("\nDRY RUN — nothing deleted. Re-run with --apply.")


@cli.command("sync-secrets")
def sync_secrets_cmd(
    repo: str = typer.Option(
        None, help="runner repo owner/name; defaults to GITHUB_SYNC_REPO"
    ),
    token: str = typer.Option(
        None, help="admin PAT for the runner repo; defaults to GITHUB_SYNC_TOKEN"
    ),
    env_file: str = typer.Argument(".env", help="local env file to read values from"),
    manifest: str = typer.Option(
        ".env.example", help="manifest tagging each name [secret]/[var]"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="show the diff without writing anything"
    ),
):
    """Push local .env values to a repo's GitHub Actions secrets/variables."""
    from .feed.github import sync_secrets

    try:
        repo, rows = sync_secrets(repo, token, env_file, manifest, dry_run)
    except Exception as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    typer.echo(f"{'DRY RUN — ' if dry_run else ''}target repo: {repo}\n")
    table = [("NAME", "KIND", "ON REPO", "ACTION")] + [
        (r["name"], r["kind"], "yes" if r["present"] else "no", r["action"])
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(4)]
    for row in table:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


@cli.command("feed")
def get_feed():
    pool = get_feed_worker_pool()
    pool.start()


@cli.command("serve")
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    prod: bool = typer.Option(
        False, "--prod", help="use `fastapi run` (no reload) instead of `fastapi dev`"
    ),
):
    """Run the API + admin page via the FastAPI CLI (http://<host>:<port>/admin)."""
    import os
    import subprocess

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    mode = "run" if prod else "dev"
    subprocess.run(
        [
            "fastapi",
            mode,
            "main.py",
            "--app",
            "api",
            "--host",
            host,
            "--port",
            str(port),
        ],
        env=env,
    )


async def _run_pool(pool):
    try:
        await pool.start()
    except Exception as e:
        print("error")
        typer.echo(str(e), err=True)
        typer.Exit(1)
    finally:
        print("closing pool")
        await pool.stop()


@crawler.command("crawl")
def crawl(workers: int = 1):
    asyncio.run(_run_pool(get_worker_pool(workers)))


@content.command("run")
def content_run(workers: int = 1):
    asyncio.run(_run_pool(get_content_worker_pool(workers)))


@crawler.command("stats")
def stats():
    pass


@evaluate.command("suggestions")
def eval_suggestions(
    methods: str = typer.Option(
        None,
        help="comma-separated: algorithm,tev1,laya (env SUGGESTION_EVAL_METHODS)",
    ),
    window_days: int = typer.Option(
        None, help="candidate window (env SUGGESTION_EVAL_WINDOW_DAYS, default 7)"
    ),
    top_k: int = typer.Option(
        None, help="ranking depth for metrics (env SUGGESTION_EVAL_TOP_K, default 20)"
    ),
    interest_tags: int = typer.Option(
        None, help="top interest tags in the reranker query (default 12)"
    ),
    max_candidates: int = typer.Option(
        None,
        help="cap candidates; rerankers do one model call each (default 80)",
    ),
    tev1_model: str = typer.Option(
        None, help="Ollama tev1 model tag (default tev1:0.8b)"
    ),
    markdown_out: str = typer.Option(
        None, help="also write the Markdown report to this path"
    ),
):
    """Compare suggestion rankers (algorithm vs tev1 vs laya) read-only.

    Writes nothing to the database. Needs Appwrite env configured; tev1 needs a
    reachable Ollama (OLLAMA_URL / SUGGESTION_EVAL_OLLAMA_HOST), laya needs the
    laya package installed.
    """
    import os

    from .eval.suggestions import render_report, run_eval

    methods_value = methods or os.environ.get(
        "SUGGESTION_EVAL_METHODS", "algorithm,tev1,laya"
    )
    method_list = [m.strip() for m in methods_value.split(",") if m.strip()]

    def _int(opt, env, default):
        if opt is not None:
            return opt
        return int(os.environ.get(env, default))

    outcome = run_eval(
        methods=method_list,
        window_days=_int(window_days, "SUGGESTION_EVAL_WINDOW_DAYS", 7),
        top_k=_int(top_k, "SUGGESTION_EVAL_TOP_K", 20),
        interest_tags=_int(interest_tags, "SUGGESTION_EVAL_INTEREST_TAGS", 12),
        max_candidates=_int(max_candidates, "SUGGESTION_EVAL_MAX_CANDIDATES", 80),
        tev1_model=tev1_model
        or os.environ.get("SUGGESTION_EVAL_TEV1_MODEL", "tev1:0.8b"),
    )
    report = render_report(outcome)
    typer.echo(report)
    if markdown_out:
        with open(markdown_out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")


if __name__ == "__main__":
    cli()
