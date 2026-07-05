import typer
import asyncio
from dotenv import load_dotenv
from . import get_worker_pool

load_dotenv()
cli = typer.Typer()

# for sub commands
crawler = typer.Typer()
sources = typer.Typer()
hostnames = typer.Typer()

cli.add_typer(crawler, name="crawler")
cli.add_typer(sources, name="sources")
cli.add_typer(hostnames, name="hostnames")


@crawler.command("crawl")
def crawl():
    async def _start_pool():
        try:
            pool = get_worker_pool()
            await pool.start()
        except Exception as e:
            print("error")
            typer.echo(str(e), err=True)
            typer.Exit(1)
        finally:
            print("closing pool")
            await pool.stop()

    asyncio.run(_start_pool())


@crawler.command("stats")
def stats():
    pass


if __name__ == "__main__":
    cli()
