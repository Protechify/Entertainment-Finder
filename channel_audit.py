import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any

import httpx

import app


YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SEARCH_URL = f"{YOUTUBE_API_BASE}/search"
YOUTUBE_CHANNELS_URL = f"{YOUTUBE_API_BASE}/channels"
YOUTUBE_PLAYLIST_ITEMS_URL = f"{YOUTUBE_API_BASE}/playlistItems"
YOUTUBE_VIDEOS_URL = f"{YOUTUBE_API_BASE}/videos"
OMDB_URL = "https://www.omdbapi.com/"
MIN_MOVIE_MINUTES = 90
RUNTIME_TOLERANCE = app.YOUTUBE_RUNTIME_TOLERANCE
REPORT_SCHEMA_VERSION = 1


class AuditError(RuntimeError):
    pass


class AuditLimitReached(AuditError):
    pass


class AuditQuotaReached(AuditError):
    pass


def _safe_error_message(value: object, limit: int = 240) -> str:
    text = re.sub(r"https?://\S+", "", str(value or ""))
    for secret in (app.YOUTUBE_API_KEY, app.OMDB_API_KEY):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = " ".join(text.split())
    return text[:limit] or "unknown error"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _youtube_request(client: httpx.Client, url: str, params: dict[str, Any]) -> dict:
    try:
        response = client.get(url, params={**params, "key": app.YOUTUBE_API_KEY})
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        error_type = AuditQuotaReached if exc.response.status_code == 429 else AuditError
        raise error_type(
            f"YouTube request failed with HTTP {exc.response.status_code}"
        ) from None
    except (httpx.HTTPError, ValueError):
        raise AuditError("YouTube request failed") from None
    if not isinstance(data, dict):
        raise AuditError("YouTube returned an invalid response")
    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AuditError(f"YouTube API error: {_safe_error_message(message)}")
    return data


def _omdb_request(client: httpx.Client, title: str, year: str = "") -> dict | None:
    if not app.OMDB_API_KEY:
        return None
    params = {
        "apikey": app.OMDB_API_KEY,
        "type": "movie",
        "t": title,
        "plot": "short",
    }
    if year:
        params["y"] = year
    try:
        response = client.get(OMDB_URL, params=params)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        error_type = AuditQuotaReached if exc.response.status_code == 429 else AuditError
        raise error_type(
            f"OMDb request failed with HTTP {exc.response.status_code}"
        ) from None
    except (httpx.HTTPError, ValueError):
        raise AuditError("OMDb request failed") from None
    if not isinstance(data, dict) or data.get("Response") != "True":
        return None
    return data


def _seed_entries() -> list[dict[str, Any]]:
    sources = (
        ("tv", app._OFFICIAL_TV_CHANNELS),
        ("studio", app._STUDIO_CHANNELS),
        ("movie_official", app._OFFICIAL_MOVIE_CHANNELS),
    )
    entries: dict[str, dict[str, Any]] = {}
    for tier, groups in sources:
        for language, names in groups.items():
            for name in names:
                key = app._normalize_title(name)
                entry = entries.setdefault(
                    key,
                    {"name": name, "tiers": [], "languages": []},
                )
                if tier not in entry["tiers"]:
                    entry["tiers"].append(tier)
                if language not in entry["languages"]:
                    entry["languages"].append(language)
    result = []
    for entry in entries.values():
        tiers = entry["tiers"]
        if "movie_official" in tiers:
            tier = "movie_official"
        elif "studio" in tiers:
            tier = "studio"
        else:
            tier = "tv"
        result.append({**entry, "tier": tier})
    return sorted(result, key=lambda item: item["name"].casefold())


def _title_candidates(video_title: str) -> list[str]:
    chunks = re.split(r"[|,()\[\]:;_~*\-–—/]+", video_title or "")
    candidates: list[str] = []
    for chunk in chunks:
        words = re.findall(r"[a-z0-9]+", chunk.lower())
        core_words = [
            word
            for word in words
            if word not in app._YT_TITLE_MARKERS
            and not re.fullmatch(r"19\d\d|20\d\d", word)
        ]
        if core_words:
            candidate = " ".join(core_words)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates[:3]


