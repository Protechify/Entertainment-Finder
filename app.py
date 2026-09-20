import concurrent.futures
import difflib
import httpx
import os
import re
import streamlit as st
from simplejustwatchapi import exceptions as jw_exceptions
from simplejustwatchapi import offers_for_countries, search as jw_search

# Personal OMDb API key (server-side only, never sent to the browser).
# Read from the environment so the key never sits in the repo; the hardcoded
# value is only a local-development fallback.
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "a97d6284")
OMDB_BASE = "http://www.omdbapi.com/"

# YouTube Data API v3 (server-side only). Read from the environment so the key
# never sits in the repo; empty string disables the YouTube full-movie section.
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no dependencies)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _secret(name: str) -> str:
    """API key from env var first, then Streamlit secrets (local .env/secrets)."""
    val = os.environ.get(name, "")
    if val:
        return val
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


YOUTUBE_API_KEY = _secret("YOUTUBE_API_KEY")

COUNTRY = "IN"
LANGUAGE = "en"

EMPTY_PROVIDERS = {"flatrate": [], "free": [], "rent": [], "buy": []}

# Providers JustWatch mis-lists for India (US channel bundles). Any name ending
# in " Amazon Channel" is also filtered (see _format_providers), except those
# known to be real Indian add-on channels.
BLOCKED_PROVIDERS = {"Crunchyroll Amazon Channel"}
ALLOWED_AMAZON_CHANNELS = {"Anime Times Amazon Channel"}

# Watch / Download sites. `url_fn` builds a dynamic per-title search URL from the
# selected title. Update base URLs/slug if a mirror moves or is blocked.
def _slugify(title: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in title.lower())
    slug = slug.strip().replace(" ", "-")
    return slug or "search"


# "attack on titan" -> https://m4uhdfree.net/search/attack-on-titan.html
def _m4u_url(title: str) -> str:
    return f"https://m4uhdfree.net/search/{_slugify(title)}.html"


# "attack on titan" -> https://cinehd.vc/search?q=attack%20on%20titan
def _cinehd_url(title: str) -> str:
    keyword = "+".join(title.split())
    return f"https://cinehd.vc/search?q={keyword}"


import html as _html


def _escape(text) -> str:
    """Escape untrusted text before it is embedded in unsafe HTML."""
    return _html.escape(str(text))


def _safe_url(url) -> str:
    """Return the URL only if it is http(s), otherwise empty (blocks unsafe links)."""
    return url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else ""


WATCH_SITES = {
    "m4uhdfree.net": {"url_fn": _m4u_url, "action": "Watch Online"},
    "cinehd.vc": {"url_fn": _cinehd_url, "action": "Watch Online"},
}
DOWNLOAD_SITES: dict = {}


def watch_download_links(selected: dict) -> list:
    """Build per-title dynamic links for a selected title, opened in a new tab."""
    title = (selected.get("title") or "").strip()
    if not title:
        return []
    links = []
    for name, cfg in {**WATCH_SITES, **DOWNLOAD_SITES}.items():
        links.append(
            {
                "site": name,
                "action": cfg["action"],
                "url": cfg["url_fn"](title),
            }
        )
    return links


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_m4u_cards(html: str) -> list:
    """Extract title cards from an m4u search results page.

    Each card's link slug carries the kind (watch-movie-* / watch-tvseries-*),
    the untruncated slugified title, and a .movie-year field.
    """
    cards = []
    for block in html.split('<div class="movie-item">')[1:]:
        href = re.search(r'<a href="([^"]+)"[^>]*>', block)
        title = re.search(r'movie-name[^>]*>\s*<a[^>]*>([^<]+)', block)
        year = re.search(r'movie-year[^>]*>\s*([^<\s]+)', block)
        if not (href and title):
            continue
        slug = href.group(1).split(".")[0].strip("/").lower()
        if slug.startswith("watch-movie"):
            kind = "movie"
        elif slug.startswith("watch-tvseries"):
            kind = "tv"
        else:
            kind = ""
        cards.append(
            {
                "kind": kind,
                "slug": slug,
                "year": re.sub(r"\D", "", year.group(1))[:4] if year else "",
            }
        )
    return cards


