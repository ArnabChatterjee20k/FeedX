import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

from scout.logger import get_logger
from ..database.models import ContentWithId

_logger = get_logger("FEED_GITHUB")

# GitHub REST Contents API. All config comes from the environment so it can be
# set per deploy / GitHub Action without touching code:
#   GITHUB_TOKEN        PAT (or Actions token) with `contents:write` on the repo
#   GITHUB_FEED_REPO    target repo as "owner/name" (a separate content repo)
#   GITHUB_FEED_BRANCH  branch to commit to            (default: main)
#   GITHUB_FEED_DIR     directory to write dated files  (default: feeds)
#   GITHUB_API_URL      API base, for GH Enterprise     (default: api.github.com)
#
# Each build writes one file per day: `<GITHUB_FEED_DIR>/<YYYY-MM-DD>.json`, so a
# static-site generator can glob the directory and render a page per date. A
# re-run on the same day overwrites that day's file (latest build wins).
GITHUB_API_VERSION = "2022-11-28"


class GithubConfigError(RuntimeError):
    pass


def _config() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_FEED_REPO")
    if not token or not repo:
        raise GithubConfigError(
            "GITHUB_TOKEN and GITHUB_FEED_REPO must be set to publish the feed"
        )
    return {
        "token": token,
        "repo": repo,
        "branch": os.environ.get("GITHUB_FEED_BRANCH", "main"),
        "dir": os.environ.get("GITHUB_FEED_DIR", "feeds").strip("/"),
        "api": os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
    }


def _feed_path(cfg: dict, date_str: str) -> str:
    return f"{cfg['dir']}/{date_str}.json" if cfg["dir"] else f"{date_str}.json"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "feedx",
    }


def serialize_feed(contents: list[ContentWithId], generated_at: datetime) -> dict:
    # only the fields the reader needs — keep crawl/simhash internals out of the
    # published artifact. `contents` is already ranked; preserve that order.
    def dt(value):
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "date": generated_at.date().isoformat(),
        "generated_at": generated_at.isoformat(),
        "count": len(contents),
        "items": [
            {
                "id": c.id,
                "url": c.url,
                "title": c.title,
                "summary": c.summary,
                "tags": c.tags,
                "score": c.score,
                "scraped_at": dt(c.scraped_at),
            }
            for c in contents
        ],
    }


def _get_existing_sha(cfg: dict, path: str) -> str | None:
    # the Contents API needs the current blob sha to overwrite a file; absent
    # file -> 404 -> None (a first-time create).
    url = f"{cfg['api']}/repos/{cfg['repo']}/contents/{path}?ref={cfg['branch']}"
    req = urllib.request.Request(url, headers=_headers(cfg["token"]), method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp).get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def publish_feed(contents: list[ContentWithId]) -> tuple[bool, Exception | None]:
    # serialize the ranked feed and create-or-update this day's dated file in the
    # content repo via a single PUT (base64 body + prior sha for the overwrite).
    try:
        cfg = _config()
        now = datetime.now(timezone.utc)
        payload = serialize_feed(contents, generated_at=now)
        path = _feed_path(cfg, payload["date"])
        blob = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

        body = {
            "message": f"feed {payload['date']}: {payload['count']} items",
            "content": base64.b64encode(blob).decode("ascii"),
            "branch": cfg["branch"],
        }
        sha = _get_existing_sha(cfg, path)
        if sha:
            body["sha"] = sha

        url = f"{cfg['api']}/repos/{cfg['repo']}/contents/{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={**_headers(cfg["token"]), "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)

        commit = result.get("commit", {}).get("sha")
        _logger.info(
            f"Published {payload['count']} items to {cfg['repo']}/{path}, commit={commit}",
            tag="PUBLISH",
        )
        return True, None
    except urllib.error.HTTPError as e:
        # surface the GitHub error body so auth / permission issues are obvious
        detail = e.read().decode("utf-8", "replace") if e.fp else ""
        err = RuntimeError(f"GitHub API {e.code}: {detail}")
        _logger.error("Failed to publish feed", tag="PUBLISH", error=err)
        return False, err
    except Exception as e:
        _logger.error("Failed to publish feed", tag="PUBLISH", error=e)
        return False, e
