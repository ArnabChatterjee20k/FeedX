import typer
import asyncio
from dotenv import load_dotenv
from . import get_worker_pool, get_content_worker_pool
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
