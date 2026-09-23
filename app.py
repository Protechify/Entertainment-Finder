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


_MEDIA_HINT_TYPES = {
    "movie": ("movie", "film", "padam", "thiraipadam", "cinema", "cinemma"),
    "tv": ("series", "serial", "tv", "show", "thodar", "dhodar"),
}


def _extract_media_hint(query: str) -> tuple:
    """Strip a trailing media-type keyword ('movie'/'series'/...) from a query.

    Returns (clean_query, hint) where hint is 'movie', 'tv', or ''. The
    keyword must be the FINAL word of the query, so normal titles like
    "The Last of Us" are never hijacked by the word "of"/"us".
    """
    words = query.split()
    if not words:
        return query, ""
    last = re.sub(r"[^a-zA-Z]", "", words[-1]).lower()
    hint = next(
        (kind for kind, keys in _MEDIA_HINT_TYPES.items() if last in keys), ""
    )
    if not hint:
        return query, ""
    return " ".join(words[:-1]).strip(), hint


def _extract_season(query: str) -> tuple:
    """Strip a season qualifier ('Season 1', 's1') from a query.

    Returns (clean_query, season) where season is an int or None.
    "Stranger Things Season 1" -> ("Stranger Things", 1). Titles that merely
    contain the word "season" without a number are left untouched.
    """
    m = re.search(r"\bseason\s+(\d{1,2})\b", query, re.IGNORECASE) or re.search(
        r"\bs(\d{1,2})\b", query, re.IGNORECASE
    )
    if not m:
        return query.strip(), None
    return re.sub(r"\bseason\s+\d{1,2}\b|\bs\d{1,2}\b", "", query,
                  flags=re.IGNORECASE).strip(), int(m.group(1))


# Spoken language hints ("in English", "Tamil dub", ...) -> (ISO code, keyword).
_LANGUAGE_HINTS = {
    "english": ("en", "english"),
    "tamil": ("ta", "tamil"),
    "hindi": ("hi", "hindi"),
    "telugu": ("te", "telugu"),
    "malayalam": ("ml", "malayalam"),
    "kannada": ("kn", "kannada"),
    "bengali": ("bn", "bengali"),
    "japanese": ("ja", "japanese"),
    "korean": ("ko", "korean"),
    "chinese": ("zh", "chinese"),
    "spanish": ("es", "spanish"),
    "french": ("fr", "french"),
    "german": ("de", "german"),
    "portuguese": ("pt", "portuguese"),
}

# Official broadcasters / studios that legally upload full movies & episodes.
# Grouped per language for maintainability, but matched against EVERY group so
# official channels are prioritized even when no language hint is given.
# A channel is deemed official when every significant token of a whitelisted
# name appears in its channel title (robust to casing / extra suffixes).
_OFFICIAL_TV_CHANNELS = {
    "ta": {"Kalaingar TV", "Sun TV", "Zee Tamil", "Star Vijay", "Jaya TV",
           "Colors Tamil", "Polimer TV", "Vendhar TV", "Raj TV", "Thanthi TV"},
    "hi": {"SET India", "Star Plus", "Sony SAB", "Colors TV", "Zee TV", "&TV"},
    "te": {"ETV Cinema", "Zee Telugu", "Star Maa", "Gemini TV", "Eenadu TV"},
    "ml": {"Asianet", "Mazhavil Manorama", "Surya TV", "Zee Keralam", "Flowers TV"},
    "kn": {"Zee Kannada", "Colors Kannada", "Udaya TV", "Star Suvarna",
           "Suvarna Plus", "Udaya Movies"},
}

_STUDIO_CHANNELS = {
    "ta": {"Super Good Films", "Pyramid Music", "Vivel Cinema",
           "Ayngaran International"},
    "hi": {"Shemaroo", "Shree Krishna International", "Rajshri"},
    "te": {"Annapurna Studios", "Geetha Arts"},
    "en": {"Paramount Movies", "MGM", "StudioCanal"},
}

# Flattened whitelist names (shared across languages).
_TV_CHANNEL_NAMES = []
for _grp in _OFFICIAL_TV_CHANNELS.values():
    for _name in _grp:
        if _name not in _TV_CHANNEL_NAMES:
            _TV_CHANNEL_NAMES.append(_name)

_STUDIO_CHANNEL_NAMES = []
for _grp in _STUDIO_CHANNELS.values():
    for _name in _grp:
        if _name not in _STUDIO_CHANNEL_NAMES:
            _STUDIO_CHANNEL_NAMES.append(_name)

_CHANNEL_TOKEN_CACHE = {}


def _channel_tokens(name: str) -> frozenset:
    cached = _CHANNEL_TOKEN_CACHE.get(name)
    if cached is None:
        cached = frozenset(_significant_tokens(name))
        _CHANNEL_TOKEN_CACHE[name] = cached
    return cached


def _channel_tier(channel: str) -> str:
    """Classify a YouTube channel title as 'tv', 'studio', or '' (other).

    Matches when every significant token of a whitelisted name appears in the
    channel title, so "Star Vijay" still matches "Star Vijay Tamil".
    """
    if not channel:
        return ""
    tokens = _channel_tokens(channel)
    if not tokens:
        return ""
    for name in _TV_CHANNEL_NAMES:
        if _channel_tokens(name) <= tokens:
            return "tv"
    for name in _STUDIO_CHANNEL_NAMES:
        if _channel_tokens(name) <= tokens:
            return "studio"
    return ""


_LANG_WORDS = sorted(_LANGUAGE_HINTS, key=len, reverse=True)
_LANG_RE = re.compile(
    r"\b(?:in|with|audio|dub|dubbed|output|subtitles|sub)\s+(" + "|".join(_LANG_WORDS) + r")\b"
    r"|\b(" + "|".join(_LANG_WORDS) + r")\s*(?:audio|dub|dubbed|version|subtitles|sub)?\b",
    re.IGNORECASE,
)


