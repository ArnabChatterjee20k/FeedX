import os
import re
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

from nacl import encoding, public
from dotenv import dotenv_values
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


# ---------------------------------------------------------------------------
# Actions secrets sync
# ---------------------------------------------------------------------------
# Push local .env values to a repo's GitHub Actions secrets/variables over the
# REST API (token only, no `gh`). `.env.example` is the manifest: each synced
# name is tagged `[secret]` or `[var]`; untagged names are skipped.
#
# The sync target is the RUNNER/code repo (where the workflows run), which can be
# a DIFFERENT repo from the feed/content repo, so it has its own env:
#   GITHUB_SYNC_REPO    owner/name of the repo to sync secrets/variables into
#   GITHUB_SYNC_TOKEN   PAT with admin (secrets + variables read/write) on it
#
# Secret VALUES are write-only (GitHub never returns them), so a dry run can only
# compare a secret's presence; variable values are readable and diffed exactly.

# A GitHub Actions secret cannot be named GITHUB_TOKEN, so the local .env's
# GITHUB_TOKEN (the feed-publish PAT) is pushed under this name; feed.yml maps it
# back to GITHUB_TOKEN.
ACTIONS_SECRET_RENAME = {"GITHUB_TOKEN": "FEED_GITHUB_TOKEN"}

_ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def classify_manifest(manifest_path: str) -> tuple[list[str], list[str]]:
    # parse the manifest into (secrets, variables) by the [secret]/[var] tag.
    secrets: list[str] = []
    variables: list[str] = []
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ASSIGN.match(stripped)
            if not match:
                continue
            name = match.group(1)
            comment = stripped.split("#", 1)[1] if "#" in stripped else ""
            if "[secret]" in comment:
                secrets.append(name)
            elif "[var]" in comment:
                variables.append(name)
            # otherwise optional / set in the workflow -> not synced
    return secrets, variables


def _sync_config(repo: str | None, token: str | None) -> dict:
    repo = repo or os.environ.get("GITHUB_SYNC_REPO")
    token = token or os.environ.get("GITHUB_SYNC_TOKEN")
    if not repo or not token:
        raise RuntimeError(
            "GITHUB_SYNC_REPO and GITHUB_SYNC_TOKEN (or --repo/--token) are required"
        )
    return {
        "repo": repo,
        "token": token,
        "api": os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
    }


def _api(method: str, url: str, token: str, body: dict | None = None):
    # returns (status, parsed_json_or_None); raises urllib HTTPError on non-2xx.
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "feedx",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


def _list_secret_names(cfg: dict) -> set[str]:
    url = f"{cfg['api']}/repos/{cfg['repo']}/actions/secrets?per_page=100"
    _, data = _api("GET", url, cfg["token"])
    return {s["name"] for s in (data or {}).get("secrets", [])}


def _list_variables(cfg: dict) -> dict[str, str]:
    url = f"{cfg['api']}/repos/{cfg['repo']}/actions/variables?per_page=100"
    _, data = _api("GET", url, cfg["token"])
    return {v["name"]: v["value"] for v in (data or {}).get("variables", [])}


def _get_public_key(cfg: dict) -> tuple[str, str]:
    url = f"{cfg['api']}/repos/{cfg['repo']}/actions/secrets/public-key"
    _, data = _api("GET", url, cfg["token"])
    return data["key"], data["key_id"]


def _encrypt(public_key_b64: str, value: str) -> str:
    # libsodium sealed box against the repo public key — GitHub's required scheme.
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


def _http_error(e: urllib.error.HTTPError) -> str:
    detail = e.read().decode("utf-8", "replace") if e.fp else ""
    return f"{e.code} {detail[:200]}"


def _put_secret(cfg, name, value, key_b64, key_id) -> tuple[bool, str | None]:
    try:
        url = f"{cfg['api']}/repos/{cfg['repo']}/actions/secrets/{name}"
        _api(
            "PUT",
            url,
            cfg["token"],
            {"encrypted_value": _encrypt(key_b64, value), "key_id": key_id},
        )
        return True, None
    except urllib.error.HTTPError as e:
        return False, _http_error(e)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _set_variable(cfg, name, value, exists) -> tuple[bool, str | None]:
    try:
        if exists:
            url = f"{cfg['api']}/repos/{cfg['repo']}/actions/variables/{name}"
            _api("PATCH", url, cfg["token"], {"name": name, "value": value})
        else:
            url = f"{cfg['api']}/repos/{cfg['repo']}/actions/variables"
            _api("POST", url, cfg["token"], {"name": name, "value": value})
        return True, None
    except urllib.error.HTTPError as e:
        return False, _http_error(e)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def sync_secrets(
    repo: str | None = None,
    token: str | None = None,
    env_file: str = ".env",
    manifest: str = ".env.example",
    dry_run: bool = False,
) -> tuple[str, list[dict]]:
    cfg = _sync_config(repo, token)
    values = dotenv_values(env_file)
    secret_names, variable_names = classify_manifest(manifest)

    existing_secrets = _list_secret_names(cfg)
    existing_vars = _list_variables(cfg)

    # fetch the repo public key lazily — only when we actually write a secret.
    key_cache: dict = {}

    def key():
        if "b64" not in key_cache:
            key_cache["b64"], key_cache["id"] = _get_public_key(cfg)
        return key_cache["b64"], key_cache["id"]

    rows: list[dict] = []

    for source in secret_names:
        target = ACTIONS_SECRET_RENAME.get(source, source)
        present = target in existing_secrets
        value = values.get(source)
        if not value:
            action = "skip (no value)"
        elif dry_run:
            action = "update" if present else "create"
        else:
            key_b64, key_id = key()
            ok, err = _put_secret(cfg, target, value, key_b64, key_id)
            action = ("updated" if present else "created") if ok else f"error: {err}"
        rows.append(
            {"name": target, "kind": "secret", "present": present, "action": action}
        )

    for source in variable_names:
        target = source
        present = target in existing_vars
        value = values.get(source)
        unchanged = present and existing_vars.get(target) == value
        if not value:
            action = "skip (no value)"
        elif unchanged:
            action = "unchanged"
        elif dry_run:
            action = "update" if present else "create"
        else:
            ok, err = _set_variable(cfg, target, value, present)
            action = ("updated" if present else "created") if ok else f"error: {err}"
        rows.append(
            {"name": target, "kind": "variable", "present": present, "action": action}
        )

    return cfg["repo"], rows
