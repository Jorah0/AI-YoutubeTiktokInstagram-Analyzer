"""Data broker intake: public video data for TikTok, Instagram and YouTube through Apify scrapers.

What it does: run one scraper ("actor") for a theme search or a channel, read the items into one
shape, and store them as creators, posts and metric readings in a per-brief catalog. Nothing here is
called unless you run a scrape (the Video tracker screen or the `scrape` command), and each scrape
costs broker credits.

Things to know before relying on it:
- Scraping these sites through a broker is outside the platforms' official APIs. Whether your use is
  permitted is between you, the broker and each platform's terms; the brief's first blueprint
  flagged this. The source is recorded as "broker" so everything it supplied can be removed with
  `revoke broker`.
- Scraper output formats change. Items that do not look like a video on the expected platform are
  dropped, never guessed at, and the raw answer is kept for 30 days so a parsing fix does not cost a
  second scrape.
"""
import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import timezone
from pathlib import Path

import httpx

from . import db as store
from .adapters import AccessDenied, SourceError, _int
from .metrics import parse_time
from .urls import parse_url

API = "https://api.apify.com/v2/actors/{actor}/run-sync-get-dataset-items"
ACTORS = {  # swap a scraper without touching code: CI_APIFY_TIKTOK=owner~actor-name
    "tiktok": os.environ.get("CI_APIFY_TIKTOK", "clockworks~tiktok-scraper"),
    "instagram": os.environ.get("CI_APIFY_INSTAGRAM", "apify~instagram-scraper"),
    "youtube": os.environ.get("CI_APIFY_YOUTUBE", "streamers~youtube-scraper"),
}
MAX_ITEMS = 100  # per scrape; a ceiling on spend
RAW_DAYS = 30
ORIGINS = ("own", "theme", "peer")
SHORT_MAX_SEC = 180


class NoToken(Exception):
    """APIFY_TOKEN is not set, so no scrape can run."""


# --- asking --------------------------------------------------------------------------------------------

def actor_input(platform: str, search: str | None = None, channel: str | None = None, limit: int = 20) -> dict:
    """The scraper's own input fields for a theme search or one channel (exactly one of the two)."""
    if platform not in ACTORS:
        raise ValueError(f"unknown platform: {platform}")
    search = (search or "").strip()
    if bool(search) == bool(channel):
        raise ValueError("give either a search or a channel link")
    n = max(1, min(int(limit), MAX_ITEMS))
    if channel:
        ref = parse_url(channel)
        if ref.platform != platform or ref.kind != "account":
            raise ValueError(f"not a {platform} channel link: {channel}")
    if platform == "tiktok":
        return {**({"searchQueries": [search]} if search else {"profiles": [ref.external_id.lstrip("@")]}),
                "resultsPerPage": n}
    if platform == "instagram":
        tag = re.sub(r"\W", "", search.lower())
        if search and not tag:
            raise ValueError("the search has no letters or digits")
        url = f"https://www.instagram.com/explore/tags/{tag}/" if search else ref.url
        return {"directUrls": [url], "resultsType": "posts", "resultsLimit": n}
    return {**({"searchQueries": [search]} if search else {"startUrls": [{"url": ref.url}]}),
            "maxResults": n, "maxResultsShorts": n}


def run_actor(actor: str, payload: dict, token: str, client=None) -> list:
    """Run a scraper and wait for its items (Apify allows five minutes). The token travels in a header,
    never in the address, and never appears in an error."""
    if not re.fullmatch(r"[\w.-]+~[\w.-]+", actor):
        raise ValueError(f"not an Apify actor id: {actor}")
    try:
        r = (client or httpx).post(API.format(actor=actor), json=payload, timeout=330,
                                   params={"clean": "true", "format": "json"},
                                   headers={"Authorization": f"Bearer {token}"})
    except httpx.TransportError as exc:
        raise SourceError(f"api.apify.com could not be reached ({type(exc).__name__})") from None
    if r.status_code in (401, 403):
        raise AccessDenied(f"api.apify.com refused access ({r.status_code})")
    if r.status_code == 408:
        raise SourceError("the scrape ran longer than five minutes; ask for fewer results")
    if r.status_code >= 400:
        raise SourceError(f"api.apify.com answered {r.status_code}")
    try:
        items = r.json()
    except ValueError:
        items = None
    if not isinstance(items, list):
        raise SourceError("api.apify.com did not send a list of items")
    return items