def _extract_language_hint(query: str) -> tuple:
    """Strip spoken-language hints ("in English", "Tamil") from a query.

    Returns (clean_query, lang) where lang is the ISO 639-1 code of the first
    language mentioned, or '' when the query does not name a language.
    """
    lang = ""
    out = _LANG_RE.sub("", " " + query + " ")
    cleaned = re.sub(r"\s{2,}", " ", out).strip()
    for word in query.lower().split():
        plain = "".join(ch for ch in word if ch.isalnum())
        if plain in _LANGUAGE_HINTS:
            lang = _LANGUAGE_HINTS[plain][0]
            break
    return cleaned, lang


def _extract_hints(query: str) -> tuple:
    """Strip both trailing media-type and spoken-language hints.

    Returns (clean_query, media_hint, lang). "Zeke's Pad series in English"
    becomes ("Zeke's Pad", "tv", "en"). Language words are stripped first so a
    media-type word directly before them ("...series in English") still hits.
    """
    query, lang = _extract_language_hint(query)
    query, media_hint = _extract_media_hint(query)
    return query, media_hint, lang


_LANG_BY_ISO = {iso: word for word, (iso, _m) in _LANGUAGE_HINTS.items()}

# Words that claim a video's AUDIO track language ("Tamil Full Movie",
# "Hindi Dubbed", "English audio"). "Sub"/"subtitles" are deliberately absent:
# an English-subbed Japanese raw is still Japanese audio.
_AUDIO_MARKERS = (
    "dubbed", "dub", "audio", "version", "language", "full movie",
    "full film", "full episodes", "full hd",
)


def _other_lang_audio(title: str, lang: str) -> bool:
    """True when a title claims its AUDIO is in a language other than `lang`.

    e.g. for lang='te', "Hindhi Dubbed", "Tamil Full Movie", "English Dub"
    all trigger and the upload is treated as wrong-language.
    """
    if not lang or lang not in _LANG_BY_ISO:
        return False
    low = title.lower()
    for word, (iso, _m) in _LANGUAGE_HINTS.items():
        if iso == lang:
            continue
        if re.search(
            rf"\b{re.escape(word)}\s*[- ]?\s*(?:{'|'.join(_AUDIO_MARKERS)})\b", low
        ):
            return True
    return False


def _normalize_audio_lang(value) -> str:
    """Normalize an ISO audio-language value ('en-US' -> 'en', None -> '').

    'zxx'/'mul'/'und' carry no usable language info and map to '' too.
    """
    if not value:
        return ""
    iso = value.split("-")[0].strip().lower()
    if iso in ("zxx", "mul", "und"):
        return ""
    return iso


def _confirm_lang(title: str, audio: str, lang: str) -> bool:
    """True when an upload is confidently in the requested language.

    Either the uploader declared it (audio == lang) or the title itself claims
    it ("Telugu", "Telugu Dubbed", ...). The caller drops audio mismatches
    first, so a declared audio language here always equals `lang`.
    """
    if audio and audio == lang:
        return True
    marker = _LANG_BY_ISO.get(lang, "")
    return bool(marker and re.search(rf"\b{re.escape(marker)}\b", title.lower()))


def omdb_search(query: str, media_hint: str = "") -> list:
    """Search OMDb for movies and TV series (optionally a single type)."""
    results = []
    if media_hint == "movie":
        types = (("movie", "movie"),)
    elif media_hint == "tv":
        types = (("series", "tv"),)
    else:
        types = (("movie", "movie"), ("series", "tv"))
    for media_type, label in types:
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


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _omdb_season_episode_count(details: dict, season: int) -> int:
    """Number of episodes in a series season via OMDb's Season endpoint.

    Returns 0 when it can't be determined (missing imdb id, bad season,
    API error) so the caller skips the episode-count check instead of
    wrongly hiding every result.
    """
    if not season:
        return 0
    imdb_id = (details or {}).get("imdbID") or (details or {}).get("id") or ""
    if not imdb_id.startswith("tt"):
        return 0
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                OMDB_BASE,
                params={"apikey": OMDB_API_KEY, "i": imdb_id, "Season": season},
            )
            data = resp.json()
    except httpx.HTTPError:
        return 0
    if data.get("Response") != "True":
        return 0
    episodes = data.get("Episodes") or []
    count = len(episodes)
    return count if count > 0 else 0


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

# Reaction ("person watches the movie") uploads. They often retitle the video
# to look like the real film, so we scan the channel name + title + description
# (all already returned by part=snippet — no extra API quota).
_REACTION_RE = re.compile(
    r"\breact(?:ion|ing|ed|s)?\b"
    r"|first time (?:watching|seeing|hearing)"
    r"|watch(?:ing)? (?:along|for the first time)"
    r"|our reaction|live reaction|commentary",
    re.IGNORECASE,
)


def _looks_like_reaction(channel: str, title: str, description: str = "") -> bool:
    """True when a channel/title/description marks an upload as a reaction video.

    Reaction channels self-identify ("Reacting To...", "XYZ Reacts"), and their
    descriptions explain that the actual movie/episode is being watched/comment.
    """
    blob = " | ".join(
        p for p in (channel or "", title or "", description or "") if p
    )
    return bool(_REACTION_RE.search(blob))


