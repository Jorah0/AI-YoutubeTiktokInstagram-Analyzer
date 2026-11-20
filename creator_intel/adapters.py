"""Everything that talks to the outside: search broker, YouTube, owner CSV exports.

Each source's permitted uses are declared in taxonomy.RIGHTS. TikTok and Instagram have no
automated adapter here on purpose: their peer data comes from reviewer notes and owner exports
until an approved connector exists (see docs/PLAN.md).
"""
import csv
import io
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

import httpx

from . import db as store
from .urls import parse_url

SHORT_MAX_SEC = 180  # YouTube Shorts run up to three minutes

# httpx logs full request URLs at INFO, and Google's API key travels in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)


class AccessDenied(Exception):
    """Permission denied, revoked, or quota exhausted. Do not retry; pause the run."""


class SourceError(Exception):
    """The source kept failing after retries, or answered with something unusable."""


def http_get(url, params=None, headers=None, client=None, sleep=time.sleep, tries=4, as_text=False):
    """GET with exponential backoff and jitter on 429/5xx/network errors. Error messages carry the
    host and status only, never the query string, because API keys travel there."""
    host = urlsplit(url).netloc
    for attempt in range(tries):
        try:
            r = (client or httpx).get(url, params=params, headers=headers, timeout=20)
        except httpx.TransportError as exc:
            status = type(exc).__name__
        else:
            if r.status_code in (401, 403):
                raise AccessDenied(f"{host} refused access ({r.status_code})")
            if r.status_code < 400:
                try:
                    return r.text if as_text else r.json()
                except ValueError:
                    raise SourceError(f"{host} sent a response that is not JSON") from None
            if r.status_code != 429 and r.status_code < 500:
                raise SourceError(f"{host} answered {r.status_code}")
            status = r.status_code
        if attempt < tries - 1:
            sleep(2 ** attempt + random.random())
    raise SourceError(f"{host} still failing after {tries} tries ({status})")


WEBHOOK_HOSTS = {"discord.com": "content", "discordapp.com": "content", "hooks.slack.com": "text"}


def post_webhook(url: str, text: str, client=None) -> None:
    """Send a short message to a Discord or Slack incoming webhook. The URL is a secret: it is only
    ever sent to those hosts over https, and it never appears in an error."""
    parts = urlsplit(url or "")
    if parts.scheme != "https" or parts.hostname not in WEBHOOK_HOSTS:
        raise ValueError("the alerts webhook must be an https Discord or Slack webhook URL")
    payload = {WEBHOOK_HOSTS[parts.hostname]: text[:1900]}
    if "content" in payload:  # Discord: a creator named @everyone must not ping the channel
        payload["allowed_mentions"] = {"parse": []}
    try:
        r = (client or httpx).post(url, json=payload, timeout=20)
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        raise SourceError(f"{parts.hostname} could not be reached ({type(exc).__name__})") from None
    if r.status_code >= 400:
        raise SourceError(f"{parts.hostname} answered {r.status_code}")


# --- search broker -------------------------------------------------------------------

def brave_search(query: str, key: str, country="us", lang="en", count=20, **kw) -> list[str]:
    """Candidate URLs only. We keep the links and nothing else from the results."""
    data = http_get("https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "country": country, "search_lang": lang, "count": count},
                    headers={"X-Subscription-Token": key, "Accept": "application/json"}, **kw)
    return [r["url"] for r in data.get("web", {}).get("results", []) if r.get("url")]


# --- YouTube ---------------------------------------------------------------------------

YT = "https://www.googleapis.com/youtube/v3/"
ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def youtube_channel_id(handle: str, key: str, **kw) -> str | None:
    """Resolve @handle to the stable channel id (1 quota unit)."""
    items = http_get(YT + "channels", params={"part": "id", "forHandle": handle, "key": key}, **kw).get("items", [])
    return items[0]["id"] if items else None


