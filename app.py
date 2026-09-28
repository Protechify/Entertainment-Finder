import concurrent.futures
import difflib
import json
import httpx
import os
import re
from pathlib import Path
from urllib.parse import quote
import streamlit as st
from simplejustwatchapi import exceptions as jw_exceptions
from simplejustwatchapi import offers_for_countries, search as jw_search

OMDB_BASE = "https://www.omdbapi.com/"

# IMDb's public title-suggestion endpoint. Unofficial and undocumented, but it
# needs no API key and is the one source that can find titles OMDb's own search
# misses (see :func:`_imdb_suggest`).
IMDB_SUGGEST_BASE = "https://v2.sg.media-imdb.com/suggestion/x/"

# YouTube Data API v3 (server-side only). Read from the environment so the key
# never sits in the repo; empty string disables the YouTube full-movie section.
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_RUNTIME_TOLERANCE = 0.10

# How many alternate spellings a single title lookup may try. Each one is a
# network round trip, so the list is kept short, is only reached when the
# catalogues have already reported a miss, and the probes run concurrently.
_TITLE_VARIANT_LIMIT = 4


# Anchor the .env to this file rather than the process's working directory:
# Streamlit is routinely launched from a different folder, and a relative
# lookup there leaves OMDB_API_KEY / YOUTUBE_API_KEY empty.
DOTENV_PATH = str(Path(__file__).with_name(".env"))


def _load_dotenv(path: str = DOTENV_PATH) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no dependencies).

    Defaults to the .env sitting next to this file, so keys resolve no matter
    which directory Streamlit was launched from. A key already present in the
    environment wins — but only when it actually holds a value, because an
    empty `KEY=` in the shell would otherwise shadow the real .env entry.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and not os.environ.get(key):
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
OMDB_API_KEY = _secret("OMDB_API_KEY")
CHANNEL_AUDIT_PATH = str(Path(__file__).with_name("channel_audit.json"))