def _omdb_runtime_minutes(details: dict) -> int:
    """OMDb movie 'Runtime' ("110 min") -> total minutes; 0 if missing/invalid."""
    runtime = str((details or {}).get("Runtime") or "")
    m = re.search(r"(\d+)", runtime)
    return int(m.group(1)) if m else 0


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def youtube_full_movie(
    title: str, year: str = "", media_type: str = "", lang: str = "", expected_minutes: int = 0
) -> list:
    """Find YouTube uploads that are actually full movies / full episodes.

    Searches YouTube "full movie" / "full episodes", then fetches every video's
    duration in one batch call and keeps only entries long enough to be a real
    film or episode (trailers, recaps, and explainers are far shorter and are
    also title-filtered). Results are ranked by title+year match (top 3). A
    language hint (e.g. 'ta') biases the search and boosts matching titles.
    When `expected_minutes` is given (movie runtime from OMDb), uploads whose
    duration deviates >15% are flagged and ranked below matching-length ones.
    """
    if not YOUTUBE_API_KEY:
        return []
    marker = _LANGUAGE_HINTS.get(lang, (None, ""))[1]
    keyword = "full movie" if media_type == "movie" else "full episodes"
    min_length = 90 if media_type == "movie" else 20

    def _collect(query: str) -> list:
        if not query.strip():
            return []
        params = {
            "part": "snippet",
            "q": query.strip(),
            "type": "video",
            "videoEmbeddable": "true",
            "maxResults": 50,
            "key": YOUTUBE_API_KEY,
        }
        if marker:
            params["relevanceLanguage"] = lang
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
                audio_langs = {}
                statuses = {}
                descriptions = {}
                if ids:
                    videos = (
                        client.get(
                            YOUTUBE_VIDEOS_URL,
                            params={
                                "part": "snippet,contentDetails,status",
                                "id": ids,
                                "key": YOUTUBE_API_KEY,
                            },
                        )
                        .json()
                        .get("items", [])
                    )
                    for video in videos:
                        durations[video["id"]] = _youtube_duration_minutes(
                            video.get("contentDetails", {}).get("duration", "")
                        )
                        audio_langs[video["id"]] = _normalize_audio_lang(
                            (video.get("snippet", {}) or {}).get("defaultAudioLanguage")
                        )
                        descriptions[video["id"]] = (
                            (video.get("snippet", {}) or {}).get("description", "") or ""
                        )
                        stt = video.get("status", {}) or {}
                        statuses[video["id"]] = (
                            stt.get("privacyStatus", ""),
                            bool(stt.get("embeddable")),
                            stt.get("uploadStatus", ""),
                        )
        except httpx.HTTPError:
            return []

        picked = []
        for item in items:
            vid = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            if not vid:
                continue
            # videos.list only returns existing videos, so a missing status
            # means the link is dead (deleted/privated video) -> drop it.
            status = statuses.get(vid)
            if not status:
                continue
            privacy, embeddable, upload = status
            if privacy != "public" or not embeddable:
                continue
            if upload and upload != "processed":
                continue
            if durations.get(vid, 0) < min_length:
                continue
            video_title = _html.unescape(snippet.get("title") or "")
            channel = _html.unescape(snippet.get("channelTitle") or "")
            if any(word in video_title.lower() for word in _YOUTUBE_BAD_WORDS):
                continue
            # Reaction uploads retitle videos to look like the real film, so the
            # title filter isn't enough — scan channel + title + description too.
            if _looks_like_reaction(channel, video_title, descriptions.get(vid, "")):
                continue
            audio = audio_langs.get(vid, "")
            if lang:
                if audio and audio != lang:
                    continue
                if _other_lang_audio(video_title, lang):
                    continue
                if not _confirm_lang(video_title, audio, lang):
                    continue
            thumb = snippet.get("thumbnails", {}).get("medium", {}).get("url", "") or ""
            duration = durations[vid]
            mismatch = (
                expected_minutes > 0
                and abs(duration - expected_minutes) > 0.15 * expected_minutes
            )
            picked.append(
                {
                    "video_id": vid,
                    "title": video_title,
                    "channel": channel,
                    "official_tier": _channel_tier(channel),
                    "duration": duration,
                    "audio_lang": audio,
                    "thumbnail": thumb,
                    "duration_mismatch": mismatch,
                    "link": f"https://www.youtube.com/watch?v={vid}",
                }
            )
        return picked

    results = _collect(f"{title} {year} {keyword}")
    if lang and not results:
        results = _collect(f"{title} {year} {marker} {keyword}")

    # Drop unrelated uploads. Only results whose title genuinely IS the queried
# movie (stand-alone name after removing upload markers like "full movie /
# tamil / hd / cast buckets") pass — a "Aanandham Aarambam" upload must never
# appear for an "Aanandham" query. Official TV channels keep a trust exemption.
    results = [
        r for r in results
        if r["official_tier"] == "tv" or _yt_title_exact_match(r["title"], title)
    ]

    # Drop movies whose duration doesn't match the OMDb runtime (>15% off).
    # Only set when an OMDb runtime exists, so this never affects TV series
    # or movies with no reference runtime.
    results = [r for r in results if not r.get("duration_mismatch")]

    _TIER_BONUS = {"tv": 300, "studio": 150}
    results.sort(
        key=lambda r: (
            _yt_relevance_score(r["title"], title, year)
            + _TIER_BONUS.get(r["official_tier"], 0)
            + (60 if marker and marker in r["title"].lower() else 0),
            r["duration"],
        ),
        reverse=True,
    )
    return results[:2]


def _yt_relevance_score(result_title: str, title: str, year: str) -> int:
    """Rank a YouTube result against the searched title+certain year.

    Exact-title match is best, then substring/token hits, then the selected
    release year appearing in the title (e.g. "(2015)") which keeps wrong-year
    uploads of similarly-named movies below the right one.
    """
    rt = _normalize_title(result_title)
    qt = _normalize_title(title)
    score = 0
    if qt and rt == qt:
        return 1000
    if qt and qt in rt:
        score += 200
    score += 5 * sum(1 for t in _significant_tokens(title) if t in rt)
    if year and str(year) in rt:
        score += 10
    return score


# Generic upload markers stripped from a title chunk before comparing it to
# the queried movie name. Kept as words so compound chunks ("aanandham aarambam")
# still fail the exact-name check while "aanandham full movie" still passes.
_YT_TITLE_MARKERS = (
    "full", "movie", "film", "hd", "4k", "2k", "dubbed", "dub", "audio",
    "version", "english", "tamil", "hindi", "telugu", "malayalam", "kannada",
    "blockbuster", "super", "hit", "superhit", "remastered", "rip", "ripped",
    "dvd", "bluray", "web", "official", "online", "watch", "best", "quality",
    "tamilrockers", "tamilyogi", "copyright", "song", "video",
)