def youtube_subscribers(channel_id: str, key: str, **kw) -> int | None:
    """Subscriber count (1 quota unit). None when the channel hides it."""
    items = http_get(YT + "channels", params={"part": "statistics", "id": channel_id, "key": key}, **kw).get("items", [])
    return _int(items[0].get("statistics", {}).get("subscriberCount")) if items else None


def parse_feed(xml_text: str) -> list[dict]:
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:  # a real feed has neither; blocks entity-expansion tricks
        raise SourceError("feed contains a DTD; refusing to parse")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise SourceError("feed is not valid XML") from None
    return [{"id": e.findtext("yt:videoId", namespaces=ATOM), "title": e.findtext("a:title", namespaces=ATOM),
             "published_at": e.findtext("a:published", namespaces=ATOM)}
            for e in root.findall("a:entry", ATOM) if e.findtext("yt:videoId", namespaces=ATOM)]


def youtube_feed(channel_id: str, **kw) -> list[dict]:
    """A channel's latest uploads from the public Atom feed (no API quota)."""
    return parse_feed(http_get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id},
                               as_text=True, **kw))


def _int(value) -> int | None:
    """A plain non-negative whole number, or None. Counts are never negative, and int() alone would
    accept oddities like '1_000'."""
    text = "" if value is None else str(value).strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) < 19 else None


def parse_videos(payload: dict) -> list[dict]:
    out = []
    for item in payload.get("items", []):
        stats, snippet = item.get("statistics", {}), item.get("snippet", {})
        m = DURATION.fullmatch(item.get("contentDetails", {}).get("duration") or "")
        sec = sum(int(v or 0) * mult for v, mult in zip(m.groups(), (86400, 3600, 60, 1))) if m else None
        out.append({"id": item["id"], "title": snippet.get("title"), "published_at": snippet.get("publishedAt"),
                    "duration_sec": sec,
                    # no length (None, or 0 for a live or upcoming video) means the format is unknown
                    "format_family": None if not sec else "short" if sec <= SHORT_MAX_SEC else "long",
                    "views": _int(stats.get("viewCount")), "likes": _int(stats.get("likeCount")),
                    "comments": _int(stats.get("commentCount"))})
    return out


def youtube_videos(ids: list[str], key: str, **kw) -> list[dict]:
    """Stats for up to 50 videos per call (1 quota unit each call)."""
    out = []
    for i in range(0, len(ids), 50):
        out += parse_videos(http_get(YT + "videos", params={
            "part": "snippet,contentDetails,statistics", "id": ",".join(ids[i:i + 50]), "key": key}, **kw))
    return out


# --- owner exports -----------------------------------------------------------------------

def import_metrics_csv(db, path) -> dict:
    """Owner-provided metrics. Columns: post_url plus any of views, likes, comments, shares, saves, reach.
    A blank or non-numeric cell stays NULL. Rows for posts we do not track are reported, not guessed.
    ponytail: one normalised layout; platform exports differ and change, so map columns in a spreadsheet first."""
    imported, skipped = 0, []
    opened = (io.TextIOWrapper(path, encoding="utf-8-sig", newline="") if hasattr(path, "read")  # an uploaded file
              else open(path, newline="", encoding="utf-8-sig"))
    try:
        with opened as f:
            for n, row in enumerate(csv.DictReader(f), start=2):
                url = (row.get("post_url") or "").strip()
                try:
                    post_id = store.find_post(db, parse_url(url))
                except ValueError:
                    skipped.append(f"row {n}: not a post link {url}")
                    continue
                if post_id is None:
                    skipped.append(f"row {n}: unknown post {url}")
                    continue
                store.add_snapshot(db, post_id, "owner_export",
                                   **{c: _int(row[c]) for c in store.METRIC_FIELDS if c in row})
                imported += 1
    except (UnicodeDecodeError, csv.Error):
        skipped.append("not a UTF-8 CSV file")
    return {"imported": imported, "skipped": skipped}