def _load_channel_audit(path: str = CHANNEL_AUDIT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as audit_file:
            data = json.load(audit_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _channel_audit_is_valid(data: dict) -> bool:
    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        return False
    policy = data.get("policy")
    if not isinstance(policy, dict):
        return False
    if policy.get("literal_title_match") is not True:
        return False
    try:
        tolerance = float(policy.get("runtime_tolerance"))
    except (TypeError, ValueError):
        return False
    if abs(tolerance - YOUTUBE_RUNTIME_TOLERANCE) > 1e-9:
        return False
    channels = data.get("channels")
    if not isinstance(channels, dict):
        return False
    if any(
        not isinstance(channel_id, str)
        or not channel_id.startswith("UC")
        or len(channel_id) < 10
        or not isinstance(record, dict)
        or record.get("channel_id") != channel_id
        for channel_id, record in channels.items()
    ):
        return False
    if any(
        record.get("tier") not in {"tv", "studio", "movie_official"}
        for record in channels.values()
    ):
        return False
    if data.get("enforced") is True:
        if not channels or data.get("review_required") is not False:
            return False
        errors = data.get("errors", [])
        unresolved = data.get("unresolved", [])
        waived = data.get("waived", [])
        if not isinstance(errors, list) or errors:
            return False
        if not isinstance(unresolved, list) or not isinstance(waived, list):
            return False
        waived_keys = {" ".join(str(item).split()).casefold() for item in waived}
        for item in unresolved:
            if not isinstance(item, dict) or " ".join(
                str(item.get("name") or "").split()
            ).casefold() not in waived_keys:
                return False
        for record in channels.values():
            if (
                record.get("status") != "qualified"
                or record.get("reviewed") is not True
                or not str(record.get("official_source") or "").strip()
            ):
                return False
    return True


_CHANNEL_AUDIT = _load_channel_audit()
_CHANNEL_AUDIT_VALID = _channel_audit_is_valid(_CHANNEL_AUDIT)
_CHANNEL_AUDIT_ENFORCED = _CHANNEL_AUDIT_VALID and _CHANNEL_AUDIT.get("enforced") is True
_CHANNEL_AUDIT_CHANNELS = _CHANNEL_AUDIT.get("channels", {})
if not _CHANNEL_AUDIT_VALID or not isinstance(_CHANNEL_AUDIT_CHANNELS, dict):
    _CHANNEL_AUDIT_CHANNELS = {}
_CHANNEL_AUDIT_VERSION = str(
    _CHANNEL_AUDIT.get("generated_at") if _CHANNEL_AUDIT_VALID else "legacy"
) or "legacy"

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
    """True only if the URL actually serves an image (guards broken poster links).

    Only the declared content type matters, so the request asks for headers
    alone: a HEAD, falling back to a single-byte ranged GET for the hosts that
    refuse HEAD. Downloading whole posters in order to read a header made every
    search pull down tens of megabytes of images it was about to discard, and
    that transfer — not the catalogue lookups — is what the search was waiting
    on. The cache keeps repeat searches from paying even the small request.
    """
    if not _safe_url(url):
        return False
    attempts = (
        ("head", _BROWSER_HEADERS),
        ("get", {**_BROWSER_HEADERS, "Range": "bytes=0-0"}),
    )
    with httpx.Client(timeout=8, follow_redirects=True) as client:
        for method, headers in attempts:
            try:
                resp = getattr(client, method)(url, headers=headers)
            except httpx.HTTPError:
                return False
            if resp.status_code in (405, 501):
                continue
            if resp.status_code not in (200, 206):
                return False
            ctype = (resp.headers.get("content-type") or "").lower()
            return ctype.startswith("image/")
    return False


def _verify_posters(rows: list) -> list:
    """Confirm the posters on these rows really serve an image, in parallel.

    A search turns up around twenty rows and each check is a network round
    trip, so verifying them one after another is what decides how long a
    search takes. Repairs are run in the same pool for the same reason: a
    broken poster is replaced by asking the other provider for its artwork,
    which is another round trip per row, and those are independent too. The
    row order is left alone.
    """
    pending = [row for row in rows if row.get("poster")]
    if not pending:
        return rows

    def _served(row: dict) -> bool:
        try:
            return _poster_ok(row["poster"])
        except Exception:
            return False

    def _replacement(row: dict):
        try:
            return _fallback_poster(row["title"], row.get("year") or "") or None
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(12, len(pending))
    ) as ex:
        broken = [row for row, ok in zip(pending, ex.map(_served, pending)) if not ok]
        for row, poster in zip(broken, ex.map(_replacement, broken)):
            row["poster"] = poster
    return rows


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

# Curated channels known to legitimately host full movies on YouTube (labels /
# official film-company channels). Matched token-subset like the TV/studio
# lists, so "Goldmines" also matches "Goldmines Hindi". Bonus: keep the shortest
# distinctive name plus any variants that need extra tokens to disambiguate.
_OFFICIAL_MOVIE_CHANNELS = {
    "en": {"Paramount Movies", "MGM", "StudioCanal", "Lionsgate Movies",
           "20th Century Studios"},
    "hi": {"Shemaroo", "Rajshri", "T-Series", "Goldmines",
           "Goldmines Telefilms", "Goldmines Hindi"},
    "ta": {"Sun Pictures", "Sun NXT", "Zee Studios", "AVM Productions", "Sony VIZHA"},
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

_MOVIE_CHANNEL_NAMES = []
for _grp in _OFFICIAL_MOVIE_CHANNELS.values():
    for _name in _grp:
        if _name not in _MOVIE_CHANNEL_NAMES:
            _MOVIE_CHANNEL_NAMES.append(_name)

_CHANNEL_TOKEN_CACHE = {}


def _channel_tokens(name: str) -> frozenset:
    cached = _CHANNEL_TOKEN_CACHE.get(name)
    if cached is None:
        cached = frozenset(_significant_tokens(name))
        _CHANNEL_TOKEN_CACHE[name] = cached
    return cached


# Words too generic to identify a channel on their own. "&TV" reduces to the
# single token "tv", and a subset match then badged any channel with "TV" in
# its name — an aggregator called "Crazy Toon TV" was labelled a broadcaster.
# A whitelist name made only of these is skipped; real names such as "Jaya TV"
# and "Shemaroo" keep matching.
_GENERIC_CHANNEL_TOKENS = frozenset({
    "tv", "movies", "movie", "film", "films", "official", "channel", "media",
    "entertainment", "hd", "4k", "plus", "cinema", "video", "videos", "world",
})


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
    for names, tier in (
        (_TV_CHANNEL_NAMES, "tv"),
        (_STUDIO_CHANNEL_NAMES, "studio"),
        (_MOVIE_CHANNEL_NAMES, "movie_official"),
    ):
        for name in names:
            name_tokens = _channel_tokens(name)
            if not name_tokens or name_tokens <= _GENERIC_CHANNEL_TOKENS:
                continue
            if name_tokens <= tokens:
                return tier
    return ""


_LANG_WORDS = sorted(_LANGUAGE_HINTS, key=len, reverse=True)
_LANG_RE = re.compile(
    r"\b(?:in|with|audio|dub|dubbed|output|subtitles|sub)\s+(" + "|".join(_LANG_WORDS) + r")\b"
    r"|\b(" + "|".join(_LANG_WORDS) + r")\s*(?:audio|dub|dubbed|version|subtitles|sub)\b",
    re.IGNORECASE,
)


def _extract_language_hint(query: str) -> tuple:
    """Strip spoken-language hints ("in English", "Tamil dub") from a query.

    Returns (clean_query, lang) where lang is the ISO 639-1 code of the first
    language mentioned, or '' when the query does not name a language.

    Only UNambiguous "in <lang>" / "<lang> dub" style hints are removed — a
    language word that is part of a movie title ("alagiya tamil magan",
    "Hindi Medium") is kept in the search query.
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


def _omdb_fetch(params: dict) -> tuple[dict, str]:
    """Call the OMDb API and return (data, error).

    OMDb reports failures with a non-2xx status (401 for a bad key or an
    exhausted free quota, 404 for a missing title) but always sends a JSON body
    whose "Error" field says why. httpx does not raise on those statuses, so the
    body is still parsed here and the explanation is handed back to the caller
    instead of being thrown away — that message is what makes a rate limit
    distinguishable from an unconfigured key.

    Returns ({}, reason) on any failure, where reason is OMDb's own "Error"
    text, or a short label for transport problems.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(OMDB_BASE, params=params)
            data = resp.json()
    except httpx.HTTPError:
        return {}, "network error"
    except ValueError:
        return {}, "invalid response"
    if not isinstance(data, dict):
        return {}, "invalid response"
    if data.get("Response") == "True":
        return data, ""
    return {}, str(data.get("Error") or "request failed")


def _omdb_search_type(query: str, media_type: str, label: str) -> list:
    """Rows from one OMDb search call, poster verification included."""
    rows = []
    data, _error = _omdb_fetch(
        {"s": query, "type": media_type, "apikey": OMDB_API_KEY}
    )
    for item in data.get("Search", []):
        title = item.get("Title") or ""
        if not title:
            continue
        poster = item.get("Poster") or ""
        if poster in ("", "N/A"):
            poster = None
        imdb_id = item.get("imdbID")
        rows.append(
            {
                "id": imdb_id,
                "media_type": label,
                "title": title,
                "year": (item.get("Year") or "")[:4],
                "poster": poster,
                "link": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
            }
        )
    return _verify_posters(rows)


def omdb_search(query: str, media_hint: str = "") -> list:
    """Search OMDb for movies and TV series (optionally a single type).

    The movie and series searches are independent requests against a slow
    endpoint — together around 2.5s in a row — so they are issued concurrently
    rather than one after the other, which roughly halves the wait for an
    unhinted search. Movies are still listed before series, because a title
    most people search for is a film far more often than a series.
    """
    if media_hint == "movie":
        types = (("movie", "movie"),)
    elif media_hint == "tv":
        types = (("series", "tv"),)
    else:
        types = (("movie", "movie"), ("series", "tv"))
    if len(types) == 1:
        return _omdb_search_type(query, *types[0])
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(types)) as ex:
        futures = [ex.submit(_omdb_search_type, query, mt, lb) for mt, lb in types]
        results = []
        for fut in futures:
            try:
                results.extend(fut.result(timeout=30))
            except Exception:
                continue
    return results


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def _imdb_suggest_cached(query: str) -> list:
    """Ask IMDb for title suggestions matching `query`. Returns normalised rows.

    The endpoint is unofficial, so every failure is swallowed and reported as
    "no suggestions". A bridge outage must leave search exactly as it behaves
    without this function, never turn into an error.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                IMDB_SUGGEST_BASE + f"{quote(query, safe='')}.json",
                params={"includeVideos": "0"},
                headers={"User-Agent": "EntertainmentFinder/1.0"},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    rows = []
    for entry in data.get("d") or []:
        if not isinstance(entry, dict):
            continue
        imdb_id = entry.get("id") or ""
        kind = entry.get("qid") or entry.get("q") or ""
        label = entry.get("l") or ""
        if not imdb_id.startswith("tt") or kind not in ("movie", "series", "tvMovie", "short", "video"):
            continue
        if not label:
            continue
        rows.append(
            {
                "id": imdb_id,
                "media_type": "tv" if kind == "series" else "movie",
                "title": label,
                "year": str(entry.get("y") or "")[:4],
                "poster": None,
                "link": f"https://www.imdb.com/title/{imdb_id}/",
            }
        )
    return rows


def _imdb_suggest(query: str) -> list:
    """Cached, network-safe wrapper around :func:`_imdb_suggest_cached`.

    Streamlit's cache is bypassed when it has not been initialised (in unit
    tests, and before the first script run), so failures are also trapped here.
    """
    try:
        return _imdb_suggest_cached(query)
    except Exception:
        return []


def _title_variants(query: str) -> list:
    """Plausible alternate spellings of a short title, for a second lookup pass.

    Regional catalogues disagree on romanisation far more often than they
    disagree on the film: the Ajith Kumar film is "Villain" everywhere except
    on YouTube, where uploads are titled "Villan". Because a wrong catalogue
    lookup returns an unrelated film rather than nothing, the extra guesses are
    only ever used against sources that report a real miss.

    The guesses are ranked rather than enumerated, because there are far more
    single-vowel edits than the lookup is allowed to try and the naive order
    spends its whole budget on the first letter of the word. Romanisation drift
    in regional catalogues is overwhelmingly a vowel inserted next to another
    vowel, or a vowel standing in for a neighbouring one, and it happens in the
    second half of a name rather than at its start. So vowel edits are offered
    from the end of the word backwards and only where a vowel actually sits, and
    an edit at either extreme — prepending or appending a vowel — is left out,
    since turning "villan" into "avillan" is not a variant of anything. That
    ordering is what puts "villain" inside the budget for "villan".
    """
    core = "".join(
        ch for ch in (query or "").lower() if ch.isalnum()
    )
    if len(core) < 4:
        return []
    ranked = []
    # A vowel appearing between a vowel and a consonant: villan -> villain.
    for index in range(1, len(core)):
        if core[index - 1] in "aeiou" and core[index] not in "aeiou":
            for rank, vowel in enumerate("aeiou"):
                ranked.append((0, -index, rank, core[:index] + vowel + core[index:]))
    for a, b in (("v", "w"), ("k", "c"), ("f", "ph"), ("s", "sh"), ("z", "s")):
        if a in core:
            ranked.append((1, 0, 0, core.replace(a, b)))
    for a, b in (("w", "v"), ("c", "k"), ("s", "z")):
        if b in core:
            ranked.append((1, 0, 0, core.replace(b, a)))
    for index in range(len(core) - 1, 0, -1):
        if core[index] in "aeiou":
            for rank, vowel in enumerate("aeiou"):
                if vowel != core[index]:
                    ranked.append((2, -index, rank, core[:index] + vowel + core[index + 1:]))
    for index in range(len(core) - 1, 0, -1):
        if core[index] in "aeiou":
            ranked.append((3, -index, 0, core[:index] + core[index + 1:]))
    for index in range(len(core) - 1, 0, -1):
        if index and core[index] == core[index - 1]:
            ranked.append((4, -index, 0, core[:index] + core[index + 1:]))
    ranked.sort()
    seen = {core, (query or "").strip().lower()}
    variants = []
    for _category, _position, _rank, candidate in ranked:
        if candidate in seen or len(candidate) < 4:
            continue
        seen.add(candidate)
        variants.append(candidate)
    return variants[:_TITLE_VARIANT_LIMIT]


def _imdb_candidate_is_the_film(
    candidate: dict, title: str, year: str = "", media_type: str = ""
) -> bool:
    """Decide whether an IMDb suggestion really is the film being looked for.

    IMDb suggestions are matched on the name alone, so a bare "Villain" comes
    back as a 1971 film, a 1979 film, a 2014 film and a 2020 film. Spelling
    cannot separate them — the 1971 entry is a perfect match for the name, and
    the wanted 2002 film is one letter away from the misspelling everyone else
    uses. Only the year can, so a year is required and a candidate without one
    is refused rather than guessed at. Attaching a plausible-looking but wrong
    plot is precisely what this whole path exists to prevent.

    With a year in hand the rule is deliberately loose on spelling: the year
    carries the identity and the comparison is only a sanity bound that keeps
    "Villa des roses" (0.53) out while admitting "Villain" (0.92). The strict
    0.80 one-letter rule in :func:`_yt_title_cores_match` is not used here,
    because rejecting single-letter differences is the very thing that hid
    this film.
    """
    if not year:
        return False
    cand_year = str(candidate.get("year") or "")
    if not cand_year or cand_year[:4] != year[:4]:
        return False
    if media_type and candidate.get("media_type") != media_type:
        return False
    want = _yt_title_core(title)
    have = _yt_title_core(candidate.get("title") or "")
    if not want or not have:
        return False
    return difflib.SequenceMatcher(None, want, have).ratio() >= 0.80


def _imdb_year_from_uploads(uploads: list) -> str:
    """The year a YouTube upload states in its own title, for identity lookups.

    The upload is the only remaining evidence of what film was wanted, and its
    title usually carries the release year ("Villan (2002) Full Tamil Movie").
    """
    for upload in uploads or []:
        year = _YEAR_RE.search(upload.get("title") or "")
        if year:
            return year.group()
    return ""


def _resolve_by_imdb(title: str, year: str, media_type: str = "") -> dict | None:
    """Identify a title by asking IMDb for suggestions, returning the match or None.

    The typed year is appended to the name because a bare name is genuinely
    ambiguous: "villain" suggests a 1971 film, a 2020 film and the 2002 film
    alike, while "villan 2002" puts the right one first. That probe is tried on
    its own first, and only if it comes back empty are spelling variants tried
    at all — which is what catches the YouTube spelling "villan" for the film
    every catalogue calls "villain". The variants are probed concurrently
    because they are independent round trips, and the list is short, so waiting
    for them in turn would dominate the time a miss costs.

    Every candidate must survive :func:`_imdb_candidate_is_the_film`, so a
    suggestion that merely sounds similar can never attach the wrong plot.
    """
    probes = [f"{title} {year}".strip() if year else title]
    for variant in _title_variants(title):
        probes.append(f"{variant} {year}".strip() if year else variant)
    if len(probes) == 1:
        return _first_imdb_match(probes[0], title, year, media_type)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as ex:
        futures = [
            ex.submit(_first_imdb_match, probe, title, year, media_type)
            for probe in probes
        ]
        for fut in futures:
            try:
                found = fut.result(timeout=30)
            except Exception:
                continue
            if found:
                return found
    return None


def _first_imdb_match(
    probe: str, title: str, year: str, media_type: str
) -> dict | None:
    """First suggestion under one probe spelling that is really the film."""
    for candidate in _imdb_suggest(probe):
        if _imdb_candidate_is_the_film(candidate, title, year, media_type):
            return candidate
    return None


def omdb_details(selected: dict) -> tuple[dict, str]:
    """Fetch full detail (plot, ratings, cast, etc.) from OMDb for a selected title.

    Returns (details, error); error is '' on success and otherwise carries the
    reason runtime verification cannot run.
    """
    if selected.get("youtube_only"):
        # No catalogue row exists, so there is no id to ask OMDb about. A blind
        # `t=` lookup is worse than useless here: it resolves the name to
        # whatever unrelated title OMDb considers closest — "Villan" gives a
        # 1920 silent film, "Villain" a 2020 Korean film — and the UI would
        # then show that film's plot and cast.
        #
        # So identify the film first. A YouTube upload usually names the release
        # year in its own title, which is enough to ask IMDb for suggestions,
        # and "villan 2002" returns the 2002 film first where "villan" alone
        # does not. The suggestion is then checked for year and spelling before
        # OMDb is asked for the real record by IMDb id.
        uploads = st.session_state.get("yt_results") or []
        year = _extract_year(selected.get("year") or _imdb_year_from_uploads(uploads))[1]
        found = _resolve_by_imdb(selected.get("title") or "", year, selected.get("media_type") or "")
        if found is None:
            return {}, "not in the streaming catalogues"
        selected["id"] = found["id"]
        selected["year"] = found["year"] or selected.get("year") or ""
        selected["title"] = found["title"]
        selected["link"] = found["link"]
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
    details, error = _omdb_fetch(params)
    if details and not selected.get("poster"):
        # The resolved record carries the poster the missing catalogue row had
        # no way of supplying.
        poster = details.get("Poster") or ""
        if poster and poster != "N/A" and _poster_ok(poster):
            selected["poster"] = poster
    return details, error


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
    data, _error = _omdb_fetch(
        {"apikey": OMDB_API_KEY, "i": imdb_id, "Season": season}
    )
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


class _YouTubeSearchError(Exception):
    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


def _youtube_api_status(response, data) -> str:
    try:
        http_status = int(getattr(response, "status_code", 200) or 200)
    except (TypeError, ValueError):
        http_status = 200
    payload_error = data.get("error", {}) if isinstance(data, dict) else {}
    if not isinstance(payload_error, dict):
        payload_error = {}
    try:
        api_code = int(payload_error.get("code") or http_status)
    except (TypeError, ValueError):
        api_code = http_status
    reason = " ".join(
        str(payload_error.get(key) or "")
        for key in ("message", "errors")
    ).lower()
    if api_code == 429 or "quota" in reason:
        return "quota"
    if api_code in (401, 403):
        return "auth"
    if api_code >= 500:
        return "unavailable"
    if api_code >= 400:
        return "error"
    return "ok"


def _youtube_response_data(response) -> dict:
    try:
        data = response.json()
    except (TypeError, ValueError):
        raise _YouTubeSearchError("unavailable") from None
    status = _youtube_api_status(response, data)
    if status != "ok":
        raise _YouTubeSearchError(status)
    if not isinstance(data, dict):
        raise _YouTubeSearchError("error")
    return data


def _youtube_runtime_matches(duration: int, expected_minutes: int) -> bool:
    if expected_minutes <= 0:
        return False
    return abs(duration - expected_minutes) <= expected_minutes * YOUTUBE_RUNTIME_TOLERANCE


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


# Pirate uploaders self-identify ("Filmy Zone", "Hd Facts", "TamilRockers",
# "TamilYogi", "Isaimini", ...) in their channel name or video title. Words are
# space-agnostic ("Filmy Zone"/"FilmyZone", "Hd Facts"/"HDFacts") so both
# formats are caught; \b keeps substrings of legit words from matching.
_PIRACY_RE = re.compile(
    r"\b(?:tamil\s*rockers?|tamil\s*yog[yi]|tamilyogi|isaimini|"
    r"kutty\s*movies?|madras\s*rockers?|1\s*tamil\s*mv|tamil\s*gun|"
    r"filmy\s*zones?|hd\s*facts|filmy\s*wap|9x\s*movies?|hi\s*movies?|"
    r"movi[ez]+\s*zones?|movie\s*hubs?|world\s*free\s*4\s*u|bolly\s*4\s*u|"
    r"veedigital|mtalkies?|"
    r"new\s*south\s*movies?|south\s*(?:indian\s*)?movies?\s*(?:in\s+)?hindi|"
    r"hindi\s*south\s*movies?|full\s*movies?\s*in\s+hindi)\b",
    re.IGNORECASE,
)


def _looks_pirated(channel: str, title: str) -> bool:
    """True when a channel or video title brands itself as a pirated upload."""
    blob = " | ".join(p for p in (channel or "", title or "") if p)
    return bool(_PIRACY_RE.search(blob)) or _looks_like_dub_channel(channel)


def _looks_like_dub_channel(channel: str) -> bool:
    """True when a channel names itself '<word> Soft' (Hindi-dub re-upload).

    Channels like "Facti Soft" re-upload (often mislabeled) regional films with
    interview-style titles; the '<word> Soft' naming is characteristic of them.
    """
    return bool(channel and re.search(r"\b\w+\s+soft\b", channel, re.IGNORECASE))


def _omdb_runtime_minutes(details: dict) -> int:
    """OMDb movie 'Runtime' ("110 min") -> total minutes; 0 if missing/invalid."""
    runtime = str((details or {}).get("Runtime") or "")
    m = re.search(r"(\d+)", runtime)
    return int(m.group(1)) if m else 0


# Substrings OMDb uses in its "Error" field, mapped to the state they mean.
_OMDB_ERROR_STATES = (
    (("daily limit", "limit reached", "too many requests", "requests per day"),
     "rate_limited"),
    (("api key", "unauthorized", "unrecognized", "not authorized"),
     "auth_failed"),
    (
        ("not found", "unknown id", "incorrect id", "not in the streaming catalog"),
        "not_found",
    ),
)


def _omdb_status(details: dict, error: str = "") -> str:
    """Classify what OMDb managed to tell us about the selected title.

    'ready' is the only state that lets YouTube runtime verification run. Every
    other state needs its own message: telling the user to "set OMDB_API_KEY"
    when the real problem is an exhausted free quota sends them off to fix
    something that is already configured.
    """
    if not OMDB_API_KEY:
        return "not_configured"
    if not details:
        reason = str(error or "").casefold()
        for markers, state in _OMDB_ERROR_STATES:
            if any(marker in reason for marker in markers):
                return state
        return "unavailable"
    if _omdb_runtime_minutes(details) <= 0:
        return "runtime_missing"
    return "ready"


# One honest sentence per state. Runtime verification is gated on "ready", so
# each message explains what is missing. None of them stop the YouTube search —
# without a reference runtime the app still lists full-length uploads, just
# without the length cross-check.
_OMDB_STATUS_MESSAGES = {
    "not_configured": (
        "OMDb is not configured, so runtimes can't be cross-checked and title "
        "details are unavailable. Set OMDB_API_KEY in .env (or as an environment "
        "variable), restart Streamlit, then select the title again."
    ),
    "auth_failed": (
        "OMDb rejected the API key, so runtimes can't be cross-checked and title "
        "details are unavailable. Check OMDB_API_KEY is a valid key from "
        "omdbapi.com, then restart Streamlit."
    ),
    "rate_limited": (
        "OMDb's free 1,000 requests/day limit is used up, so runtimes can't be "
        "cross-checked and title details are unavailable until the limit resets. "
        "Add a paid OMDb key to lift it."
    ),
    "not_found": (
        "OMDb has no record for this title, so the video lengths below weren't "
        "cross-checked against a runtime."
    ),
    "unavailable": (
        "Couldn't load OMDb data, so the video lengths below weren't cross-checked "
        "against a runtime and title details are unavailable."
    ),
    "runtime_missing": (
        "OMDb recorded no runtime for this title, so the video lengths below "
        "weren't cross-checked."
    ),
}


def _omdb_status_message(status: str) -> str:
    return _OMDB_STATUS_MESSAGES.get(
        status,
        "OMDb runtime data could not be loaded, so the video lengths below weren't "
        "cross-checked. Select the title again.",
    )


# States where the app is working with reduced information rather than being
# misconfigured. These get a quiet note instead of a warning.
_OMDB_BENIGN_STATES = frozenset({"not_found", "runtime_missing"})


def _omdb_original_lang(details: dict) -> str:
    """OMDb movie 'Language' field ("Tamil, Hindi") -> ISO of the primary language.

    Returns '' when no listed language is one the app can verify against (e.g.
    only "Marathi"), so the caller falls back to no language enforcement.
    """
    lang_field = str((details or {}).get("Language") or "")
    for token in re.split(r"[,\s]+", lang_field):
        token = token.strip().lower()
        for word, (iso, _marker) in _LANGUAGE_HINTS.items():
            if token == word:
                return iso
    return ""


# Full-movie results are restricted to these channel tiers ("trusted").
_TRUSTED_TIERS = ("tv", "studio", "movie_official")


def _channel_tier_for_video(channel: str, channel_id: str) -> str:
    if not _CHANNEL_AUDIT_ENFORCED:
        return _channel_tier(channel)
    record = _CHANNEL_AUDIT_CHANNELS.get(channel_id)
    if not isinstance(record, dict) or record.get("status") != "qualified":
        return ""
    tier = record.get("tier") or ""
    return tier if isinstance(tier, str) and tier in _TRUSTED_TIERS else ""


# Ranking bonus for trusted channel tiers (module-level, shared by every
# YouTube flow).
_TIER_BONUS = {"tv": 300, "studio": 150, "movie_official": 150}

# How many verified uploads to show. Most films have one or two legitimate
# uploads, so this is a ceiling rather than a target.
YOUTUBE_MOVIE_RESULT_LIMIT = 5

# Set True to also keep strictly-verified uploads from NON-trusted channels
# (title matches exactly + passes the language/duration checks). Default False =
# "trusted channels only"; the sidebar toggle flips it per session, because the
# only legitimate upload of a regional film is often on a channel nobody has
# reviewed.
_ALLOW_UNTRUSTED_VERIFIED = False


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _youtube_full_movie_cached(
    title: str, year: str = "", media_type: str = "", lang: str = "",
    expected_minutes: int = 0, strict_lang: bool = False,
    audit_version: str = "", include_unofficial: bool = False,
    search_epoch: str = "",
) -> list:
    """Find YouTube uploads that are actually full movies / full episodes.

    Searches YouTube "full movie" / "full episodes", then fetches every video's
    duration in one batch call and keeps only entries long enough to be a real
    film or episode (trailers, recaps, and explainers are far shorter and are
    also title-filtered). Results are ranked by title+year match. A language
    hint (e.g. 'ta') biases the search and boosts matching titles. When
    `expected_minutes` is given (movie runtime from OMDb), uploads whose
    duration deviates by more than 10% are dropped. `lang` is the effective
    language (user's hint, else the movie's original language when no hint was
    given); with `strict_lang=True` the upload must also name that language.

    `expected_minutes` of 0 means OMDb has no record for the title, so there is
    nothing to compare against. The runtime check is then skipped rather than
    failed — a long-enough, correctly-titled upload is shown and flagged as
    unverified, because reporting nothing at all would hide a film that is
    genuinely on YouTube.
    """
    if not YOUTUBE_API_KEY:
        return []
    marker = _LANGUAGE_HINTS.get(lang, (None, ""))[1]
    keyword = "full movie" if media_type == "movie" else "full episodes"
    min_length = 90 if media_type == "movie" else 20
    runtime_known = expected_minutes > 0

    query_status = "ok"
    successful_queries = 0

    def _collect(query: str, use_language: bool) -> list:
        nonlocal query_status, successful_queries
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
        if use_language and marker:
            params["relevanceLanguage"] = lang
        try:
            with httpx.Client(timeout=10) as client:
                search_data = _youtube_response_data(
                    client.get(YOUTUBE_SEARCH_URL, params=params)
                )
                items = search_data.get("items") or []
                if not isinstance(items, list):
                    raise _YouTubeSearchError("error")
                ids = ",".join(
                    item["id"]["videoId"]
                    for item in items
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), dict)
                    and item["id"].get("videoId")
                )
                durations = {}
                audio_langs = {}
                statuses = {}
                descriptions = {}
                if ids:
                    videos_data = _youtube_response_data(
                        client.get(
                            YOUTUBE_VIDEOS_URL,
                            params={
                                "part": "snippet,contentDetails,status",
                                "id": ids,
                                "key": YOUTUBE_API_KEY,
                            },
                        )
                    )
                    videos = videos_data.get("items") or []
                    if not isinstance(videos, list):
                        raise _YouTubeSearchError("error")
                    for video in videos:
                        if not isinstance(video, dict):
                            continue
                        durations[video["id"]] = _youtube_duration_minutes(
                            (video.get("contentDetails", {}) or {}).get("duration", "")
                        )
                        snippet = video.get("snippet", {}) or {}
                        if not isinstance(snippet, dict):
                            snippet = {}
                        audio_langs[video["id"]] = _normalize_audio_lang(
                            snippet.get("defaultAudioLanguage")
                        )
                        descriptions[video["id"]] = (
                            snippet.get("description", "") or ""
                        )[:400]
                        stt = video.get("status", {}) or {}
                        statuses[video["id"]] = (
                            stt.get("privacyStatus", ""),
                            bool(stt.get("embeddable")),
                            stt.get("uploadStatus", ""),
                        )
        except _YouTubeSearchError as exc:
            if query_status == "ok":
                query_status = exc.status
            return []
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            if query_status == "ok":
                query_status = "unavailable"
            return []

        successful_queries += 1
        picked = []
        for item in items:
            if not isinstance(item, dict):
                continue
            vid = (item.get("id", {}) or {}).get("videoId", "")
            snippet = item.get("snippet", {}) or {}
            if not isinstance(snippet, dict):
                snippet = {}
            if not vid:
                continue
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
            channel_id = snippet.get("channelId", "") or ""
            if any(word in video_title.lower() for word in _YOUTUBE_BAD_WORDS):
                continue
            if _looks_like_reaction(channel, video_title, descriptions.get(vid, "")):
                continue
            audio = audio_langs.get(vid, "")
            # A language the user asked for ("interstellar in tamil") is a
            # requirement. A language inferred from OMDb's record is only a
            # hint: uploader-set defaultAudioLanguage is routinely wrong for
            # regional and dubbed uploads (a Kannada film tagged "en"), so
            # hard-filtering on it hid films that were there. It ranks instead,
            # below.
            if lang and strict_lang:
                if audio and audio != lang:
                    continue
                if _other_lang_audio(video_title, lang):
                    continue
                if not _confirm_lang(video_title, audio, lang):
                    continue
            thumb = (
                (snippet.get("thumbnails", {}) or {}).get("medium", {}) or {}
            ).get("url", "") or ""
            duration = durations[vid]
            tier = _channel_tier_for_video(channel, channel_id)
            mismatch = runtime_known and not _youtube_runtime_matches(
                duration, expected_minutes
            )
            picked.append(
                {
                    "video_id": vid,
                    "title": video_title,
                    "channel": channel,
                    "channel_id": channel_id,
                    "official_tier": tier,
                    "duration": duration,
                    "audio_lang": audio,
                    "thumbnail": thumb,
                    "duration_mismatch": mismatch,
                    "runtime_verified": runtime_known and not mismatch,
                    "link": f"https://www.youtube.com/watch?v={vid}",
                }
            )
        return picked

    queries = [
        (f"{title} {year} {keyword}".strip(), True),
        (f"{title} {keyword}".strip(), False),
    ]
    if marker:
        queries.append((f"{title} {marker} {keyword}".strip(), True))
    queries = list(dict.fromkeys(queries))
    pool = []
    allow_unofficial = include_unofficial or _ALLOW_UNTRUSTED_VERIFIED

    def _verified(candidates: list) -> list:
        out = []
        for result in candidates:
            if _looks_pirated(result["channel"], result["title"]):
                continue
            if result["official_tier"] not in _TRUSTED_TIERS and not allow_unofficial:
                continue
            if not _yt_title_exact_match(result["title"], title):
                continue
            if result.get("duration_mismatch"):
                continue
            # A language the user asked for must be obeyed. A language OMDb
            # inferred for the film is only a hint, because regional uploads
            # frequently declare the wrong `defaultAudioLanguage`; it still
            # disqualifies a candidate whose own title and audio both point at
            # another language, which is how the unrelated Kannada "Villan"
            # film was being offered alongside the Tamil one.
            if lang and strict_lang and audio_disagrees(result):
                continue
            out.append(result)
        return out

    def audio_disagrees(result: dict) -> bool:
        if not lang or strict_lang or not result.get("audio_lang"):
            return False
        if _other_lang_audio(result["title"], lang):
            return True
        return _normalize_audio_lang(result["audio_lang"]) != lang

    for query, use_language in queries:
        picked = _collect(query, use_language)
        if not picked and query_status != "ok":
            break
        by_id = {result["video_id"]: result for result in [*pool, *picked]}
        pool = list(by_id.values())
        if _verified(pool):
            break

    results = _verified(pool)
    if not results and query_status != "ok":
        raise _YouTubeSearchError(query_status)

    results.sort(
        key=lambda result: (
            _yt_relevance_score(result["title"], title, year)
            + _TIER_BONUS.get(result["official_tier"], 0)
            + (60 if marker and marker in result["title"].lower() else 0)
            # OMDb's language, when the user didn't name one, is a preference
            # rather than a filter.
            + (40 if lang and not strict_lang and result.get("audio_lang") == lang else 0),
            result["duration"],
        ),
        reverse=True,
    )
    return results[:YOUTUBE_MOVIE_RESULT_LIMIT]


# Set once YouTube reports its daily search cap is reached. Searching costs
# 100 units per query and a title lookup runs three of them, so the cap goes
# quickly and retrying after it is gone is pure waste.
_YT_QUOTA_SPENT = False


def _mark_yt_quota_spent() -> None:
    global _YT_QUOTA_SPENT
    _YT_QUOTA_SPENT = True


def youtube_full_movie(
    title: str, year: str = "", media_type: str = "", lang: str = "",
    expected_minutes: int = 0, strict_lang: bool = False,
    audit_version: str = "", include_unofficial: bool = False,
    search_epoch: str = "",
) -> list:
    # Once YouTube's daily search cap is reached, every later lookup is a
    # guaranteed 429, and each attempt would still cost the three search
    # queries this runs. Latching the failure means browsing titles stays
    # responsive and the quota that is left is not spent on requests that
    # cannot succeed. The latch is per-process, so a restart picks YouTube
    # back up as soon as the counter resets.
    if _YT_QUOTA_SPENT:
        st.session_state["yt_status"] = "quota"
        return []
    try:
        results = _youtube_full_movie_cached(
            title,
            year,
            media_type,
            lang,
            expected_minutes,
            strict_lang,
            audit_version,
            include_unofficial,
            search_epoch,
        )
    except _YouTubeSearchError as exc:
        st.session_state["yt_status"] = exc.status
        if exc.status == "quota":
            _mark_yt_quota_spent()
        return []
    st.session_state["yt_status"] = "ok" if results else "no_results"
    return results


# Why a YouTube lookup came back empty. Without these, an exhausted quota reads
# as "this movie isn't on YouTube", which is a false negative the user cannot
# act on.
_YT_STATUS_MESSAGES = {
    "quota": (
        "YouTube's daily search quota is used up, so no full-movie results "
        "could be looked up. The rest of the app is unaffected — this is a "
        "limit on YouTube's search API, which costs 100 units per query "
        "(3 per title here, so roughly 30 titles a day). It resets at "
        "midnight Pacific time."
    ),
    "auth": (
        "YouTube rejected the API key, so full-movie results couldn't be checked. "
        "Check YOUTUBE_API_KEY, then restart Streamlit."
    ),
    "unavailable": (
        "Couldn't reach YouTube, so full-movie results couldn't be checked. Try "
        "again in a minute."
    ),
    "error": (
        "YouTube returned an unexpected response, so full-movie results couldn't be "
        "checked. Try again in a minute."
    ),
}

_YT_FAILURE_STATES = frozenset(_YT_STATUS_MESSAGES)


def _yt_status_message(status: str) -> str:
    return _YT_STATUS_MESSAGES.get(status, "")


youtube_full_movie.clear = _youtube_full_movie_cached.clear


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
    "tamilrockers", "tamilyogi", "copyright", "song", "video", "trailer",
    "review", "explained", "reaction", "breakdown",
    # Plurals of the above. Titles are inconsistent ("Full Movie" vs "Full
    # Movies"), and only explicit entries are safe: stripping a trailing "s"
    # would turn "3 Idiots" into "3 Idiot".
    "movies", "films", "songs", "videos", "dubs", "trailers", "reviews",
)


def _yt_title_core(text: str) -> str:
    """Upload noise removed from a title so only the film's own words remain.

    Drops generic markers ("full movie", "in english", "1080p"), standalone
    years, and the function words that glue a language or quality note onto a
    title. Applied to the queried title as well as the upload, so a film whose
    name genuinely contains a stopword ("Life In a Nutshell") still matches
    itself.
    """
    return "".join(
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if word not in _YT_TITLE_MARKERS
        and word not in _STOPWORDS
        and not re.fullmatch(r"19\d\d|20\d\d", word)
    )


# "Chapter 1", "Part 2" — kept in the title rather than stripped, because the
# number after them is what keeps a film's entries apart.
_YT_PART_TOKENS = ("chapter", "part", "vol", "season", "episode", "chap")


def _yt_title_cores_match(query_core: str, chunk_core: str) -> bool:
    """Decide whether one reduced title chunk stands for the queried film.

    Two rules, in order:

    * Numbers must agree when both sides carry them. A fuzzy comparison alone
      rates "kgfchapter1" and "kgfchapter2" at 0.92, i.e. the same film. A
      single side having no number is not a conflict, so "K.G.F: Chapter 1"
      can still match an upload that just says "K.G.F: Chapter 1 Hindi".
    * Otherwise the spellings must be near-identical, which admits the
      romanisation drift endemic in regional catalogues — OMDb and JustWatch
      write "Samuthiram" where the studio's own upload says "Samudhiram" — while
      still separating different films that share a first syllable
      ("Samuthiram" vs "Samasthanam" scores 0.67, "Aanandham" vs "Aanandham
      Aarambam" scores 0.78).

    Containment is deliberately *not* used. A sequel's title is its predecessor
    plus extra words, so accepting "shorter title is inside longer title" would
    answer every query for the first film with uploads of the second.
    """
    if not query_core or not chunk_core:
        return False
    if query_core == chunk_core:
        return True
    query_nums = re.findall(r"\d+", query_core)
    chunk_nums = re.findall(r"\d+", chunk_core)
    if query_nums and chunk_nums and query_nums != chunk_nums:
        return False
    # A trailing number is the entry of a series, so an upload that stops
    # before it is a different film: "Bigil" is not "Bigil 2". Leading numbers
    # are part of the name and are left alone ("3 Idiots").
    query_tail = re.search(r"\d+$", query_core)
    chunk_tail = re.search(r"\d+$", chunk_core)
    if query_tail and (not chunk_tail or chunk_tail.group() != query_tail.group()):
        return False
    # A one or two letter tail is a sequel marker rather than a spelling
    # variant: "3 Idiots" and "3 Idiots K" are 0.93 similar. A plural is not.
    shorter, longer = sorted((query_core, chunk_core), key=len)
    if (
        longer.startswith(shorter)
        and longer[-1] != "s"
        and re.fullmatch(r"[a-z]{1,2}", longer[len(shorter):])
    ):
        return False
    # One character longer is a different word far more often than a variant of
    # the same one, and the fuzzy step cannot see the difference: "villan" and
    # "villain" are 0.92 similar. The only exemptions are a plural "s" and a
    # doubled letter, so a film's name can still grow a suffix.
    if len(longer) - len(shorter) == 1 and longer[-1] != "s" and longer != shorter + shorter[-1]:
        return False
    # Similarity alone is too generous when the upload's title is much longer:
    # "interstellar" and "interstellarwars" rate 0.96. Transliteration drift
    # costs a letter or two, not a third of the name, so cap the gap.
    if len(longer) - len(shorter) > max(2, len(shorter) // 4):
        return False
    return difflib.SequenceMatcher(None, query_core, chunk_core).ratio() >= 0.80


def _yt_title_exact_match(result_title: str, title: str) -> bool:
    """True when a result's title names the queried movie.

    The title is split into chunks by typical upload separators, and each chunk
    is reduced to the film's own words before being compared — so
    "Aanandham | Tamil Full Movie |..." passes, "Interstellar Full Movie In
    English" passes (the trailing "in" and language note are not part of the
    name), and "Aanandham Aarambam Tamil Full Movie" (a different film glued
    on) still fails. Chunk comparison is delegated to
    :func:`_yt_title_cores_match` so spelling variants still land.
    """
    q = _yt_title_core(title)
    if not q:
        return False
    for chunk in re.split(r"[|,()\[\]:;_~*\-]+", result_title):
        if _yt_title_cores_match(q, _yt_title_core(chunk)):
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
    title: str, year: str = "", lang: str = "", expected_episodes: int = 0,
    audit_version: str = "",
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
            channel_id = snippet.get("channelId", "") or ""
            # Reaction channels build "watch the whole series" playlists too —
            # drop them using channel + title + description signals.
            if _looks_like_reaction(channel, playlist_title, snippet.get("description") or ""):
                continue
            tier = _channel_tier_for_video(channel, channel_id)
            score = (
                _yt_relevance_score(playlist_title, title, year)
                + min(item_count, 60)
                + (60 if marker and marker in lower_title else 0)
                + _TIER_BONUS.get(tier, 0)
            )
            results.append(
                {
                    "playlist_id": pid,
                    "title": playlist_title,
                    "channel": channel,
                    "channel_id": channel_id,
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
                        + {"tv": 300, "studio": 150, "movie_official": 150}.get(r.get("official_tier", ""), 0)
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
    title: str, year: str = "", lang: str = "", season: int = 0,
    audit_version: str = "",
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
            channel_id = snippet.get("channelId", "") or ""
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
            tier = _channel_tier_for_video(channel, channel_id)
            score = (
                _yt_relevance_score(video_title, title, year)
                + {"tv": 300, "studio": 150, "movie_official": 150}.get(tier, 0)
                + (30 if marker and marker in lower_title else 0)
            )
            results.append(
                {
                    "video_id": vid,
                    "title": video_title,
                    "channel": channel,
                    "channel_id": channel_id,
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
    if verify_poster:
        _verify_posters(results)
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
    """True if the title contains every significant query word.

    Matching is per word, and only in the direction where the query word sits
    inside a title word ("iron" in "Ironman"). Comparing against a run of glued
    characters meant "Villa Negra" collapsed to "villanegra", which contains the
    query "villan" and put an unrelated 1963 film at the top of a search for the
    Ajith Kumar movie. The gap is capped so "villan" does not match the much
    longer "villanelle".
    """
    words = {
        "".join(ch for ch in word if ch.isalnum())
        for word in (title or "").lower().split()
    }
    words.discard("")
    if not words:
        return False
    for token in _significant_tokens(query):
        if any(
            token == word
            # "iron" in "Ironman" and "man" in "Ironman": a compound title
            # written as one word still names the film.
            or (len(token) >= 3 and word.startswith(token) and len(word) - len(token) <= 3)
            or (len(token) >= 3 and word.endswith(token) and len(word) - len(token) <= 4)
            for word in words
        ):
            continue
        return False
    return True


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
    a_title = a.get("title") or ""
    b_title = b.get("title") or ""
    a_tokens = _token_set(a_title)
    b_tokens = _token_set(b_title)
    if not a_tokens or not b_tokens:
        return False
    overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
    if overlap >= 0.6:
        return True
    # Spelling-variant listings ("Modha Rathiri" vs "Modha Rathri") share fewer
    # than 60% of their words, so token overlap misses them. Char-level
    # similarity catches them while keeping genuinely different films apart
    # (year clash already returned False above).
    return _fuzzy_score(a_title, b_title) >= 0.8


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
    """Retry queries for the fuzzy fallback: raw query, token windows, and
    prefixes of the longest token (JustWatch tokenizes short prefixes well).

    Token windows matter most: for "alagiya tamil magan" we ALSO probe each
    word alone and every word-group ("tamil magan", "alagiya tamil", ...). A
    single overlapping word (like "magan") is enough for JustWatch/OMDb to
    surface the intended title when the user typed a different transliteration
    of the other words (azhagiya/alagiya, tamizh/tamil). Single tokens are
    ordered first because they are the fastest, highest-yield probes.
    """
    tokens = _significant_tokens(query)
    relaxed = [query] + ([" ".join(tokens)] if tokens else [])
    relaxed += tokens
    for width in range(len(tokens), 1, -1):
        for start in range(len(tokens) - width + 1):
            relaxed.append(" ".join(tokens[start : start + width]))
    longest = max(tokens, key=len) if tokens else ""
    if len(longest) >= 4:
        relaxed += [longest[:n] for n in (8, 7, 6, 5, 4) if len(longest) >= n]
    seen, out = set(), []
    for q in relaxed:
        key = _normalize_title(q)
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:10]


def _fuzzy_score(query: str, title: str) -> float:
    """String similarity (0..1) between a query and a candidate title."""
    q = _normalize_title(query)
    t = _normalize_title(title)
    if not q or not t:
        return 0.0
    return difflib.SequenceMatcher(None, q, t).ratio()


def _fuzzy_suggest_score(query: str, title: str) -> float:
    """Similarity plus a token-overlap bonus for the "did you mean" fallback.

    A typo'd/transliterated query ("alagiya tamil magan") often scores high on
    the corrected spelling, but when ≥50% of its significant words literally
    appear in the candidate title (e.g. "tamil", "magan"), it is almost surely
    the intended pick — lift it slightly while unrelated titles (ratio ~0.3)
    stay far below the 0.82 cutoff.
    """
    score = _fuzzy_score(query, title)
    q_tokens = _significant_tokens(query)
    t_norm = _normalize_title(title)
    if q_tokens and t_norm:
        overlap = sum(1 for t in q_tokens if t in t_norm) / len(q_tokens)
        if overlap >= 0.5:
            score = min(1.0, score + 0.05 * overlap)
    return score


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
        + _relaxed_queries(query)[1:]
        + (_spelling_variants(query) if len(query) >= 6 else [])
    ))[1:][:10]  # raw query already searched by search(); window probes first
    pool = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(queries) or 1)) as ex:
        futures = [
            ex.submit(_suggest_source, q, media_hint)
            for q in queries
        ]
        for fut in futures:
            try:
                items = fut.result(timeout=45)
            except Exception:
                continue
            for item in items:
                key = _dedup_key(item)
                if any(_dedup_key(k) == key for k in pool):
                    continue
                if any(_fuzzy_same_movie(item, k) for k in pool):
                    continue
                if media_hint and item["media_type"] != media_hint:
                    continue
                pool.append(item)
    scored = [
        (score, item)
        for item in pool
        if (score := _fuzzy_suggest_score(query, item["title"])) >= _MIN_SUGGEST_SCORE
        and len(_normalize_title(item["title"])) >= _MIN_SUGGEST_TITLE_LEN
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [item for _, item in scored[:limit]]
    return _verify_posters(chosen)


def _correction_candidates(query: str, results: list) -> list:
    """The titles worth offering as a correction for a misspelled query.

    Both catalogues answer a misspelling rather than refusing it, so "villan"
    comes back as a 1920 silent film and a Kannada one, and the wrong answer
    gets picked by default. Scoring what the search already returned and
    labelling the close ones as an explicit correction fixes that without a
    single extra request; only a query whose own results offer nothing
    plausible falls through to the wider `fuzzy_suggest` sweep.

    Similarity decides, not the word-overlap ordering used for search results.
    `_title_score` rates "John Wick" a top mark of 3 for the query "jonas",
    because it measures how results should be ordered rather than how well
    they answer the question. The 0.82 similarity gate is what separates a
    real correction ("villan" -> "Villain", 0.92) from a coincidence
    ("jonas" -> "John Wick", 0.46).
    """
    if not query:
        return []
    scored = [
        (score, item)
        for item in results
        if (score := _fuzzy_suggest_score(query, item["title"])) >= _MIN_SUGGEST_SCORE
        and len(_normalize_title(item["title"])) >= _MIN_SUGGEST_TITLE_LEN
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    # The same film arrives from more than one provider, and can also turn up
    # twice from the misspelling pass, so collapse by title: three rows all
    # reading "Villain" is not a choice, it is a stutter.
    picks = []
    seen = set()
    for _score, item in scored:
        key = _normalize_title(item["title"])
        if key in seen:
            continue
        seen.add(key)
        picks.append(item)
        if len(picks) == 3:
            break
    return picks


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _extract_year(query: str) -> tuple:
    """Split a year out of a search query, returning (query, year).

    "Villan 2011" names a year, not a word of the title. Left in place it became
    a required title token, so every result was rejected and the search came
    back empty. OMDb rejects `s=Villan 2011` outright, so it is removed from
    the provider queries too and used to rank instead.
    """
    years = _YEAR_RE.findall(query)
    if not years:
        return " ".join(query.split()), ""
    return " ".join(_YEAR_RE.sub(" ", query).split()), years[-1]


def _catalogue_search(query: str, media_hint: str) -> list:
    """Ask OMDb and JustWatch for the same query at the same time.

    Neither provider knows the other exists, so running them one after the
    other made every search pay for the sum of their latencies — around 4.8s
    measured for an unhinted search. Concurrently the wait is the slower of the
    two. JustWatch is asked second so that a slow or failing provider cannot
    delay OMDb's rows, which are the ones carrying IMDb ids and posters.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        omdb_future = ex.submit(omdb_search, query, media_hint)
        jw_future = ex.submit(justwatch_search, query)
        results = []
        for fut in (omdb_future, jw_future):
            try:
                results.extend(fut.result(timeout=45))
            except Exception:
                continue
    return results


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def search(query: str) -> list:
    """Titles matching a query, best match first.

    Cached for six hours like the other lookups: re-submitting a title is
    common, and each miss otherwise costs several seconds of provider latency
    to arrive at the same answer.
    """
    query, media_hint, _ = _extract_hints(query)
    query, year_hint = _extract_year(query)
    query = query.strip()
    if not query:
        return []
    results = [
        item
        for item in _catalogue_search(query, media_hint)
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
    # A year the user typed outranks a better-spelled title from another year.
    kept.sort(
        key=lambda item: (
            0 if year_hint and item.get("year") == year_hint else 1,
            _title_score(query, item["title"]),
        )
    )
    if not _has_exact_title(kept, query):
        kept = _variant_pass(query, year_hint, media_hint, kept)
    if not _has_exact_title(kept, query):
        kept = _offer_youtube_only(query, year_hint, kept)
    return kept


def _variant_pass(query: str, year_hint: str, media_hint: str, kept: list) -> list:
    """Look the typed name up again under its common misspellings.

    A catalogue that spells a film one letter away from the user's memory hides
    it completely — OMDb's own search returns nothing for both "Villan" and
    "Villain", while the record exists. Asking with the neighbouring spellings
    finds it, and each hit is matched against the variant it was found under
    rather than the original query, because the original query is precisely the
    thing that did not match.

    Only run once the catalogues have reported a miss: every variant is another
    round trip, and a hit here may be a different film with a similar name, so
    the bar is an exact or near-exact title match, not the loose search filter.
    """
    extra = []
    seen = {_dedup_key(item) for item in kept}
    for variant in _title_variants(query):
        v_core = _yt_title_core(variant)
        for item in omdb_search(variant, media_hint) + _imdb_suggest(variant):
            if media_hint and item.get("media_type") != media_hint:
                continue
            if year_hint and item.get("year") and item["year"][:4] != year_hint[:4]:
                continue
            title_core = _yt_title_core(item.get("title") or "")
            if title_core != v_core and not _yt_title_cores_match(v_core, title_core):
                continue
            key = _dedup_key(item)
            if key in seen:
                continue
            if any(_fuzzy_same_movie(item, other) for other in [*kept, *extra]):
                continue
            seen.add(key)
            extra.append(item)
    if not extra:
        return kept
    extra.sort(
        key=lambda item: (
            0 if year_hint and item.get("year") == year_hint else 1,
            _title_score(query, item["title"]),
        )
    )
    return [*extra, *kept]


def _has_exact_title(items: list, query: str) -> bool:
    """True when the catalogue already has the film under the typed name."""
    wanted = _normalize_title(query)
    return any(_normalize_title(item["title"]) == wanted for item in items)


def _offer_youtube_only(query: str, year_hint: str, kept: list) -> list:
    """Offer a row for a film the streaming catalogues have no entry for.

    Some films are missing from both OMDb and JustWatch — searching "Villan"
    returned only "Villa Negra" and friends, with the Ajith Kumar film absent
    from every provider even though a studio upload of it exists. Rather than
    report nothing, probe YouTube and offer the title.

    The upload usually states the release year in its own title, and that year
    is often the only thing distinguishing the film from its namesakes, so it is
    used to identify the title through :func:`_resolve_by_imdb`. When that
    succeeds the row is a real catalogue entry with a poster and an IMDb link
    rather than a placeholder, and `omdb_details` fills it in normally. Only if
    the film cannot be identified is the row marked `youtube_only`, which makes
    `omdb_details` skip the OMDb lookup — a blind `t=` lookup would attach
    whatever unrelated title OMDb considers closest, which for "Villan" is a
    1920 silent film.
    """
    if not YOUTUBE_API_KEY or len(query) < 3:
        return kept
    try:
        found = youtube_full_movie(
            query, year_hint, "movie", "", 0, False, _CHANNEL_AUDIT_VERSION, True,
        )
    except Exception:
        return kept
    if not found:
        return kept
    row_year = year_hint or _imdb_year_from_uploads(found)
    row_title = query.title() if query.islower() else query
    identified = _resolve_by_imdb(row_title, row_year, "movie")
    if identified:
        return [identified, *kept]
    return [
        {
            "id": "",
            "media_type": "movie",
            "title": row_title,
            "year": row_year,
            "poster": None,
            "link": "",
            "youtube_only": True,
        },
        *kept,
    ]


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
        f'letter-spacing:.04em;text-transform:uppercase">{_escape(label)}</span>'
    )


