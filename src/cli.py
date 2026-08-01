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

cli.add_typer(crawler, name="crawler")
cli.add_typer(content, name="content")
cli.add_typer(sources, name="sources")
cli.add_typer(hostnames, name="hostnames")


@cli.command("init")
def init():
    init_database()


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


if __name__ == "__main__":
    cli()
