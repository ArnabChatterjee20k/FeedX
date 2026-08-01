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

# Generic / boilerplate *content tags* that carry no topical signal. A small
# summariser latches onto page furniture — nav, auth, commerce, legal, social,
# and format words — instead of the actual subject, so "python, http, socket"
# comes back padded with "bookshop, barnes and noble, newsletter, privacy". This
# is the tag-side counterpart to IGNORE_SEGMENTS: matched case-insensitively
# against the whole (already-normalised) tag, exact match only, so a real topic
# that merely contains one of these words is safe. Grow it as junk shows up in
# the feed — that's the intended knob.
IGNORE_TAGS = {
    # site chrome / navigation / meta
    "home",
    "homepage",
    "home page",
    "website",
    "web site",
    "site",
    "web page",
    "webpage",
    "page",
    "pages",
    "landing page",
    "menu",
    "navigation",
    "sidebar",
    "footer",
    "header",
    "breadcrumb",
    "table of contents",
    "contents",
    "index",
    "read more",
    "learn more",
    "click here",
    "see more",
    "view all",
    "show more",
    "search",
    "search results",
    # account / auth / social actions
    "login",
    "log in",
    "sign in",
    "signin",
    "sign up",
    "signup",
    "register",
    "registration",
    "logout",
    "account",
    "my account",
    "profile",
    "dashboard",
    "settings",
    "password",
    "subscribe",
    "subscription",
    "newsletter",
    "mailing list",
    "follow",
    "follow us",
    "followers",
    "following",
    "membership",
    "member",
    "members",
    "share",
    "share this",
    "social media",
    "like",
    "comment",
    "comments",
    "reply",
    # legal / policy / corporate boilerplate
    "about",
    "about us",
    "about me",
    "contact",
    "contact us",
    "get in touch",
    "faq",
    "faqs",
    "help",
    "support",
    "help center",
    "privacy",
    "privacy policy",
    "terms",
    "terms of service",
    "terms of use",
    "terms and conditions",
    "cookie",
    "cookies",
    "cookie policy",
    "gdpr",
    "disclaimer",
    "legal",
    "license",
    "copyright",
    "dmca",
    "careers",
    "career",
    "jobs",
    "hiring",
    "press",
    "press release",
    "media kit",
    "advertise",
    "advertising",
    "sponsor",
    "sponsorship",
    "partners",
    "partnership",
    # commerce / retail / purchase
    "store",
    "shop",
    "shopping",
    "cart",
    "checkout",
    "buy",
    "buy now",
    "order",
    "purchase",
    "pricing",
    "price",
    "deal",
    "deals",
    "discount",
    "coupon",
    "sale",
    "ecommerce",
    "e-commerce",
    "marketplace",
    "bookshop",
    "bookstore",
    "barnes and noble",
    "amazon",
    "amazon.com",
    "ebay",
    "walmart",
    "etsy",
    "google play",
    "app store",
    "play store",
    "oxford university press",
    "university press",
    # generic format / container words (topically empty)
    "blog",
    "blog post",
    "blog posts",
    "article",
    "articles",
    "post",
    "posts",
    "news",
    "story",
    "stories",
    "book",
    "ebook",
    "e-book",
    "pdf",
    "download",
    "downloads",
    "free download",
    "repository",
    "repo",
    "github repository",
    "project",
    "forum",
    "discussion",
    "discussion forum",
    "community",
    "ubiquity",
    # generic promo / listing words
    "latest",
    "trending",
    "popular",
    "featured",
    "recommended",
    "related",
    "related posts",
    "related articles",
    "recent posts",
    "you may also like",
}

# A tag longer than this is almost never a topic — it's a sentence fragment or a
# section heading the model dumped in (e.g. "part 4: modern browsers ...").
MAX_TAG_LEN = 40

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


def is_ignored_tag(tag: str) -> bool:
    # generic/boilerplate tag (nav, auth, commerce, legal, format words) or an
    # over-long sentence fragment — either way, no topical signal.
    t = tag.strip().lower()
    if not t or len(t) > MAX_TAG_LEN:
        return True
    return t in IGNORE_TAGS


def filter_tags(tags: list[str]) -> list[str]:
    # drop junk tags -> dedup, preserving first-seen order (case-insensitive).
    # The tag-domain twin of filter_links; used on free-form summariser output
    # where there is no controlled vocabulary to fall back on.
    seen: set[str] = set()
    kept: list[str] = []
    for tag in tags:
        key = tag.strip().lower()
        if not key or key in seen or is_ignored_tag(tag):
            continue
        seen.add(key)
        kept.append(tag)
    return kept


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