# --- reading -----------------------------------------------------------------------------------------------

def _get(item: dict, *paths: str):
    for path in paths:
        value = item
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value not in (None, ""):
            return value
    return None


def _when(value) -> str | None:
    """A real timestamp in UTC, or None. Scrapers also send things like "10 months ago": not a date."""
    try:
        return parse_time(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _seconds(value) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 172_800:  # two days at most
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\d{1,3}(:\d{2}){1,2}", value):
        total = 0
        for part in value.split(":"):
            total = total * 60 + int(part)
        return total
    return None


def normalize(platform: str, item) -> dict | None:
    """One scraped item in our terms, or None when it is not a video on that platform. Every link is
    rebuilt through parse_url, so nothing a scraper sends can become an arbitrary stored URL."""
    if not isinstance(item, dict) or item.get("errorCode"):
        return None
    author = {"tiktok": "authorMeta.name", "instagram": "ownerUsername", "youtube": "channelUrl"}.get(platform)
    if not author or not _get(item, author):  # no author: never file it under a made-up creator
        return None
    try:
        if platform == "tiktok":
            post = parse_url(str(_get(item, "webVideoUrl")))
            profile = parse_url(f"https://www.tiktok.com/@{_get(item, 'authorMeta.name')}")
            views, shares, saves = _get(item, "playCount"), _get(item, "shareCount"), _get(item, "collectCount")
            likes, comments, when = _get(item, "diggCount"), _get(item, "commentCount"), _get(item, "createTimeISO")
            text, duration, followers = _get(item, "text"), _get(item, "videoMeta.duration"), _get(item, "authorMeta.fans")
        elif platform == "instagram":
            post = parse_url(str(_get(item, "url")))
            profile = parse_url(f"https://www.instagram.com/{_get(item, 'ownerUsername')}/")
            views, shares, saves = _get(item, "videoPlayCount", "videoViewCount"), None, None
            likes, comments, when = _get(item, "likesCount"), _get(item, "commentsCount"), _get(item, "timestamp")
            text, duration, followers = _get(item, "caption"), _get(item, "videoDuration"), None
        elif platform == "youtube":
            post = parse_url(str(_get(item, "url")))
            profile = parse_url(str(_get(item, "channelUrl")))
            views, shares, saves = _get(item, "viewCount"), None, None
            likes, comments, when = _get(item, "likes"), _get(item, "commentsCount"), _get(item, "date")
            text, duration, followers = _get(item, "title"), _get(item, "duration"), _get(item, "numberOfSubscribers")
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    if not (post.platform == profile.platform == platform and post.kind == "post" and profile.kind == "account"):
        return None
    # one line, no control or invisible formatting characters (they can fake rows in a terminal or
    # reverse text in a table), at most 500 characters
    caption = " ".join("".join(c for c in str(text or "") if unicodedata.category(c) not in ("Cc", "Cf") or c in "\n\t")
                       .split())[:500]
    return {"platform": platform, "external_id": post.external_id, "url": post.url,
            "handle": profile.handle or profile.external_id, "profile_url": profile.url, "text": caption,
            "published_at": _when(when), "duration_sec": _seconds(duration), "views": _int(views), "likes": _int(likes),
            "comments": _int(comments), "shares": _int(shares), "saves": _int(saves), "followers": _int(followers)}


# --- storing ---------------------------------------------------------------------------------------------------

def intake(db, brief_id: int, videos: list[dict], origin: str, query: str | None = None) -> int:
    """Store normalized videos and list them in the brief's catalog. Returns how many were new to the
    catalog. A video seen again gets one more metric reading, never a duplicate. origin: "own" (your
    channel), "theme" (found by a search) or "peer" (another creator's channel)."""
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}")
    added = 0
    for v in videos:
        account = store.upsert_account(db, parse_url(v["profile_url"]), follower_count=v.get("followers"),
                                       is_owner=1 if origin == "own" else None)
        ref, duration = parse_url(v["url"]), v.get("duration_sec")
        family = ref.format_family or (None if not duration else "short" if duration <= SHORT_MAX_SEC else "long")
        post = store.upsert_post(db, account, ref, title=v.get("text") or None, published_at=v.get("published_at"),
                                 duration_sec=duration, format_family=family)
        reading = {k: v.get(k) for k in ("views", "likes", "comments", "shares", "saves")}
        if any(value is not None for value in reading.values()):  # an empty reading would only hide a real one
            store.add_snapshot(db, post, "broker", captured_at=v.get("captured_at"), **reading)
        added += db.execute("INSERT OR IGNORE INTO catalog (brief_id, post_id, origin, query, added_at) "
                            "VALUES (?, ?, ?, ?, ?)", (brief_id, post, origin, query, store.now_iso())).rowcount
        if v.get("text"):  # the caption as the broker sent it, kept apart from post.title, which another source may have filled
            db.execute("UPDATE catalog SET caption = ? WHERE brief_id = ? AND post_id = ?", (v["text"], brief_id, post))
        if origin == "own":  # found earlier by a search, now known to be yours: yours it stays
            db.execute("UPDATE catalog SET origin = 'own' WHERE brief_id = ? AND post_id = ?", (brief_id, post))
    return added