# Where a YouTube upload came from, as a visible chip. A broadcaster's TV channel
# (Jaya TV, Kalaingar TV, Sun TV) is a rebroadcast of a film, which is a
# different thing from the studio's own upload (Sun Pictures, AVM Productions)
# even though both pass the runtime check — so the two are labelled apart.
_TIER_BADGES = {
    "tv": ("TV Channel", "#16a085"),
    "studio": ("Official Studio", "#6ea8ff"),
    "movie_official": ("Official Label", "#8e6ee0"),
}

# Shown on full-movie results, which only reach the list after passing the OMDb
# runtime comparison within YOUTUBE_RUNTIME_TOLERANCE.
_RUNTIME_BADGE = ("Runtime ✓", "#16a085")

# OMDb had no record for the title, so the length was never cross-checked. The
# upload is still a full-length, correctly-titled video — just not confirmed.
_RUNTIME_UNKNOWN_BADGE = ("Runtime unverified", "#8c95a8")

# A result from a channel that isn't on the reviewed list. Deliberately drab:
# the point is that it reads as weaker than a broadcaster or studio badge.
_UNTRUSTED_BADGE = ("Unverified Channel", "#8c95a8")


def _tier_badge(tier: str) -> str:
    """Chip naming the source of a YouTube upload; '' for untrusted channels."""
    entry = _TIER_BADGES.get(tier or "")
    return _chip(*entry) if entry else ""