def _yt_title_exact_match(result_title: str, title: str) -> bool:
    """True only when a result's title names the queried movie exactly.

    The title is split into chunks by typical upload separators, and each chunk
    is checked after stripping generic markers ("full movie", "tamil", "hd",
    year digits, ...) — so "Aanandham | Tamil Full Movie |..." passes but
    "Aanandham Aarambam Tamil Full Movie" (a different film glued on) fails.
    """
    q = _normalize_title(title)
    if not q:
        return False
    for chunk in re.split(r"[|,()\[\]:;_~*\-]+", result_title):
        words = re.findall(r"[a-z0-9]+", chunk.lower())
        core = "".join(
            w for w in words
            if w not in _YT_TITLE_MARKERS and not re.fullmatch(r"19\d\d|20\d\d", w)
        )
        if core == q:
            return True
    return False


_YOUTUBE_PLAYLIST_BAD_WORDS = (
    "trailer", "teaser", "best moments", "movie", "review", "explained",
    "recap", "reaction", "music", "ending", "opening",
    # Playlists that LOOK like full episodes but are actually compilations:
    "interview", "panel", "featurette", "behind", "scenes", "bts",
    "compilation", "moments", "funniest", "soundtrack", "ost", "countdown",
    "ranking", "quiz", "theory", "news", "blooper", "documentary", "making",
    # Netflix "Inside the Episodes" / companion talk-show style lists:
    "inside the episodes", "still watching", "watch along", "after show",
    "aftershow", "discussion", "official podcast", "companion",
)

# Playlist items that identify a playlist as NOT full episodes (trailers,
# teasers, premieres, recaps, interviews, podcasts, VFX breakdowns...). A
# playlist only counts when REAL numbered episodes are a strict majority of the
# sampled items; one "Best Quality" episode upload never trips it.
_YT_PLAYLIST_JUNK_RE = re.compile(
    r"\b(?:trailer|teaser|tease|premiere|announcement|production|recap|review|"
    r"reaction|interview|panel|featurette|behind|scenes|bts|compilation|best|"
    r"moments|funniest|ending|opening|soundtrack|ost|song|music|score|cast|quiz|"
    r"explained|theory|ranking|countdown|promo|clip|blooper|breakdown|deleted|"
    r"spoiler|news|update|documentary|making|carpet|world|tour|vfx|podcast|"
    r"scene|moment|celebration|comic|con|insider|live|feature|features|special|"
    r"event|intros|outros|teases)\b",
    re.IGNORECASE,
)

_YT_ITEM_EPISODE_RE = re.compile(
    r"\b(?:episode|ep)\b|\be\d{1,3}\b|\bs\d{1,2}e\d{1,3}\b|"
    r"\bpart \d\b|\bvol(?:ume)? \d\b|\bseason \d\b",
    re.IGNORECASE,
)

# Companion/behind-the-scenes content that numbers its entries like episodes
# ("Inside the Episodes | Episode 1", "Official Podcast: E5", "Watch Along #3")
# but is NOT the actual series. Checked BEFORE the episode marker so these
# numbered companion items count as junk, never as real episodes.
_YT_PLAYLIST_JUNK_PHRASES = re.compile(
    r"inside\s+the\s+episodes|watch\s+along|after\s+show|aftershow|"
    r"discussion|official\s+podcast|companion|talk\s+show|making\s+of|"
    r"behind\s+the\s+scenes|recap\s+series|still\s+watching|"
    r"re-?watch|reaction\s+podcast|deep\s+dive|explained\s+series|"
    r"round\s*table|cast\s+commentary|audio\s+commentary|director'?s\s+commentary",
    re.IGNORECASE,
)


