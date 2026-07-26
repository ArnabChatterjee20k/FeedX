import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# URL discovery filter shared by all sources (no per-source logic). The pipeline
# per candidate link is: canonicalize -> dedup -> drop junk. Grow the blocklists
# below as garbage shows up; that's the intended knob.

# Tracking / attribution query params that never change *what* a URL points to.
# Stripped in canonicalize so the same page isn't stored many times — e.g.
# Medium stamps every card link with ?source=collection_home_page----...----5----
# so one profile page showed up as six different rows.
TRACKING_PARAMS = {
    "source",
    "sk",
    "ref",
    "referrer",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "spm",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}

# Path *segments* that are never article content on a typical blog / publication.
# Matched case-insensitively against each "/"-separated segment (exact segment,
# so an article slug like "new-in-python" is safe against the "new" entry).
IGNORE_SEGMENTS = {
    # auth / account
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "login",
    "log-in",
    "logout",
    "register",
    "auth",
    "oauth",
    "account",
    "accounts",
    "password",
    "settings",
    "verify",
    "confirm",
    # social / profile
    "followers",
    "following",
    "user",
    "users",
    "profile",
    "members",
    "membership",
    "subscribe",
    "subscription",
    "notifications",
    "bookmarks",
    "lists",
    "me",
    # nav / meta / legal
    "about",
    "contact",
    "help",
    "faq",
    "support",
    "terms",
    "privacy",
    "policy",
    "policies",
    "legal",
    "cookie",
    "cookies",
    "dmca",
    "guidelines",
    "careers",
    "jobs",
    "press",
    "advertise",
    "advertising",
    "sponsor",
    "partners",
    # commerce
    "store",
    "shop",
    "cart",
    "checkout",
    "pricing",
    "plans",
    "billing",
    "payment",
    "upgrade",
    "premium",
    # search / taxonomy / feeds / pagination
    "search",
    "tag",
    "tags",
    "topic",
    "topics",
    "category",
    "categories",
    "archive",
    "feed",
    "rss",
    "sitemap",
    "amp",
    "page",
    # app / admin / actions
    "admin",
    "dashboard",
    "stats",
    "wp-admin",
    "wp-login",
    "wp-json",
    "new",
    "edit",
    "create",
    "draft",
    "share",
    "print",
}

# Matched anywhere in the path (for things that aren't clean segments).
IGNORE_SUBSTRINGS = {
    "/m/",  # medium infra routes: /m/signin, /m/signout, ...
    "wp-login",
    "robots.txt",
}

# Non-HTML assets we never want to treat as content.
IGNORE_EXTENSIONS = {
    ".xml",
    ".json",
    ".pdf",
    ".rss",
    ".atom",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".gz",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
}

# Bare handle/profile pages like "/@Moesif" (but NOT "/@Moesif/some-post-abc123").
_BARE_PROFILE = re.compile(r"^/@[^/]+$")


def canonicalize(url: str) -> str:
    parts = urlsplit(url)
    # drop fragment, strip tracking params, drop the trailing slash so
    # ".../post" and ".../post/" dedup to one.
    query = [
        (k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def is_ignored(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return True

    path = parts.path.lower()

    if not [s for s in path.split("/") if s]:
        return True  # root / homepage

    if _BARE_PROFILE.match(parts.path.rstrip("/")):
        return True

    if any(sub in path for sub in IGNORE_SUBSTRINGS):
        return True

    if any(path.endswith(ext) for ext in IGNORE_EXTENSIONS):
        return True

    segments = [s for s in path.split("/") if s]
    if any(seg in IGNORE_SEGMENTS for seg in segments):
        return True

    return False


def filter_links(urls: list[str], base_host: str | None = None) -> list[str]:
    # canonicalize -> dedup -> drop junk, preserving first-seen order. Pass
    # base_host to keep only same-host links (don't wander off the source site).
    seen: set[str] = set()
    kept: list[str] = []
    for url in urls:
        canon = canonicalize(url)
        if canon in seen:
            continue
        seen.add(canon)
        if base_host and urlsplit(canon).netloc != base_host:
            continue
        if is_ignored(canon):
            continue
        kept.append(canon)
    return kept