def _trust_badges(tier: str, runtime_verified: bool | None = True) -> str:
    """Chips describing where a YouTube upload came from and how it was checked.

    Every result is labelled, including the unverified ones — when results from
    unreviewed channels are allowed through, the user has to be able to see
    that at a glance instead of assuming a broadcaster posted it.

    `runtime_verified` is None for series playlists, which are checked by
    episode count rather than length and so get no runtime chip at all.
    """
    chips = [_tier_badge(tier) or _chip(*_UNTRUSTED_BADGE)]
    if runtime_verified is True:
        chips.append(_chip(*_RUNTIME_BADGE))
    elif runtime_verified is False:
        chips.append(_chip(*_RUNTIME_UNKNOWN_BADGE))
    return " ".join(chip for chip in chips if chip)


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
                # Resolve the title before the session copy is taken: a row that
                # started life as a YouTube-only placeholder is given its real
                # IMDb id, year, poster and link here, and everything below
                # needs to see the enriched values.
                details, omdb_error, providers = _resolve_selection(item)
                st.session_state["selected"] = {**item, "season": season}
                st.session_state["providers"] = providers
                st.session_state["details"] = details
                omdb_status = _omdb_status(details, omdb_error)
                st.session_state["omdb_status"] = omdb_status
                st.session_state["omdb_error"] = omdb_error
                _load_youtube_for_selected(item, details, omdb_status)