def _extract_year(text: str) -> str:
    match = re.search(r"\b(19\d\d|20\d\d)\b", text or "")
    return match.group(1) if match else ""


def _candidate_score(candidate: dict, seed_name: str) -> int:
    title = str((candidate.get("snippet") or {}).get("title") or "")
    candidate_title = app._normalize_title(title)
    seed_title = app._normalize_title(seed_name)
    if candidate_title == seed_title:
        return 100
    seed_tokens = set(app._channel_tokens(seed_name))
    candidate_tokens = set(app._channel_tokens(title))
    if seed_tokens and seed_tokens.issubset(candidate_tokens):
        return 50
    return 0


def _previous_channel_record(previous: dict, channel_id: str) -> dict:
    channels = previous.get("channels")
    if not isinstance(channels, dict):
        return {}
    record = channels.get(channel_id)
    return record if isinstance(record, dict) else {}


def _previous_channel_id(previous: dict, seed_name: str) -> str:
    seed_title = app._normalize_title(seed_name)
    channels = previous.get("channels")
    if not isinstance(channels, dict):
        return ""
    for record in channels.values():
        if not isinstance(record, dict):
            continue
        names = record.get("seed_names") or []
        if any(app._normalize_title(name) == seed_title for name in names):
            channel_id = str(record.get("channel_id") or "")
            if channel_id.startswith("UC"):
                return channel_id
    return ""


def _resolve_channel(client: httpx.Client, seed: dict, previous: dict) -> tuple[str, list[dict]]:
    previous_id = _previous_channel_id(previous, seed["name"])
    if previous_id:
        return previous_id, []
    data = _youtube_request(
        client,
        YOUTUBE_SEARCH_URL,
        {
            "part": "snippet",
            "type": "channel",
            "q": seed["name"],
            "maxResults": 10,
        },
    )
    candidates = [
        item
        for item in data.get("items", [])
        if item.get("id", {}).get("channelId")
    ]
    scored = [
        (_candidate_score(item, seed["name"]), item)
        for item in candidates
    ]
    best_score = max((score for score, _ in scored), default=0)
    if best_score < 50:
        return "", candidates
    best = [item for score, item in scored if score == best_score]
    if len(best) != 1:
        return "", best
    channel_id = str(best[0]["id"]["channelId"])
    return (channel_id, best) if channel_id.startswith("UC") else ("", best)


def _channel_metadata(client: httpx.Client, channel_id: str) -> dict:
    data = _youtube_request(
        client,
        YOUTUBE_CHANNELS_URL,
        {
            "part": "snippet,contentDetails,status",
            "id": channel_id,
        },
    )
    items = data.get("items", [])
    if not items:
        raise AuditError(f"Channel not found: {channel_id}")
    item = items[0]
    if item.get("id") != channel_id:
        raise AuditError("Channel metadata ID mismatch")
    snippet = item.get("snippet", {}) or {}
    details = item.get("contentDetails", {}) or {}
    return {
        "title": snippet.get("title") or "",
        "handle": snippet.get("customUrl") or "",
        "country": snippet.get("country") or "",
        "description": snippet.get("description") or "",
        "uploads_playlist": (details.get("relatedPlaylists") or {}).get("uploads") or "",
    }


def _video_batches(client: httpx.Client, video_ids: list[str]) -> list[dict]:
    videos = []
    for start in range(0, len(video_ids), 50):
        data = _youtube_request(
            client,
            YOUTUBE_VIDEOS_URL,
            {
                "part": "snippet,contentDetails,status",
                "id": ",".join(video_ids[start : start + 50]),
            },
        )
        videos.extend(data.get("items", []))
    return videos


def _upload_batches(
    client: httpx.Client,
    playlist_id: str,
    max_pages: int,
    state: dict[str, Any],
):
    page_token = ""
    pages = 0
    while True:
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _youtube_request(client, YOUTUBE_PLAYLIST_ITEMS_URL, params)
        ids = []
        for item in data.get("items", []):
            video_id = (item.get("contentDetails") or {}).get("videoId")
            if not video_id:
                video_id = (item.get("snippet") or {}).get("resourceId", {}).get("videoId")
            if video_id:
                ids.append(video_id)
        pages += 1
        state["pages"] = pages
        if ids:
            yield ids
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            state["complete"] = True
            return
        if max_pages and pages >= max_pages:
            state["complete"] = False
            return