def _m4u_card_match(card: dict, title: str, year: str, media_type: str) -> bool:
    """True only if an m4u card is the exact searched title (kind + slug + year)."""
    if not card.get("kind"):
        return False
    wanted_kind = "movie" if media_type == "movie" else "tv"
    if card["kind"] != wanted_kind:
        return False
    kind_slug = "movie" if wanted_kind == "movie" else "tvseries"
    prefix = f"watch-{kind_slug}-{_slugify(title)}"
    if not card["slug"].startswith(prefix):
        return False
    suffix = card["slug"][len(prefix):]
    if suffix and not re.fullmatch(r"-\d{4}(?:-\d+)?", suffix):
        return False
    card_year = int(card["year"]) if card["year"].isdigit() else None
    query_year = int(year) if str(year or "").isdigit() else None
    if query_year and card_year and abs(query_year - card_year) > 1:
        return False
    return True


def _raw_link_has_content(url: str, title: str, year: str = "", media_type: str = "") -> bool:
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.get(url, headers=_BROWSER_HEADERS)
    except httpx.HTTPError:
        return False

    status = resp.status_code
    if status == 404 or status == 410 or status >= 500:
        return False
    if status in (401, 403):
        return True  # exists but blocks bots

    body = resp.content
    if len(body) < 512:
        return False

    text = resp.text.lower()
    # Client-rendered apps (e.g. Next.js) load results via JS, so the shell is
    # expected to be empty on the server - treat as valid, can't verify deeper.
    if "_next/static" in text or "__next_data__" in text:
        return True
    # m4u serves real search-result cards, so verify the exact searched title
    # is among them (type + slug + year). Simply seeing any .movie-item block
    # is not enough: a page full of loosely-related titles is not "the movie".
    if "m4uhdfree.net" in url:
        return any(
            _m4u_card_match(card, title, year, media_type)
            for card in _parse_m4u_cards(resp.text)
        )
    # Other server-rendered search sites: fall back to presence of content
    # blocks. We do NOT test `title in text`: sites echo the search query into
    # the page <title>, so it is present even on "no results" pages.
    markers = ("movie-item",)
    return any(marker in text for marker in markers)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _link_has_content(url: str, title: str, year: str = "", media_type: str = "") -> bool:
    return _raw_link_has_content(url, title, year, media_type)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _poster_ok(url: str) -> bool:
    """True only if the URL actually serves an image (guards broken poster links)."""
    if not _safe_url(url):
        return False
    try:
        with httpx.Client(timeout=8, follow_redirects=True) as client:
            resp = client.get(url, headers=_BROWSER_HEADERS)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    ctype = (resp.headers.get("content-type") or "").lower()
    return ctype.startswith("image/")


def _fallback_poster(title: str, title_year: str = "") -> str:
    """Try to find a working poster for a title via JustWatch."""
    try:
        entries = jw_search(title.strip(), country=COUNTRY, language=LANGUAGE, count=10)
    except (jw_exceptions.JustWatchHttpError, jw_exceptions.JustWatchApiError):
        return ""
    title_l = title.strip().lower()
    year_int = int(title_year) if str(title_year).isdigit() else None
    candidates = [
        entry for entry in entries
        if (entry.title or "").strip().lower() == title_l
    ]
    if not candidates:
        candidates = entries
    for entry in candidates:
        if year_int is not None and entry.release_year and entry.release_year != year_int:
            continue
        poster = entry.poster or ""
        if poster and _poster_ok(poster):
            return poster
    for entry in entries:
        poster = entry.poster or ""
        if poster and _poster_ok(poster):
            return poster
    return ""