def _resolve_selection(item: dict) -> tuple:
    """Fetch a row's details and its watch providers.

    For an ordinary catalogue row the two are independent: OMDb supplies plot
    and ratings, JustWatch supplies offers, and neither consults the other.
    They are fetched together so a click costs the slower of the two rather
    than their sum.

    A YouTube-only row is the exception, because identifying it rewrites its
    id, title and year in place — the fields JustWatch matches on. Resolving it
    first, then asking for providers, is what lets a title no catalogue carries
    still come back with the right offers instead of none.
    """
    if item.get("youtube_only"):
        details, error = omdb_details(item)
        return details, error, get_providers(item)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        detail_future = ex.submit(omdb_details, item)
        provider_future = ex.submit(get_providers, item)
        try:
            details, error = detail_future.result(timeout=45)
        except Exception as exc:
            details, error = {}, str(exc)
        try:
            providers = provider_future.result(timeout=45)
        except Exception:
            providers = dict(EMPTY_PROVIDERS)
    return details, error, providers


def _load_youtube_for_selected(
    selected: dict, details: dict, omdb_status: str
) -> None:
    """Run the YouTube lookup for the current selection and stash the results.

    Shared by the initial "Select" click and by the unverified-channels toggle,
    which has to re-run the lookup because `include_unofficial` is part of the
    result cache key.

    A missing OMDb runtime is not treated as a blocker: many regional and older
    titles are absent from OMDb entirely, and reporting no YouTube results at
    all for those hides films that really are available. The lookup runs with
    `expected_minutes=0`, which drops the runtime comparison and flags the
    results as unverified instead.
    """
    user_lang = st.session_state.get("yt_lang") or ""
    if selected.get("media_type") != "movie":
        st.session_state.pop("yt_auto_lang", None)
        st.session_state.pop("yt_results", None)
        return
    orig_lang = _omdb_original_lang(details)
    eff_lang = user_lang or orig_lang
    if user_lang or not orig_lang:
        st.session_state.pop("yt_auto_lang", None)
    else:
        st.session_state["yt_auto_lang"] = next(
            (k for k, (iso, _m) in _LANGUAGE_HINTS.items() if iso == orig_lang), ""
        )
    st.session_state["yt_results"] = youtube_full_movie(
        selected["title"], selected.get("year") or "", selected["media_type"],
        eff_lang, _omdb_runtime_minutes(details), bool(user_lang),
        _CHANNEL_AUDIT_VERSION,
        bool(st.session_state.get("yt_include_unofficial", True)),
    )