def _playlist_is_full_episodes(playlist_id: str) -> bool:
    """Confirm a playlist actually contains the show's episodes — or reject it.

    Samples the first ~25 videos via playlistItems (one quota cost). The
    playlist passes ONLY when numbered episode uploads are a strict majority of
    the sample. "All things Season 3" style promotion playlists (trailers,
    premieres, panels, bloopers, podcasts) contain zero or few real episodes and
    are rejected — the user asked for the exact full episodes or nothing.
    """
    try:
        with httpx.Client(timeout=10) as client:
            data = (
                client.get(
                    "https://www.googleapis.com/youtube/v3/playlistItems",
                    params={
                        "part": "snippet",
                        "playlistId": playlist_id,
                        "maxResults": 25,
                        "key": YOUTUBE_API_KEY,
                    },
                )
                .json()
                .get("items", [])
            )
    except httpx.HTTPError:
        return True
    titles = [_html.unescape(it.get("snippet", {}).get("title") or "") for it in data]
    if not titles:
        return True
    episode = junk = 0
    for item in titles:
        il = item.lower()
        if _YT_PLAYLIST_JUNK_PHRASES.search(il):
            junk += 1
        elif _YT_ITEM_EPISODE_RE.search(il):
            episode += 1
        elif _YT_PLAYLIST_JUNK_RE.search(il):
            junk += 1
    if episode == 0:
        return False
    if episode * 2 <= len(titles):
        return False
    if junk and junk >= episode:
        return False
    return True


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def youtube_series_playlists(
    title: str, year: str = "", lang: str = "", expected_episodes: int = 0
) -> list:
    """Find full-series playlists on YouTube for a TV series.

    Searches YouTube for playlists of the series' full episodes, fetches each
    playlist's video count in one batch call, and keeps only entries big enough
    to be a real season/box-set. Results are ranked by title+year match and
    playlist size (top 3). If the year-scoped search finds nothing, a broader
    search without the year is retried (playlist titles rarely carry the year).
    A language hint additionally shows only playlists CONFIRMED to be in that
    language (title claim or video audio check), trying a language-word query
    when the plain search finds none.
    When `expected_episodes` > 0 (e.g. OMDb season episode count), playlists
    whose item count does not EXACTLY match it are hidden.
    """
    if not YOUTUBE_API_KEY:
        return []
    marker = _LANGUAGE_HINTS.get(lang, (None, ""))[1]
    need = _significant_tokens(title)

    def _try_query(query: str) -> list:
        params = {
            "part": "snippet",
            "q": query,
            "type": "playlist",
            "maxResults": 10,
            "key": YOUTUBE_API_KEY,
        }
        if marker:
            params["relevanceLanguage"] = lang
        try:
            with httpx.Client(timeout=10) as client:
                data = client.get(YOUTUBE_SEARCH_URL, params=params).json()
                items = data.get("items", [])
                ids = ",".join(
                    item["id"]["playlistId"]
                    for item in items
                    if item.get("id", {}).get("playlistId")
                )
                counts = {}
                if ids:
                    playlists = (
                        client.get(
                            "https://www.googleapis.com/youtube/v3/playlists",
                            params={
                                "part": "contentDetails",
                                "id": ids,
                                "key": YOUTUBE_API_KEY,
                            },
                        )
                        .json()
                        .get("items", [])
                    )
                    counts = {
                        pl["id"]: int(pl.get("contentDetails", {}).get("itemCount", 0))
                        for pl in playlists
                    }
        except httpx.HTTPError:
            return []

        results = []
        for item in items:
            pid = item.get("id", {}).get("playlistId", "")
            snippet = item.get("snippet", {})
            if not pid:
                continue
            playlist_title = _html.unescape(snippet.get("title") or "")
            if counts.get(pid, 0) < 5:
                continue
            # Strict episode-count verification: a playlist whose item count
            # differs from the expected season episode count is a wrong/mixed
            # upload — never show it.
            if expected_episodes > 0 and counts.get(pid, 0) != expected_episodes:
                continue
            lower_title = playlist_title.lower()
            if any(word in lower_title for word in _YOUTUBE_PLAYLIST_BAD_WORDS):
                continue
            rt = _normalize_title(playlist_title)
            if not rt or not all(token in rt for token in need):
                continue
            if lang and _other_lang_audio(playlist_title, lang):
                continue
            thumb = snippet.get("thumbnails", {}).get("medium", {}).get("url", "") or ""
            item_count = counts[pid]
            channel = _html.unescape(snippet.get("channelTitle") or "")
            # Reaction channels build "watch the whole series" playlists too —
            # drop them using channel + title + description signals.
            if _looks_like_reaction(channel, playlist_title, snippet.get("description") or ""):
                continue
            tier = _channel_tier(channel)
            score = (
                _yt_relevance_score(playlist_title, title, year)
                + min(item_count, 60)
                + (60 if marker and marker in lower_title else 0)
                + {"tv": 300, "studio": 150}.get(tier, 0)
            )
            results.append(
                {
                    "playlist_id": pid,
                    "title": playlist_title,
                    "channel": channel,
                    "official_tier": tier,
                    "item_count": item_count,
                    "thumbnail": thumb,
                    "link": f"https://www.youtube.com/playlist?list={pid}",
                    "_score": score,
                }
            )
        results.sort(key=lambda r: r["_score"], reverse=True)
        kept = []
        for r in results:
            if not _playlist_is_full_episodes(r["playlist_id"]):
                continue
            kept.append(r)
            if len(kept) >= 3:
                break
        for r in kept:
            r.pop("_score", None)
        return kept

    results = _try_query(f"{title} {year} full episodes".strip())
    if not results and year:
        results = _try_query(f"{title} full episodes".strip())

    if lang:
        def _pick(rs: list) -> list:
            if not rs:
                return []
            votes = _verify_playlist_languages([r["playlist_id"] for r in rs])
            kept = []
            for r in rs:
                v = votes.get(r["playlist_id"], set())
                if r["title"].lower() and _other_lang_audio(r["title"], lang):
                    continue
                if lang in v or (marker and re.search(rf"\b{re.escape(marker)}\b", r["title"].lower())):
                    r["audio_lang"] = _LANG_BY_ISO.get(lang, "") if lang in v else ""
                    r["_score"] = (
                        _yt_relevance_score(r["title"], title, year)
                        + min(r["item_count"], 60)
                        + (60 if marker and marker in r["title"].lower() else 0)
                        + {"tv": 300, "studio": 150}.get(r.get("official_tier", ""), 0)
                        + (30 if lang in v else 0)
                    )
                    kept.append(r)
            return kept

        kept = _pick(results)
        if not kept:
            extra = _try_query(f"{title} {year} {marker} full episodes".strip())
            if not extra and year:
                extra = _try_query(f"{title} {marker} full episodes".strip())
            kept = _pick(extra)
        kept.sort(key=lambda r: r["_score"], reverse=True)
        for r in kept:
            r.pop("_score", None)
        return kept[:3]

    return results[:3]