def _video_record(
    client: httpx.Client,
    video: dict,
    seed: dict,
    omdb_cache: dict[tuple[str, str], dict | None],
    omdb_state: dict[str, int],
    max_omdb_lookups: int,
) -> dict | None:
    snippet = video.get("snippet", {}) or {}
    content_details = video.get("contentDetails", {}) or {}
    status = video.get("status", {}) or {}
    video_id = video.get("id") or ""
    title = html.unescape(snippet.get("title") or "")
    channel = html.unescape(snippet.get("channelTitle") or "")
    if not video_id or snippet.get("channelId") != seed.get("channel_id"):
        return None
    if status.get("privacyStatus") != "public" or not status.get("embeddable"):
        return None
    if status.get("uploadStatus") and status.get("uploadStatus") != "processed":
        return None
    duration = app._youtube_duration_minutes(content_details.get("duration", ""))
    if duration < MIN_MOVIE_MINUTES:
        return None
    if any(word in title.lower() for word in app._YOUTUBE_BAD_WORDS):
        return None
    if app._looks_like_reaction(channel, title, snippet.get("description") or ""):
        return None
    if app._looks_pirated(channel, title):
        return None
    audio = app._normalize_audio_lang(snippet.get("defaultAudioLanguage"))
    if audio and seed.get("languages") and audio not in seed["languages"]:
        return None
    year = _extract_year(title)
    for candidate in _title_candidates(title):
        key = (candidate, year)
        if key not in omdb_cache:
            if max_omdb_lookups and omdb_state["lookups"] >= max_omdb_lookups:
                raise AuditLimitReached("OMDb lookup limit reached")
            omdb_state["lookups"] += 1
            omdb_cache[key] = _omdb_request(client, candidate, year)
        omdb_details = omdb_cache[key]
        if not omdb_details:
            continue
        omdb_title = str(omdb_details.get("Title") or "")
        if not omdb_title or not app._yt_title_exact_match(title, omdb_title):
            continue
        if year and str(omdb_details.get("Year") or "")[:4] != year:
            continue
        omdb_minutes = app._omdb_runtime_minutes(omdb_details)
        if omdb_minutes <= 0 or not app._youtube_runtime_matches(duration, omdb_minutes):
            continue
        return {
            "video_id": video_id,
            "title": title,
            "channel_title": channel,
            "audio_language": audio,
            "youtube_minutes": duration,
            "omdb_id": omdb_details.get("imdbID") or "",
            "omdb_title": omdb_title,
            "omdb_year": str(omdb_details.get("Year") or "")[:4],
            "omdb_minutes": omdb_minutes,
            "link": f"https://www.youtube.com/watch?v={video_id}",
        }
    return None


def _tier_rank(tier: object) -> int:
    return {"tv": 1, "studio": 2, "movie_official": 3}.get(tier, 0)


def _merge_record(existing: dict, record: dict) -> dict:
    if not existing:
        return record
    existing_rank = _tier_rank(existing.get("tier"))
    record_rank = _tier_rank(record.get("tier"))
    if record_rank > existing_rank:
        base = record
    elif existing_rank > record_rank:
        base = existing
    elif existing.get("status") == "qualified" and record.get("status") != "qualified":
        base = existing
    else:
        base = record if record.get("status") == "qualified" else existing
    merged = dict(base)
    merged["seed_names"] = sorted(
        set(existing.get("seed_names", [])) | set(record.get("seed_names", []))
    )
    merged["languages"] = sorted(
        set(existing.get("languages", [])) | set(record.get("languages", []))
    )
    qualifying = []
    seen = set()
    for item in list(existing.get("qualifying_videos", [])) + list(record.get("qualifying_videos", [])):
        video_id = item.get("video_id") if isinstance(item, dict) else ""
        if video_id and video_id not in seen:
            seen.add(video_id)
            qualifying.append(item)
    merged["qualifying_videos"] = qualifying
    merged["reviewed"] = bool(existing.get("reviewed") or record.get("reviewed"))
    merged["official_source"] = str(
        existing.get("official_source") or record.get("official_source") or ""
    )
    merged["content_check"] = (
        "movie_runtime"
        if max(existing_rank, record_rank) == 3
        else base.get("content_check", "channel_identity")
    )
    return merged