def main():
    st.set_page_config(page_title="Entertainment Finder", layout="centered")

    st.title("Entertainment Finder")
    st.caption(
        "Search any movie, TV series, or anime and find out where it's streaming in India. "
        "Add a language to prioritize it on YouTube (e.g. \"One Piece (Mention Movie or Series after movie name) in Japanese\")."
    )

    # Trusted channels are the only ones allowed by default, which hides the
    # majority of regional and non-English films — the sole legitimate upload of
    # a Tamil or Kannada film is often on a channel nobody has reviewed. The
    # toggle is on by default and the unverified results are badged, so widening
    # the net doesn't silently pass unvetted links off as official.
    st.session_state["yt_include_unofficial"] = st.checkbox(
        "Include uploads from unverified channels",
        value=True,
        help=(
            "On: any channel counts, as long as the video names the film and its "
            "length matches OMDb's runtime. Those results are badged "
            "\"Unverified channel\". Off: only official broadcasters, studios and "
            "movie labels."
        ),
    )
    if st.session_state["yt_include_unofficial"] != st.session_state.get(
        "_yt_unofficial_seen"
    ):
        st.session_state["_yt_unofficial_seen"] = st.session_state[
            "yt_include_unofficial"
        ]
        # The visible results were built with the other setting, so re-run the
        # lookup for the open title instead of showing a stale list.
        if st.session_state.get("selected"):
            with st.spinner("Rechecking YouTube matches..."):
                _load_youtube_for_selected(
                    st.session_state["selected"],
                    st.session_state.get("details") or {},
                    st.session_state.get("omdb_status", "unavailable"),
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
            # An exact match needs no correction. Otherwise prefer scoring the
            # rows already in hand over a second, slower sweep of the
            # catalogues for something closer.
            exact = _has_exact_title(results, query)
            suggestions = [] if exact else _correction_candidates(query, results)
            if not suggestions and not exact:
                suggestions = fuzzy_suggest(query)
        st.session_state["results"] = results
        st.session_state["suggestions"] = suggestions
        st.session_state["hint_label"] = hint_label
        st.session_state["yt_lang"] = lang
        st.session_state.pop("selected", None)
        st.session_state.pop("providers", None)
        st.session_state.pop("details", None)
        st.session_state.pop("yt_results", None)
        st.session_state.pop("omdb_status", None)
        st.session_state.pop("omdb_error", None)
        st.session_state.pop("yt_status", None)
        if not results and not suggestions:
            if hint_label:
                type_name = "Movie" if hint_label == "movie" else "TV series"
                st.info(
                    f"No {type_name} results found for that title. Try a different spelling."
                )
            else:
                st.info("No results found for that title. Try a different spelling.")

    results = st.session_state.get("results", [])
    suggestions = st.session_state.get("suggestions", [])
    # A correction and a search result are the same film, so the suggested rows
    # are pulled out of the results list rather than rendered twice.
    suggested = {_dedup_key(item) for item in suggestions}
    others = [item for item in results if _dedup_key(item) not in suggested]
    if suggestions:
        st.subheader("Did you mean?")
        if results:
            st.caption(
                "Nothing here is an exact match for that spelling. "
                "Did you mean one of these?"
            )
        else:
            st.caption("No exact match found. Did you mean one of these?")
        _render_result_cards(suggestions)
    if others:
        st.subheader("Results" if not suggestions else "Other results")
        hint_label = st.session_state.get("hint_label")
        if hint_label:
            type_name = "Movie" if hint_label == "movie" else "TV series"
            st.caption(f"Showing {type_name} results only")
        _render_result_cards(others)

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
        omdb_status = st.session_state.get("omdb_status", "unavailable")
        omdb_error = st.session_state.get("omdb_error", "")
        if not details:
            # A title OMDb simply doesn't carry shouldn't look like a fault.
            if omdb_status in _OMDB_BENIGN_STATES:
                st.info(_omdb_status_message(omdb_status))
            else:
                st.warning(_omdb_status_message(omdb_status))
            if omdb_error:
                st.caption(f"OMDb reported: {omdb_error}")
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
        if YOUTUBE_API_KEY and not is_series:
            st.divider()
            yt_lang = st.session_state.get("yt_lang") or ""
            lang_name = next(
                (k for k, (iso, m) in _LANGUAGE_HINTS.items() if iso == yt_lang), ""
            )
            lang_note = f" · {lang_name.title()} prioritized" if lang_name else ""
            auto_name = st.session_state.get("yt_auto_lang") or ""
            auto_note = f" · Original language ({auto_name.title()}) prioritized" if auto_name else ""
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
                            # Playlists are checked by episode count, so they
                            # get the source badge but no runtime chip.
                            badge = _tier_badge(r.get("official_tier", ""))
                            st.markdown(
                                f"{badge + chr(10) if badge else ''}**{_escape(r['title'])}**  \n"
                                f"{_escape(r['channel'])} · {r.get('item_count')} episodes{audio_chip}",
                                unsafe_allow_html=True,
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
                        f"Full-series playlist not available on YouTube in {lang_name.title()} right now."
                        if lang_name
                        else "Full-series playlist not available on YouTube right now."
                    )
            else:
                st.subheader("Full Movie on YouTube")
                if yt_results:
                    include_unofficial = st.session_state.get(
                        "yt_include_unofficial", True
                    )
                    source_note = (
                        "Any channel, each video badged by source."
                        if include_unofficial
                        else "Official broadcasters, studios and movie labels only."
                    )
                    runtime_note = (
                        "lengths checked against OMDb's runtime"
                        if omdb_status == "ready"
                        else "lengths not cross-checked — OMDb has no runtime for this title"
                    )
                    st.caption(
                        f"Full-length uploads ({runtime_note}) — {source_note}"
                        f"{lang_note}{auto_note} Pick one and play."
                    )
                    if not _CHANNEL_AUDIT_ENFORCED:
                        st.caption("Channel source is matched by name; the reviewed channel audit isn't applied yet.")
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
                            audio_chip = f" · {_escape(aw)} audio" if al else ""
                            st.markdown(
                                f"{_trust_badges(r.get('official_tier', ''), r.get('runtime_verified'))}\n"
                                f"**{_escape(r['title'])}**  \n"
                                f"{_escape(r['channel'])} · {r['duration']} min{audio_chip}",
                                unsafe_allow_html=True,
                            )
                        with ycols[2]:
                            st.link_button(
                                "Play",
                                _safe_url(r["link"]),
                                use_container_width=True,
                                type="primary",
                            )
                else:
                    # OMDb being unusable no longer stops the search, so the
                    # only reason for an empty list is a genuine API failure or
                    # nothing qualifying. Surface both accurately.
                    if (yt_status := st.session_state.get("yt_status", "")) in _YT_FAILURE_STATES:
                        st.warning(_yt_status_message(yt_status))
                    elif omdb_status != "ready":
                        st.info(
                            f"No full movie found on YouTube. {_omdb_status_message(omdb_status)}"
                        )
                    else:
                        st.info(
                            f"No {lang_name.title()} version found on YouTube right now."
                            if lang_name
                            else (
                                "No full movie found on YouTube, official channels only — "
                                "switch on \"Include uploads from unverified channels\" to "
                                "search all channels."
                                if not st.session_state.get("yt_include_unofficial", True)
                                else "No full movie found on YouTube, even across "
                                "unverified channels."
                            )
                        )
                    if omdb_error and omdb_status != "ready":
                        st.caption(f"OMDb reported: {omdb_error}")

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