def omdb_search(query: str) -> list:
    """Search OMDb for movies and TV series."""
    results = []
    for media_type, label in (("movie", "movie"), ("series", "tv")):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(
                    OMDB_BASE,
                    params={"s": query, "type": media_type, "apikey": OMDB_API_KEY},
                )
                data = resp.json()
        except httpx.HTTPError:
            continue
        if data.get("Response") != "True":
            continue
        for item in data.get("Search", []):
            title = item.get("Title") or ""
            if not title:
                continue
            poster = item.get("Poster") or ""
            if poster in ("", "N/A"):
                poster = None
            if poster and not _poster_ok(poster):
                poster = _fallback_poster(title, (item.get("Year") or "")[:4]) or None
            imdb_id = item.get("imdbID")
            results.append(
                {
                    "id": imdb_id,
                    "media_type": label,
                    "title": title,
                    "year": (item.get("Year") or "")[:4],
                    "poster": poster,
                    "link": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
                }
            )
    return results


def omdb_details(selected: dict) -> dict:
    """Fetch full detail (plot, ratings, cast, etc.) from OMDb for a selected title."""
    params = {
        "apikey": OMDB_API_KEY,
        "plot": "full",
    }
    if selected["media_type"] == "tv":
        params["type"] = "series"
    else:
        params["type"] = "movie"
    imdb_id = selected.get("id") or ""
    if imdb_id.startswith("tt"):
        params["i"] = imdb_id
    else:
        params["t"] = selected["title"].strip()
        year = str(selected.get("year") or "").strip()
        if year.isdigit():
            params["y"] = year
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(OMDB_BASE, params=params)
            data = resp.json()
    except httpx.HTTPError:
        return {}
    if data.get("Response") != "True":
        return {}
    return data


_YOUTUBE_ISO_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _youtube_duration_minutes(iso: str) -> int:
    """YouTube ISO 8601 duration ('PT1H54M39S') -> total minutes."""
    m = _YOUTUBE_ISO_RE.match(iso or "")
    if not m:
        return 0
    d, h, min_, s = (int(x) if x else 0 for x in m.groups())
    return d * 1440 + h * 60 + min_ + (1 if s >= 30 else 0)


_YOUTUBE_BAD_WORDS = (
    "trailer", "teaser", "review", "explained", "recap", "reaction", "music video"
)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def youtube_full_movie(title: str, year: str = "", media_type: str = "") -> list:
    """Find YouTube uploads that are actually full movies / full episodes.

    Searches YouTube "full movie" / "full episodes", then fetches every video's
    duration in one batch call and keeps only entries long enough to be a real
    film or episode (trailers, recaps, and explainers are far shorter and are
    also title-filtered). Results are sorted longest-first.
    """
    if not YOUTUBE_API_KEY:
        return []
    keyword = "full movie" if media_type == "movie" else "full episodes"
    query = f"{title} {year} {keyword}".strip()
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "videoEmbeddable": "true",
        "maxResults": 10,
        "key": YOUTUBE_API_KEY,
    }
    try:
        with httpx.Client(timeout=10) as client:
            data = client.get(YOUTUBE_SEARCH_URL, params=params).json()
            items = data.get("items", [])
            ids = ",".join(
                item["id"]["videoId"]
                for item in items
                if item.get("id", {}).get("videoId")
            )
            durations = {}
            if ids:
                videos = (
                    client.get(
                        YOUTUBE_VIDEOS_URL,
                        params={"part": "contentDetails", "id": ids, "key": YOUTUBE_API_KEY},
                    )
                    .json()
                    .get("items", [])
                )
                durations = {
                    video["id"]: _youtube_duration_minutes(
                        video.get("contentDetails", {}).get("duration", "")
                    )
                    for video in videos
                }
    except httpx.HTTPError:
        return []

    min_length = 90 if media_type == "movie" else 20
    results = []
    for item in items:
        vid = item.get("id", {}).get("videoId", "")
        snippet = item.get("snippet", {})
        if not vid:
            continue
        if durations.get(vid, 0) < min_length:
            continue
        lower_title = (snippet.get("title") or "").lower()
        if any(word in lower_title for word in _YOUTUBE_BAD_WORDS):
            continue
        thumb = snippet.get("thumbnails", {}).get("medium", {}).get("url", "") or ""
        results.append(
            {
                "video_id": vid,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "duration": durations[vid],
                "thumbnail": thumb,
                "link": f"https://www.youtube.com/watch?v={vid}",
                "embed": f"https://www.youtube.com/embed/{vid}",
            }
        )
    results.sort(key=lambda r: r["duration"], reverse=True)
    return results