def _audit_channel(
    client: httpx.Client,
    seed: dict,
    metadata: dict,
    max_pages: int,
    max_omdb_lookups: int,
    max_matches: int,
    omdb_cache: dict[tuple[str, str], dict | None],
    omdb_state: dict[str, int],
) -> dict:
    seed_with_id = {**seed, "channel_id": metadata["channel_id"]}
    requires_movie_match = seed["tier"] == "movie_official"
    record = {
        "channel_id": metadata["channel_id"],
        "seed_names": [seed["name"]],
        "tier": seed["tier"],
        "languages": seed["languages"],
        "title": metadata["title"],
        "handle": metadata.get("handle", ""),
        "url": f"https://www.youtube.com/channel/{metadata['channel_id']}",
        "status": "no_match",
        "last_audited": _utc_now(),
        "pages": 0,
        "scan_complete": False,
        "videos_seen": 0,
        "qualifying_videos": [],
        "content_check": "movie_runtime" if requires_movie_match else "channel_identity",
        "identity_basis": "curated_seed_name",
        "reviewed": False,
        "official_source": "",
    }
    if not metadata.get("uploads_playlist"):
        record["status"] = "unknown"
        record["error"] = "Channel has no uploads playlist"
        return record
    scan_state = {"pages": 0, "complete": False, "partial": False}
    effective_max_pages = max_pages if requires_movie_match else 1
    batches = _upload_batches(
        client,
        metadata["uploads_playlist"],
        effective_max_pages,
        scan_state,
    )
    try:
        for ids in batches:
            record["videos_seen"] += len(ids)
            if not requires_movie_match:
                scan_state["complete"] = True
                break
            for video in _video_batches(client, ids):
                match = _video_record(
                    client,
                    video,
                    seed_with_id,
                    omdb_cache,
                    omdb_state,
                    max_omdb_lookups,
                )
                if match:
                    record["qualifying_videos"].append(match)
                    if len(record["qualifying_videos"]) >= max_matches:
                        record["status"] = "qualified"
                        scan_state["partial"] = True
                        break
            if record["status"] == "qualified":
                break
    except AuditQuotaReached:
        raise
    except AuditLimitReached as exc:
        record["status"] = "unknown"
        record["error"] = str(exc)
    except AuditError as exc:
        record["status"] = "unknown"
        record["error"] = str(exc)
    finally:
        batches.close()
    if not requires_movie_match and scan_state["pages"]:
        scan_state["complete"] = True
    record["pages"] = scan_state["pages"]
    record["scan_complete"] = scan_state["complete"]
    if not requires_movie_match and scan_state["complete"]:
        record["status"] = "qualified"
    if record["status"] != "qualified" and not scan_state["complete"]:
        record["status"] = "unknown"
        record.setdefault("error", "Upload scan was incomplete")
    return record


