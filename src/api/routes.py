from fastapi import APIRouter, HTTPException
from ..database import get_database, APPWRITE_DATABASE_ID, get_read_all_permission
from ..database.models import URL, Hostname, CrawlState
from .models import SourceRequest, SourceResponse, SourceListRequest, SourceListReponse
from urllib.parse import urlsplit
import asyncio
from datetime import datetime, timezone
from functools import wraps
from typing import TypeVar, ParamSpec, Callable, cast, Annotated
from collections.abc import Coroutine
from appwrite.query import Query
from appwrite.id import ID
from fastapi import Query as RequestQuery

router = APIRouter()
database = get_database()
DB_ID: str = cast(str, APPWRITE_DATABASE_ID)
T = TypeVar("T")
P = ParamSpec("P")


def to_thread(fn: Callable[P, T]) -> Callable[P, Coroutine[None, None, T]]:
    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return wrapper


@router.post("/sources", response_model=SourceResponse)
async def create_source(body: SourceRequest):
    parsed = urlsplit(body.url)
    hostname_str = parsed.hostname
    if not hostname_str:
        raise HTTPException(
            status_code=400, detail="Invalid URL: could not extract hostname"
        )

    url_data = URL(
        url=body.url, hostname=hostname_str, crawl_state=CrawlState.QUEUED.value
    ).model_dump()
    url_data["crawl_state"] = str(CrawlState.QUEUED.value)
    url_data["next_crawl_at"] = datetime.now(timezone.utc).isoformat()
    hostname_data = Hostname(name=hostname_str).model_dump()

    def _create_hostnames() -> dict:
        try:
            row = database.create_row(
                DB_ID,
                Hostname.__name__,
                data=hostname_data,
                permissions=[get_read_all_permission()],
                row_id=ID.unique(),
            )
            return row.model_dump()
        except Exception as e:
            return {"error": str(e)}

    def _create_urls() -> dict:
        try:
            row = database.create_row(
                DB_ID,
                URL.__name__,
                data=url_data,
                permissions=[get_read_all_permission()],
                row_id=ID.unique(),
            )
            return {"id": row.id, **row.model_dump()}
        except Exception as e:
            return {"error": str(e)}

    create_hostname = to_thread(_create_hostnames)
    create_url = to_thread(_create_urls)

    hostname_result, url_result = await asyncio.gather(create_hostname(), create_url())

    if "error" in hostname_result and "already" not in hostname_result["error"].lower():
        raise HTTPException(status_code=500, detail=hostname_result["error"])
    if "error" in url_result:
        raise HTTPException(status_code=500, detail=url_result["error"])

    return SourceResponse(
        id=url_result.get("id"),
        url=body.url,
        hostname=hostname_str,
        crawl_state=str(CrawlState.QUEUED.value),
        retry_count=0,
        priority_score=0.0,
        next_crawl_at=None,
        last_crawl_at=None,
    )


@router.get("/sources")
async def list_sources(filters: Annotated[SourceListRequest, RequestQuery()]):
    queries = [Query.order_desc("$createdAt"), Query.limit(filters.limit)]
    if filters.before_id:
        queries.append(Query.cursor_before(filters.before_id))

    elif filters.after_id:
        queries.append(Query.cursor_after(filters.after_id))

    for field in ["id", "url", "hostname"]:
        value = getattr(filters, field)
        if value is not None:
            queries.append(Query.equal(field, [value]))

    def _list_urls():
        try:
            rows = database.list_rows(
                DB_ID, URL.__name__, queries=queries, total="false"
            )
            return [{"id": row.id, **row.data} for row in rows.rows]
        except Exception as e:
            return {"error": str(e)}

    urls: list[dict] = await to_thread(_list_urls)()
    if "error" in urls and "already" not in urls["error"].lower():
        raise HTTPException(status_code=500, detail=urls["error"])
    response = []
    for url in urls:
        response.append(SourceResponse(**url))
    return SourceListReponse(data=response)


@router.get("/sources/{id}")
async def get_source(id: str):
    pass


# feed builder endpoints

# content scheduler endpoints
