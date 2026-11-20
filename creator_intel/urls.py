"""Turn any pasted or searched link into one canonical creator or post reference.

Trust boundary: links come from reviewers and search brokers. Anything that is not a
recognised creator or post URL on the three platforms raises ValueError.
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# re.ASCII: without it \w and \d accept letters and digits of every alphabet, which no platform id uses
VIDEO_ID = re.compile(r"[\w-]{11}", re.ASCII)
CHANNEL_ID = re.compile(r"UC[\w-]{22}", re.ASCII)
HANDLE = re.compile(r"[A-Za-z0-9._-]{1,30}")
NUMERIC = re.compile(r"\d{5,25}", re.ASCII)
SHORTCODE = re.compile(r"[\w-]{5,20}", re.ASCII)
IG_RESERVED = {"explore", "reel", "reels", "p", "tv", "accounts", "stories", "direct", "about", "developer", "legal"}


@dataclass(frozen=True)
class Ref:
    platform: str
    kind: str  # "account" | "post"
    external_id: str
    url: str
    handle: str | None = None
    format_family: str | None = None


def _handle(segment: str) -> str:
    name = segment.lstrip("@")
    if not HANDLE.fullmatch(name):
        raise ValueError(f"not a valid handle: {segment!r}")
    return "@" + name.lower()


def _need(pattern: re.Pattern, value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"not a valid {what}: {value!r}")
    return value


def parse_url(raw: str) -> Ref:
    raw = (raw or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not a web link: {raw!r}")
    host = re.sub(r"^(www|m)\.", "", parts.hostname.lower())
    seg = [s for s in parts.path.split("/") if s]

    if host == "youtu.be" and seg:
        vid = _need(VIDEO_ID, seg[0], "video id")
        return Ref("youtube", "post", vid, f"https://www.youtube.com/watch?v={vid}")
    if host == "youtube.com" and seg:
        if seg[0].startswith("@"):
            h = _handle(seg[0])
            return Ref("youtube", "account", h, f"https://www.youtube.com/{h}", handle=h)
        if seg[0] == "channel" and len(seg) > 1:
            cid = _need(CHANNEL_ID, seg[1], "channel id")
            return Ref("youtube", "account", cid, f"https://www.youtube.com/channel/{cid}")
        if seg[0] == "shorts" and len(seg) > 1:
            vid = _need(VIDEO_ID, seg[1], "video id")
            return Ref("youtube", "post", vid, f"https://www.youtube.com/shorts/{vid}", format_family="short")
        if seg[0] == "watch":
            vid = _need(VIDEO_ID, parse_qs(parts.query).get("v", [""])[0], "video id")
            return Ref("youtube", "post", vid, f"https://www.youtube.com/watch?v={vid}")
    if host == "tiktok.com" and seg and seg[0].startswith("@"):
        h = _handle(seg[0])
        if len(seg) >= 3 and seg[1] == "video":
            vid = _need(NUMERIC, seg[2], "video id")
            return Ref("tiktok", "post", vid, f"https://www.tiktok.com/{h}/video/{vid}", handle=h, format_family="short")
        return Ref("tiktok", "account", h, f"https://www.tiktok.com/{h}", handle=h)
    if host == "instagram.com" and seg:
        if seg[0] in ("reel", "reels", "p") and len(seg) > 1 and seg[1] != "audio":  # /reels/audio/ is a sound page
            code = _need(SHORTCODE, seg[1], "post code")
            kind = "p" if seg[0] == "p" else "reel"
            return Ref("instagram", "post", code, f"https://www.instagram.com/{kind}/{code}/",
                       format_family="short" if kind == "reel" else None)
        if seg[0] not in IG_RESERVED:
            h = _handle(seg[0])
            return Ref("instagram", "account", h, f"https://www.instagram.com/{h[1:]}/", handle=h)
    raise ValueError(f"not a supported creator or post link: {raw!r}")