_RATING_SOURCES = {
    "Internet Movie Database": "IMDb",
    "Rotten Tomatoes": "RT",
    "Metacritic": "Metacritic",
}


def _rating_chip(source: str, value: str) -> str:
    label = _escape(source)
    val = _escape(value)
    return (
        f'<div style="display:inline-flex;align-items:center;gap:6px;'
        f'background:#171c27;border:1px solid #262c39;border-radius:12px;'
        f'padding:8px 14px;font-size:.85rem;white-space:nowrap">'
        f'<span style="color:#8c95a8;font-weight:600">{label}</span>'
        f'<span style="color:#e6e9f0;font-weight:700">{val}</span></div>'
    )


def justwatch_search(query: str, verify_poster: bool = True, count: int = 24) -> list:
    """Fallback search via JustWatch when OMDb has nothing."""
    try:
        entries = jw_search(query.strip(), country=COUNTRY, language=LANGUAGE, count=count)
    except (jw_exceptions.JustWatchHttpError, jw_exceptions.JustWatchApiError):
        return []
    results = []
    for entry in entries:
        if not entry.title:
            continue
        poster = entry.poster or ""
        if poster and verify_poster and not _poster_ok(poster):
            poster = ""
        results.append(
            {
                "id": entry.entry_id,
                "media_type": "movie" if entry.object_type == "MOVIE" else "tv",
                "title": entry.title,
                "year": str(entry.release_year) if entry.release_year else "",
                "poster": poster or None,
                "link": entry.url or "",
            }
        )
    return results


_STOPWORDS = {
    "the", "a", "an", "and", "of", "for", "with", "in", "on", "to", "is",
    "ko", "ke", "ka", "ki", "kar", "karo", "de", "la", "el", "di", "da",
}


def _significant_tokens(text: str) -> list:
    """Meaningful search words from a query (common filler words excluded)."""
    tokens = []
    for word in text.lower().split():
        word = "".join(ch for ch in word if ch.isalnum())
        if len(word) >= 2 and word not in _STOPWORDS and word not in tokens:
            tokens.append(word)
    return tokens


def _title_contains_query(query: str, title: str) -> bool:
    """True if the title contains every significant query word (rejects
    loose JustWatch hits like unrelated titles or mis-listed duplicates)."""
    t = "".join(ch for ch in (title or "").lower() if ch.isalnum())
    if not t:
        return False
    return all(word in t for word in _significant_tokens(query))


def _token_set(text: str) -> set:
    return set(_significant_tokens(text))


def _fuzzy_same_movie(a: dict, b: dict) -> bool:
    """Same listing under a slightly different title/spelling -> true."""
    if a.get("media_type") != b.get("media_type"):
        return False
    a_year = str(a.get("year") or "").strip()
    b_year = str(b.get("year") or "").strip()
    if a_year.isdigit() and b_year.isdigit() and abs(int(a_year) - int(b_year)) > 1:
        return False
    a_tokens = _token_set(a.get("title") or "")
    b_tokens = _token_set(b.get("title") or "")
    if not a_tokens or not b_tokens:
        return False
    overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
    return overlap >= 0.6


def _dedup_key(item: dict) -> tuple:
    title = "".join(ch for ch in (item.get("title") or "").lower() if ch.isalnum())
    return (title, item.get("year") or "", item.get("media_type") or "")