def _keep_raw(db, platform: str, payload: dict, items: list) -> None:
    """Keep the scraper's raw answer beside the database for RAW_DAYS (it never holds the token)."""
    database = db.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        return
    folder = Path(database).parent / "raw"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("broker-*.json"):
        if time.time() - old.stat().st_mtime > RAW_DAYS * 86400:
            old.unlink(missing_ok=True)
    stamp = hashlib.sha256(f"{time.time()}{json.dumps(payload, sort_keys=True)}".encode()).hexdigest()[:16]
    (folder / f"broker-{platform}-{stamp}.json").write_text(
        json.dumps({"asked": payload, "at": store.now_iso(), "items": items}, ensure_ascii=False), encoding="utf-8")


def scrape(db, brief_id: int, platform: str, search: str | None = None, channel: str | None = None,
           own: bool = False, limit: int = 20, run=None) -> dict:
    """One scrape, start to finish. Spends broker credits. Raises NoToken, AccessDenied or SourceError."""
    if own and search:
        raise ValueError("a search finds other people's videos; use your channel link for your own")
    payload = actor_input(platform, search=search, channel=channel, limit=limit)  # checks the request before any spend
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise NoToken("APIFY_TOKEN is not set (enter it on the Data and access screen)")
    # a scraper can ignore the limit it was given: nothing past twice the ceiling is kept or processed
    items = (run or run_actor)(ACTORS[platform], payload, token)[:MAX_ITEMS * 2]
    try:
        _keep_raw(db, platform, payload, items)
    except OSError:
        pass  # a full disk must not throw away a scrape that has already been paid for
    store.record_usage(db, "broker_items", len(items))
    videos = [v for v in (normalize(platform, item) for item in items) if v]
    unique = list({v["external_id"]: v for v in videos}.values())
    origin = "own" if own else "theme" if search else "peer"
    return {"fetched": len(items), "usable": len(videos),
            "added": intake(db, brief_id, unique, origin, query=search or channel)}


def scrape_many(db, brief_id: int, platforms, search: str, limit: int = 20, run=None) -> dict:
    """The same theme search on several platforms: one scrape, and one charge, per platform. A platform
    that fails is reported under "error" and the others are still stored. A missing token is raised
    before anything is requested."""
    out = {}
    for platform in dict.fromkeys(platforms):
        try:
            out[platform] = scrape(db, brief_id, platform, search=search, limit=limit, run=run)
        except (ValueError, AccessDenied, SourceError) as exc:
            out[platform] = {"error": str(exc)}
    return out