def _load_previous(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as report_file:
            data = json.load(report_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _waiver_key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _waived_names(previous: dict) -> set[str]:
    values = previous.get("waived", [])
    if not isinstance(values, list):
        return set()
    return {_waiver_key(value) for value in values if _waiver_key(value)}


def _report_is_enforceable(
    channels: dict[str, dict],
    unresolved: list[dict],
    errors: list[str],
    waived: set[str],
) -> bool:
    if (
        not isinstance(channels, dict)
        or not isinstance(unresolved, list)
        or not isinstance(errors, list)
        or not isinstance(waived, set)
        or any(not isinstance(record, dict) for record in channels.values())
    ):
        return False
    unresolved_is_waived = all(
        isinstance(item, dict) and _waiver_key(item.get("name")) in waived
        for item in unresolved
    )
    return unresolved_is_waived and not errors and bool(channels) and all(
        record.get("status") == "qualified"
        and record.get("reviewed") is True
        and bool(str(record.get("official_source") or "").strip())
        for record in channels.values()
    )


def audit(
    output: Path,
    max_pages: int = 0,
    max_omdb_lookups: int = 900,
    max_matches: int = 3,
) -> dict:
    if not app.YOUTUBE_API_KEY:
        raise AuditError("YOUTUBE_API_KEY is not configured")
    max_pages = max(0, max_pages)
    max_omdb_lookups = max(0, max_omdb_lookups)
    max_matches = max(1, max_matches)
    previous = _load_previous(output)
    seeds = _seed_entries()
    channels: dict[str, dict] = {}
    unresolved: list[dict] = []
    errors: list[str] = []
    waived = _waived_names(previous)
    omdb_cache: dict[tuple[str, str], dict | None] = {}
    omdb_state = {"lookups": 0}
    quota_exhausted = False
    with httpx.Client(timeout=30) as client:
        for seed in seeds:
            try:
                channel_id, candidates = _resolve_channel(client, seed, previous)
                if not channel_id:
                    unresolved.append({
                        "name": seed["name"],
                        "reason": "ambiguous_or_missing",
                        "candidate_ids": [
                            item.get("id", {}).get("channelId", "")
                            for item in candidates
                        ],
                    })
                    continue
                metadata = _channel_metadata(client, channel_id)
                if _candidate_score(
                    {"snippet": {"title": metadata.get("title", "")}},
                    seed["name"],
                ) < 50:
                    channel_id, candidates = _resolve_channel(client, seed, {})
                    if not channel_id:
                        unresolved.append({
                            "name": seed["name"],
                            "reason": "stale_id_or_unresolved",
                            "candidate_ids": [
                                item.get("id", {}).get("channelId", "")
                                for item in candidates
                            ],
                        })
                        continue
                    metadata = _channel_metadata(client, channel_id)
                metadata["channel_id"] = channel_id
                record = _audit_channel(
                    client,
                    seed,
                    metadata,
                    max_pages,
                    max_omdb_lookups,
                    max_matches,
                    omdb_cache,
                    omdb_state,
                )
                previous_record = _previous_channel_record(previous, channel_id)
                record["reviewed"] = bool(previous_record.get("reviewed"))
                record["official_source"] = str(
                    previous_record.get("official_source") or ""
                )
                channels[channel_id] = _merge_record(channels.get(channel_id, {}), record)
            except AuditQuotaReached as exc:
                errors.append(f"{seed['name']}: {_safe_error_message(exc)}")
                quota_exhausted = True
                break
            except AuditError as exc:
                errors.append(f"{seed['name']}: {_safe_error_message(exc)}")
            except Exception:
                errors.append(f"{seed['name']}: unexpected audit failure")
    complete = _report_is_enforceable(channels, unresolved, errors, waived)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "enforced": complete,
        "review_required": not complete,
        "generated_at": _utc_now(),
        "policy": {
            "literal_title_match": True,
            "runtime_tolerance": RUNTIME_TOLERANCE,
            "minimum_movie_minutes": MIN_MOVIE_MINUTES,
            "movie_content_tier": "movie_official",
            "scopes": ["tv", "studio", "movie_official"],
        },
        "channels": channels,
        "unresolved": unresolved,
        "errors": errors,
        "waived": sorted(waived),
        "omdb_lookups": omdb_state["lookups"],
        "quota_exhausted": quota_exhausted,
        "omdb_limit_reached": bool(
            max_omdb_lookups and omdb_state["lookups"] >= max_omdb_lookups
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)
        report_file.write("\n")
        report_file.flush()
    temporary.replace(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("channel_audit.json"))
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--max-omdb-lookups", type=int, default=900)
    parser.add_argument("--max-matches", type=int, default=3)
    args = parser.parse_args()
    report = audit(
        args.output,
        max_pages=max(0, args.max_pages),
        max_omdb_lookups=max(0, args.max_omdb_lookups),
        max_matches=max(1, args.max_matches),
    )
    counts: dict[str, int] = {}
    for record in report.get("channels", {}).values():
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({
        "output": str(args.output),
        "enforced": report.get("enforced"),
        "channels": counts,
        "unresolved": len(report.get("unresolved", [])),
        "errors": len(report.get("errors", [])),
        "quota_exhausted": report.get("quota_exhausted", False),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