_YT_EPISODE_RE = re.compile(
    r"\b(?:episode|ep|e)\s*\.?\s*(\d{1,3})\b|\bs(\d{1,2})\s*e(\d{1,3})\b",
    re.IGNORECASE,
)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def youtube_series_episodes(
    title: str, year: str = "", lang: str = "", season: int = 0
) -> list:
    """Find individual full-episode uploads for a TV series / anime.

    Searches for the series' episodes as separate videos (common for anime),
    keeps only working, full-length uploads that name an episode number, and
    hides reaction/junk channels. When a season is provided, uploads that name
    a DIFFERENT season are dropped; when no season marker is present the video
    is kept (ambigous first-episode listings). Duplicate episode numbers are
    deduped (best one wins) and results are capped at 6, Episode 1 first.
    """
    if not YOUTUBE_API_KEY:
        return []
    marker = _LANGUAGE_HINTS.get(lang, (None, ""))[1]
    need = _significant_tokens(title)

    def _try_query(query: str) -> list:
        if not query.strip():
            return []
        params = {
            "part": "snippet",
            "q": query.strip(),
            "type": "video",
            "videoEmbeddable": "true",
            "maxResults": 50,
            "key": YOUTUBE_API_KEY,
        }
        if marker:
            params["relevanceLanguage"] = lang
        try:
            with httpx.Client(timeout=10) as client:
                data = client.get(YOUTUBE_SEARCH_URL, params=params).json()
                items = data.get("items", [])
                ids = ",".join(
                    item["id"]["videoId"]
                    for item in items
                    if item.get("id", {}).get("videoId")
                )
                durations, audio_langs, statuses, descriptions = {}, {}, {}, {}
                if ids:
                    videos = (
                        client.get(
                            YOUTUBE_VIDEOS_URL,
                            params={
                                "part": "snippet,contentDetails,status",
                                "id": ids,
                                "key": YOUTUBE_API_KEY,
                            },
                        )
                        .json()
                        .get("items", [])
                    )
                    for video in videos:
                        durations[video["id"]] = _youtube_duration_minutes(
                            video.get("contentDetails", {}).get("duration", "")
                        )
                        audio_langs[video["id"]] = _normalize_audio_lang(
                            video.get("snippet", {}).get("defaultAudioLanguage")
                        )
                        stt = video.get("status", {}) or {}
                        statuses[video["id"]] = (
                            stt.get("privacyStatus", ""),
                            bool(stt.get("embeddable")),
                            stt.get("uploadStatus", ""),
                        )
                        desc = _html.unescape(video.get("snippet", {}).get("description") or "")
                        descriptions[video["id"]] = desc[:400]
        except httpx.HTTPError:
            return []

        results = []
        for item in items:
            vid = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            if not vid:
                continue
            privacy, embeddable, upload_status = statuses.get(vid, ("", False, ""))
            if privacy != "public" or not embeddable or (upload_status and upload_status != "processed"):
                continue
            if durations.get(vid, 0) < 15:
                continue
            video_title = _html.unescape(snippet.get("title") or "")
            channel = _html.unescape(snippet.get("channelTitle") or "")
            lower_title = video_title.lower()
            if any(word in lower_title for word in _YOUTUBE_BAD_WORDS):
                continue
            if _looks_like_reaction(channel, video_title, descriptions.get(vid, "")):
                continue
            if (
                _YT_PLAYLIST_JUNK_PHRASES.search(video_title)
                or _YT_PLAYLIST_JUNK_PHRASES.search(channel)
            ):
                continue
            rt = _normalize_title(video_title)
            if not rt or not all(token in rt for token in need):
                continue
            if season:
                sm = re.search(r"\bs(\d{1,2})\b", lower_title)
                if sm and int(sm.group(1)) != season:
                    continue
            m = _YT_EPISODE_RE.search(lower_title)
            if not m:
                continue
            episode_num = int(m.group(1) or m.group(3))
            if season and m.group(2) and int(m.group(2)) != season:
                continue
            audio = audio_langs.get(vid, "")
            if lang:
                if audio and audio != lang:
                    continue
                if _other_lang_audio(video_title, lang):
                    continue
                if not _confirm_lang(video_title, audio, lang):
                    continue
            thumb = snippet.get("thumbnails", {}).get("medium", {}).get("url", "") or ""
            tier = _channel_tier(channel)
            score = (
                _yt_relevance_score(video_title, title, year)
                + {"tv": 300, "studio": 150}.get(tier, 0)
                + (30 if marker and marker in lower_title else 0)
            )
            results.append(
                {
                    "video_id": vid,
                    "title": video_title,
                    "channel": channel,
                    "official_tier": tier,
                    "duration": durations[vid],
                    "audio_lang": audio,
                    "thumbnail": thumb,
                    "episode": episode_num,
                    "link": f"https://www.youtube.com/watch?v={vid}",
                    "_score": score,
                }
            )
        out = sorted(results, key=lambda r: (-r["_score"], r["episode"]))
        return out

    all_results = []
    season_q = f" s{season}" if season else ""
    all_results = (
        _try_query(f"{title}{season_q} full episode".strip())
        or _try_query(f"{title} episode 1".strip())
        or _try_query(f"{title}{season_q} episode".strip())
    )
    if not all_results and lang and marker:
        all_results = _try_query(f"{title}{season_q} {marker} episode".strip())
    if not all_results:
        return []

    seen = set()
    kept = []
    for r in all_results:
        episode_key = (season, r["episode"]) if season else r["episode"]
        if episode_key in seen:
            continue
        seen.add(episode_key)
        kept.append(r)
    kept.sort(key=lambda r: (-r["_score"], r["episode"]))
    for r in kept:
        r.pop("_score", None)
    return kept[:6]


def _verify_playlist_languages(playlist_ids: list) -> dict:
    """Map playlistId -> set of audio languages among its first 5 videos.

    Reads each playlist's first few videos via the playlistItems endpoint, then
    a single batched videos call surfaces their uploader-declared
    defaultAudioLanguage. Playlists whose videos declare only OTHER languages
    are treated as wrong-language by the caller.
    """
    votes = {pid: set() for pid in playlist_ids}
    if not playlist_ids:
        return votes
    try:
        with httpx.Client(timeout=10) as client:
            first_ids = []
            for pid in playlist_ids:
                items = (
                    client.get(
                        "https://www.googleapis.com/youtube/v3/playlistItems",
                        params={
                            "part": "snippet",
                            "playlistId": pid,
                            "maxResults": 5,
                            "key": YOUTUBE_API_KEY,
                        },
                    )
                    .json()
                    .get("items", [])
                )
                first_ids += [
                    (pid, it["snippet"]["resourceId"]["videoId"])
                    for it in items
                    if it.get("snippet", {}).get("resourceId", {}).get("videoId")
                ]
            if not first_ids:
                return votes
            ids = ",".join(vid for _, vid in first_ids)
            videos = (
                client.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"part": "snippet", "id": ids, "key": YOUTUBE_API_KEY},
                )
                .json()
                .get("items", [])
            )
            lookup = {
                v["id"]: _normalize_audio_lang(
                    (v.get("snippet", {}) or {}).get("defaultAudioLanguage")
                )
                for v in videos
            }
            for pid, vid in first_ids:
                audio = lookup.get(vid, "")
                if audio:
                    votes[pid].add(audio)
    except httpx.HTTPError:
        pass
    return votes


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