def _normalize_title(text: str) -> str:
    """Lowercase alphanumeric form of a title (punctuation/hyphens stripped)."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _title_score(query: str, title: str) -> int:
    """0 = exact match, 1 = starts with, 2 = contains, 3 = otherwise.

    Both sides are normalized (lowercase, punctuation/hyphens stripped), so
    "spiderman 2" matches "Spider-Man 2" exactly.
    """
    q = _normalize_title(query)
    t = _normalize_title(title)
    if not q or not t:
        return 3
    if t == q:
        return 0
    if t.startswith(q):
        return 1
    if q in t:
        return 2
    return 3


def _relaxed_queries(query: str) -> list:
    """Retry queries for the fuzzy fallback: raw query, significant tokens, and
    prefixes of the longest token (JustWatch tokenizes short prefixes well)."""
    tokens = _significant_tokens(query)
    relaxed = [query] + ([" ".join(tokens)] if tokens else [])
    longest = max(tokens, key=len) if tokens else ""
    if len(longest) >= 4:
        relaxed += [longest[:n] for n in (8, 7, 6, 5, 4) if len(longest) >= n]
    seen, out = set(), []
    for q in relaxed:
        key = _normalize_title(q)
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out


def _fuzzy_score(query: str, title: str) -> float:
    """String similarity (0..1) between a query and a candidate title."""
    q = _normalize_title(query)
    t = _normalize_title(title)
    if not q or not t:
        return 0.0
    return difflib.SequenceMatcher(None, q, t).ratio()


_MIN_SUGGEST_SCORE = 0.82
_MIN_SUGGEST_TITLE_LEN = 6


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fuzzy_suggest(query: str, limit: int = 3) -> list:
    """Suggest likely-correct titles when the exact search misses (e.g. typos).

    Searches JustWatch with relaxed/prefix queries in parallel, scores the
    pooled titles against the original query, and returns the best matches.
    Poster URLs are verified only for the final candidates to stay fast.
    """
    query = query.strip()
    if not query:
        return []
    queries = _relaxed_queries(query)[1:]  # raw query already searched by search()
    pool = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(queries) or 1)) as ex:
        futures = [
            ex.submit(justwatch_search, q, verify_poster=False, count=8)
            for q in queries
        ]
        for fut in futures:
            try:
                items = fut.result(timeout=20)
            except Exception:
                continue
            for item in items:
                key = _dedup_key(item)
                if not any(_dedup_key(k) == key for k in pool):
                    pool.append(item)
    scored = [
        (score, item)
        for item in pool
        if (score := _fuzzy_score(query, item["title"])) >= _MIN_SUGGEST_SCORE
        and len(_normalize_title(item["title"])) >= _MIN_SUGGEST_TITLE_LEN
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [item for _, item in scored[:limit]]
    for item in chosen:
        poster = item.get("poster")
        if poster and not _poster_ok(poster):
            item["poster"] = _fallback_poster(item["title"], item.get("year") or "") or None
    return chosen


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def search(query: str) -> list:
    query = query.strip()
    if not query:
        return []
    results = [
        item for item in (omdb_search(query) + justwatch_search(query))
        if _title_contains_query(query, item["title"])
    ]
    kept = []
    for item in results:
        key = _dedup_key(item)
        if key in {_dedup_key(k) for k in kept}:
            continue
        if any(_fuzzy_same_movie(item, k) for k in kept):
            continue
        kept.append(item)
    kept.sort(key=lambda item: _title_score(query, item["title"]))
    return kept


def _pick_jw_entry(imdb_id, title, year, expected_type, entries):
    """Match a selected title to a JustWatch entry (IMDB id first, then title+year)."""
    for entry in entries:
        if entry.object_type == expected_type and entry.imdb_id == imdb_id:
            return entry

    title_l = title.strip().lower()
    candidates = [
        entry
        for entry in entries
        if entry.object_type == expected_type and (entry.title or "").strip().lower() == title_l
    ]
    if not candidates:
        return None
    if str(year).isdigit():
        year_int = int(year)
        for entry in candidates:
            if entry.release_year == year_int:
                return entry
    return candidates[0]


def _format_providers(offers: list, kind: str = "") -> list:
    seen = set()
    providers = []
    for offer in offers:
        name = offer.package.name
        if name in seen:
            continue
        # Skip US channel bundles JustWatch mis-lists for India.
        if name in BLOCKED_PROVIDERS:
            continue
        if name.endswith(" Amazon Channel") and name not in ALLOWED_AMAZON_CHANNELS:
            continue
        # Amazon Prime Video is a paid service; never show it as free/ads.
        if kind == "free" and name in ("Amazon Prime Video", "Amazon Prime Video with Ads"):
            continue
        seen.add(name)
        providers.append(
            {
                "provider_name": name,
                "logo": offer.package.icon or None,
                "url": offer.url or None,
            }
        )

    base_names = {
        p["provider_name"]
        for p in providers
        if not p["provider_name"].endswith(" with Ads")
    }
    return [
        p for p in providers
        if not p["provider_name"].endswith(" with Ads")
        or p["provider_name"][: -len(" with Ads")] not in base_names
    ]


def get_providers(selected: dict) -> dict:
    """Watch providers scoped to India for the selected title."""
    expected_type = "SHOW" if selected["media_type"] == "tv" else "MOVIE"

    try:
        entries = jw_search(
            selected["title"].strip(), country=COUNTRY, language=LANGUAGE, count=20
        )
    except (jw_exceptions.JustWatchHttpError, jw_exceptions.JustWatchApiError):
        return dict(EMPTY_PROVIDERS)

    entry = _pick_jw_entry(
        selected.get("id"), selected["title"], selected.get("year", ""), expected_type, entries
    )
    if entry is None:
        return dict(EMPTY_PROVIDERS)

    try:
        offers = offers_for_countries(
            entry.entry_id, {COUNTRY}, language=LANGUAGE, best_only=True
        ).get(COUNTRY, [])
    except (jw_exceptions.JustWatchHttpError, jw_exceptions.JustWatchApiError):
        return dict(EMPTY_PROVIDERS)

    groups = {"flatrate": [], "free": [], "rent": [], "buy": []}
    for offer in offers:
        monetization = (offer.monetization_type or "").upper()
        if monetization == "FLATRATE":
            groups["flatrate"].append(offer)
        elif monetization in ("FREE", "ADS"):
            groups["free"].append(offer)
        elif monetization == "RENT":
            groups["rent"].append(offer)
        elif monetization == "BUY":
            groups["buy"].append(offer)

    return {
        "flatrate": _format_providers(groups["flatrate"]),
        "free": _format_providers(groups["free"], kind="free"),
        "rent": _format_providers(groups["rent"]),
        "buy": _format_providers(groups["buy"]),
    }


def platform_links(providers: dict) -> list:
    """Build direct links to the selected title's page on each paid platform
    that actually carries it in India (based on JustWatch offer.url)."""
    actions = {"flatrate": "Watch on", "rent": "Rent on", "buy": "Buy on"}
    raw = []
    for kind in ("flatrate", "rent", "buy"):
        for provider in providers.get(kind) or []:
            url = _safe_url(provider.get("url"))
            if not url:
                continue
            name = (provider.get("provider_name") or "").strip() or "Platform"
            raw.append((kind, name, url))

    base_names = {name for _, name, _ in raw if not name.endswith(" with Ads")}
    seen = set()
    links = []
    for kind, name, url in raw:
        if name.endswith(" with Ads") and name[: -len(" with Ads")] in base_names:
            continue
        if name in seen:
            continue
        seen.add(name)
        links.append({"site": name, "action": f"{actions[kind]} {name}", "url": url})
    return links


def _chip(label: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'background:{color};color:#fff;font-size:.78rem;font-weight:600;'
        f'letter-spacing:.04em;text-transform:uppercase">{label}</span>'
    )


def _navigate_chip(link: dict) -> str:
    label = _escape(link["action"])
    site = _escape(link["site"])
    url = _safe_url(link["url"])
    if not url:
        return ""
    return (
        f'<div style="padding:2px;">'
        f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
        f'style="display:flex;align-items:center;justify-content:center;'
        f'width:100%;box-sizing:border-box;gap:10px;background:#4a6ba9;'
        f'color:#fff;border-radius:14px;padding:16px 18px;text-decoration:none;'
        f'font-size:1.02rem;font-weight:700;'
        f'box-shadow:0 4px 12px rgba(0,0,0,.35);border:1px solid #5d7bb8;">'
        f'<span style="font-size:1.25rem;">&#8599;</span>'
        f'<span>{label}</span></a></div>'
    )


def _poster_placeholder(title: str, width: int = 72) -> str:
    initial = _escape((title or "?")[0].upper() if title else "?")
    height = int(width * 1.5)
    return (
        f'<div style="width:{width}px;height:{height}px;border-radius:6px;'
        f'background:#1a2029;border:1px solid #2a3140;display:flex;align-items:center;'
        f'justify-content:center;color:#5a6472;font-weight:700;font-size:1.3rem;'
        f'font-family:inherit;">{initial}</div>'
    )


def _show_navigate(links: list, chunk: int = 2):
    for i in range(0, len(links), chunk):
        row = links[i : i + chunk]
        cols = st.columns(len(row), gap="small")
        for col, link in zip(cols, row):
            with col:
                st.markdown(_navigate_chip(link), unsafe_allow_html=True)


def _render_result_cards(items: list):
    """Render search result cards with a Select button for each item."""
    for item in items:
        cols = st.columns([1, 3, 1])
        with cols[0]:
            if item.get("poster"):
                st.image(item["poster"], width=72)
            else:
                st.markdown(
                    _poster_placeholder(item["title"]), unsafe_allow_html=True
                )
        with cols[1]:
            badge = _chip(
                "Movie" if item["media_type"] == "movie" else "TV",
                "#c0392b" if item["media_type"] == "movie" else "#16a085",
            )
            st.markdown(
                f"{badge} &nbsp; **{item['title']}**  \n {item.get('year') or '—'}",
                unsafe_allow_html=True,
            )
        if cols[2].button("Select", key=f"select-{item['id']}", use_container_width=True):
            st.session_state["selected"] = item
            st.session_state["providers"] = get_providers(item)
            st.session_state["details"] = omdb_details(item)
            st.session_state["yt_results"] = youtube_full_movie(
                item["title"], item.get("year") or "", item["media_type"]
            )
            st.session_state.pop("yt_play", None)


def main():
    st.set_page_config(page_title="Entertainment Finder", layout="centered")

    st.title("Entertainment Finder")
    st.caption(
        "Search any movie, TV series, or anime and find out where it's streaming in India."
    )

    with st.form("search_form"):
        query = st.text_input(
            "Title",
            placeholder="e.g. Interstellar, One Piece, Attack on Titan",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Search", type="primary", use_container_width=True)

    if submitted:
        results = search(query)
        suggestions = fuzzy_suggest(query) if not results else []
        st.session_state["results"] = results
        st.session_state["suggestions"] = suggestions
        st.session_state.pop("selected", None)
        st.session_state.pop("providers", None)
        st.session_state.pop("details", None)
        st.session_state.pop("yt_results", None)
        st.session_state.pop("yt_play", None)
        if not results and not suggestions:
            st.info("No results found for that title. Try a different spelling.")

    results = st.session_state.get("results", [])
    if results:
        st.subheader("Results")
        _render_result_cards(results)

    suggestions = st.session_state.get("suggestions", [])
    if suggestions:
        st.subheader("Did you mean?")
        st.caption("No exact match found. Did you mean one of these?")
        _render_result_cards(suggestions)

    selected = st.session_state.get("selected")
    if selected:
        st.divider()
        left, right = st.columns([1, 2])
        with left:
            if selected.get("poster"):
                st.image(selected["poster"], width=180)
        with right:
            badge = _chip(
                "Movie" if selected["media_type"] == "movie" else "TV",
                "#c0392b" if selected["media_type"] == "movie" else "#16a085",
            )
            st.markdown(f"{badge} **{selected['title']}**", unsafe_allow_html=True)
            st.markdown(f"{selected.get('year') or '—'}")
            if selected.get("link"):
                st.markdown(f"[View on IMDB]({selected['link']})")

        details = st.session_state.get("details")
        if not details:
            st.info("Couldn't load description & reviews for this title.")
        else:
            st.subheader("Synopsis")
            plot = (details.get("Plot") or "").strip()
            if plot and plot != "N/A":
                st.markdown(plot)
            else:
                st.info("No synopsis available.")

            ratings = details.get("Ratings") or []
            if ratings:
                st.subheader("Reviews & Ratings")
                chips_html = []
                for r in ratings:
                    source = _RATING_SOURCES.get(r.get("Source", ""), r.get("Source", ""))
                    value = r.get("Value", "")
                    if value and value != "N/A":
                        chips_html.append(_rating_chip(source, value))
                if chips_html:
                    st.markdown(
                        '<div style="display:flex;flex-wrap:wrap;gap:8px">'
                        + "".join(chips_html)
                        + "</div>",
                        unsafe_allow_html=True,
                    )

            _detail_fields = [
                ("Director", "Director"),
                ("Cast", "Actors"),
                ("Writer", "Writer"),
                ("Genre", "Genre"),
                ("Runtime", "Runtime"),
                ("Released", "Released"),
                ("Rated", "Rated"),
                ("Awards", "Awards"),
            ]
            has_info = any(
                details.get(jw_key, "") not in ("", "N/A")
                for _, jw_key in _detail_fields
            )
            if has_info:
                with st.expander("More info"):
                    for label, jw_key in _detail_fields:
                        val = details.get(jw_key) or ""
                        if val and val != "N/A":
                            st.markdown(f"**{label}:** {val}")

        providers = st.session_state.get("providers", EMPTY_PROVIDERS)

        st.divider()
        st.subheader("Watch (Paid)")
        st.caption("Jump straight to this title on the platform that carries it.")
        paid_links = platform_links(providers)
        if paid_links:
            _show_navigate(paid_links)
        else:
            st.info("No paid streaming platform offers this title in India right now.")

        yt_results = st.session_state.get("yt_results") or []
        if YOUTUBE_API_KEY:
            st.divider()
            st.subheader("Full Movie on YouTube")
            if yt_results:
                st.caption("Verified uploads only (full-length, title clean). Pick one and play.")
                for r in yt_results:
                    ycols = st.columns([1, 3, 1])
                    with ycols[0]:
                        if r.get("thumbnail"):
                            st.image(r["thumbnail"], width=96)
                    with ycols[1]:
                        st.markdown(
                            f"**{_escape(r['title'])}**  \n"
                            f"{_escape(r['channel'])} · {r['duration']} min"
                        )
                    with ycols[2]:
                        if st.button(
                            "Play", key=f"yt-{r['video_id']}", use_container_width=True
                        ):
                            st.session_state["yt_play"] = r
            else:
                st.info("No verified full movie for this title on YouTube right now.")

        yt_play = st.session_state.get("yt_play")
        if yt_play:
            st.video(_safe_url(yt_play["embed"]))
            st.markdown(f"[Watch on YouTube]({_safe_url(yt_play['link'])})")

        st.divider()
        st.subheader("Watch for Free")
        st.caption("Opens a search for the title on each free site.")
        free_links = watch_download_links(selected)
        if free_links:
            with st.spinner("Checking which links have content…"):
                free_links = [
                    link
                    for link in free_links
                    if _link_has_content(
                        link["url"],
                        selected["title"],
                        selected.get("year") or "",
                        selected.get("media_type") or "",
                    )
                ]
            if free_links:
                _show_navigate(free_links)
            else:
                st.warning(
                    "Couldn't confirm the title on the watch/download sites right now. "
                    "Try again later."
                )
        else:
            st.info("No watch / download site link could be built for this title.")


if __name__ == "__main__":
    main()