def _spelling_variants(query: str, cap: int = 5) -> list:
    """Spelling variants fixing doubled-vowel typos (Tamil/Indian names).

    "vanathaipola" -> ["vaanathaipola", "vanathaipolaa", ...]: for each vowel,
    a double-vowel and a halve-double-vowel twist is produced (deduped, capped)
    so OMDb/JustWatch fuzzy search can still find the intended title.
    """
    out: list = []
    for i, ch in enumerate(query):
        low = ch.lower()
        if low in "aeiou":
            out.append(query[:i] + ch + query[i:])
        if low in "aaeeiioouu":
            out.append(query[:i] + query[i + 1:])
        if len(out) >= cap:
            break
    return list(dict.fromkeys(out))


def _suggest_source(query: str, media_hint: str = "") -> list:
    """Pooled OMDb + JustWatch candidates for a suggestion search query."""
    return omdb_search(query, media_hint) + justwatch_search(
        query, verify_poster=False, count=8
    )


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fuzzy_suggest(query: str, limit: int = 3) -> list:
    """Suggest likely-correct titles when the exact search misses (e.g. typos).

    Searches OMDb and JustWatch with relaxed/prefix queries plus spelling
    variants in parallel, scores every pooled title against the original query,
    and returns the best matches. Poster URLs are verified only for the final
    candidates to stay fast.
    """
    query, media_hint, _ = _extract_hints(query)
    query = query.strip()
    if not query:
        return []
    queries = list(dict.fromkeys(
        [query]
        + (_spelling_variants(query) if len(query) >= 6 else [])
        + _relaxed_queries(query)[1:]
    ))[1:]  # raw query already searched by search()
    pool = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(queries) or 1)) as ex:
        futures = [
            ex.submit(_suggest_source, q, media_hint)
            for q in queries
        ]
        for fut in futures:
            try:
                items = fut.result(timeout=25)
            except Exception:
                continue
            for item in items:
                key = _dedup_key(item)
                if any(_dedup_key(k) == key for k in pool):
                    continue
                if media_hint and item["media_type"] != media_hint:
                    continue
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
    query, media_hint, _ = _extract_hints(query)
    query = query.strip()
    if not query:
        return []
    results = [
        item
        for item in (omdb_search(query, media_hint) + justwatch_search(query))
        if (not media_hint or item["media_type"] == media_hint)
        and _title_contains_query(query, item["title"])
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


def _poster_placeholder(title: str, width: int = 120) -> str:
    initial = _escape((title or "?")[0].upper() if title else "?")
    height = int(width * 1.5)
    return (
        f'<div style="width:{width}px;height:{height}px;border-radius:6px;'
        f'background:#1a2029;border:1px solid #2a3140;display:flex;align-items:center;'
        f'justify-content:center;color:#5a6472;font-weight:700;font-size:1.3rem;'
        f'font-family:inherit;">{initial}</div>'
    )


@st.dialog("Larger view", width="large")
def _show_large_media(url: str) -> None:
    """Modal lightbox showing an image (poster/thumbnail) full-size."""
    st.image(url, use_container_width=True)


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
                st.image(item["poster"], width=120)
                if st.button(
                    "⤢",
                    key=f"zoom-{item['id']}",
                    help="Show larger",
                    use_container_width=True,
                ):
                    _show_large_media(item["poster"])
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
            with st.spinner("Loading details, providers & YouTube matches..."):
                season = st.session_state.get("season")
                st.session_state["selected"] = {**item, "season": season}
                st.session_state["providers"] = get_providers(item)
                details = omdb_details(item)
                st.session_state["details"] = details
                lang = st.session_state.get("yt_lang") or ""
                if item["media_type"] == "movie":
                    st.session_state["yt_results"] = youtube_full_movie(
                        item["title"], item.get("year") or "", item["media_type"], lang,
                        _omdb_runtime_minutes(details) or 0,
                    )
                    st.session_state.pop("yt_episodes", None)
                else:
                    season_num = season or 0
                    st.session_state["yt_results"] = youtube_series_playlists(
                        item["title"], item.get("year") or "", lang,
                        _omdb_season_episode_count(details, season_num) or 0,
                    )
                    st.session_state["yt_episodes"] = youtube_series_episodes(
                        item["title"], item.get("year") or "", lang, season_num,
                    )


def main():
    st.set_page_config(page_title="Entertainment Finder", layout="centered")

    st.title("Entertainment Finder")
    st.caption(
        "Search any movie, TV series, or anime and find out where it's streaming in India. "
        "Add a language to prioritize it on YouTube (e.g. \"One Piece (Mention Movie or Series after movie name) in Japanese\")."
    )

    with st.form("search_form"):
        query = st.text_input(
            "Title",
            placeholder="e.g. Interstellar, One Piece, Attack on Titan",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Search", type="primary", use_container_width=True)

    if submitted:
        _, hint_label, lang = _extract_hints(query)
        query, season = _extract_season(query)
        st.session_state["season"] = season
        with st.spinner("Searching movies & shows..."):
            results = search(query)
            suggestions = fuzzy_suggest(query) if not results else []
        st.session_state["results"] = results
        st.session_state["suggestions"] = suggestions
        st.session_state["hint_label"] = hint_label
        st.session_state["yt_lang"] = lang
        st.session_state.pop("selected", None)
        st.session_state.pop("providers", None)
        st.session_state.pop("details", None)
        st.session_state.pop("yt_results", None)
        st.session_state.pop("yt_episodes", None)
        if not results and not suggestions:
            if hint_label:
                type_name = "Movie" if hint_label == "movie" else "TV series"
                st.info(
                    f"No {type_name} results found for that title. Try a different spelling."
                )
            else:
                st.info("No results found for that title. Try a different spelling.")

    results = st.session_state.get("results", [])
    if results:
        st.subheader("Results")
        hint_label = st.session_state.get("hint_label")
        if hint_label:
            type_name = "Movie" if hint_label == "movie" else "TV series"
            st.caption(f"Showing {type_name} results only")
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
                if st.button(
                    "⤢",
                    key="zoom-detail",
                    help="Show larger",
                    use_container_width=False,
                ):
                    _show_large_media(selected["poster"])
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
            st.subheader("Description")
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
        is_series = selected.get("media_type") == "tv"
        if YOUTUBE_API_KEY:
            st.divider()
            yt_lang = st.session_state.get("yt_lang") or ""
            lang_name = next(
                (k for k, (iso, m) in _LANGUAGE_HINTS.items() if iso == yt_lang), ""
            )
            lang_note = f" · {lang_name.title()} prioritized" if lang_name else ""
            if is_series:
                st.subheader("Full Series on YouTube")
                if yt_results:
                    st.caption(f"Full-episode playlists (verified by size). Official TV channels prioritized.{lang_note} Pick one and play.")
                    for r in yt_results:
                        ycols = st.columns([1, 3, 1])
                        with ycols[0]:
                            if r.get("thumbnail"):
                                st.image(r["thumbnail"], width=120)
                                if st.button(
                                    "⤢",
                                    key=f"ytzoom-{r.get('playlist_id') or r['video_id']}",
                                    help="Show larger",
                                    use_container_width=True,
                                ):
                                    _show_large_media(r["thumbnail"])
                        with ycols[1]:
                            al = r.get("audio_lang") or ""
                            audio_chip = f" · {_escape(al)} audio" if al else ""
                            tier_tag = {"tv": " · Official TV", "studio": " · Official"}.get(
                                r.get("official_tier", ""), ""
                            )
                            st.markdown(
                                f"**{_escape(r['title'])}**  \n"
                                f"{_escape(r['channel'])} · {r.get('item_count')} episodes{audio_chip}{tier_tag}"
                            )
                        with ycols[2]:
                            st.link_button(
                                "Play",
                                _safe_url(r["link"]),
                                use_container_width=True,
                                type="primary",
                            )
                else:
                    st.info(
                        f"No {lang_name.title()} full-series playlist for this title on YouTube right now."
                        if lang_name
                        else "No verified full-series playlist for this title on YouTube right now."
                    )
                st.subheader("Episodes on YouTube")
                yt_episodes = st.session_state.get("yt_episodes") or []
                if yt_episodes:
                    st.caption(
                        f"Individual full episodes, verified by length and title.{lang_note} Official channels prioritized — Episode 1 first."
                    )
                    for r in yt_episodes:
                        ycols = st.columns([1, 3, 1])
                        with ycols[0]:
                            if r.get("thumbnail"):
                                st.image(r["thumbnail"], width=120)
                                if st.button(
                                    "⤢",
                                    key=f"ytzoom-{r['video_id']}",
                                    help="Show larger",
                                    use_container_width=True,
                                ):
                                    _show_large_media(r["thumbnail"])
                        with ycols[1]:
                            al = r.get("audio_lang") or ""
                            aw = _LANG_BY_ISO.get(al, "") or al
                            audio_chip = f" · {_escape(aw)} audio" if aw else ""
                            tier_tag = {"tv": " · Official TV", "studio": " · Official"}.get(
                                r.get("official_tier", ""), ""
                            )
                            st.markdown(
                                f"**{_escape(r['title'])}**  \n"
                                f"{_escape(r['channel'])} · Ep {r.get('episode')} · {r['duration']} min{audio_chip}{tier_tag}"
                            )
                        with ycols[2]:
                            st.link_button(
                                "Play",
                                _safe_url(r["link"]),
                                use_container_width=True,
                                type="primary",
                            )
                else:
                    st.info(
                        f"No {lang_name.title()} individual full episodes found for this title right now."
                        if lang_name
                        else "No verified individual full episodes found for this title right now."
                    )
            else:
                st.subheader("Full Movie on YouTube")
                if yt_results:
                    st.caption(f"Verified uploads only (full-length, official channels prioritized). Pick one and play.{lang_note}")
                    for r in yt_results:
                        ycols = st.columns([1, 3, 1])
                        with ycols[0]:
                            if r.get("thumbnail"):
                                st.image(r["thumbnail"], width=120)
                                if st.button(
                                    "⤢",
                                    key=f"ytzoom-{r['video_id']}",
                                    help="Show larger",
                                    use_container_width=True,
                                ):
                                    _show_large_media(r["thumbnail"])
                        with ycols[1]:
                            al = r.get("audio_lang") or ""
                            aw = _LANG_BY_ISO.get(al, "") or al
                            audio_chip = f" · {_escape(aw)} audio" if aw else ""
                            tier_tag = {"tv": " · Official TV", "studio": " · Official"}.get(
                                r.get("official_tier", ""), ""
                            )
                            st.markdown(
                                f"**{_escape(r['title'])}**  \n"
                                f"{_escape(r['channel'])} · {r['duration']} min{audio_chip}{tier_tag}"
                            )
                        with ycols[2]:
                            st.link_button(
                                "Play",
                                _safe_url(r["link"]),
                                use_container_width=True,
                                type="primary",
                            )
                else:
                    st.info(
                        f"No {lang_name.title()} version found on YouTube right now."
                        if lang_name
                        else "No verified full movie for this title on YouTube right now."
                    )

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