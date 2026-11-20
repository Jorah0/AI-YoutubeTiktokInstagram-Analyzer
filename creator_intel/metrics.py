"""Breakout, engagement and experiment math. Every function returns None when an input
is missing: an unknown metric stays unknown instead of becoming a zero."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

HISTORY = 15  # rolling window of previous same-format posts
MIN_HISTORY = 3  # ponytail: below this a median is noise; raise once channels have deeper history


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def outlier(views, history) -> float | None:
    """Views relative to the median of the creator's previous 15 same-format posts (oldest first)."""
    recent = [v for v in (history or []) if v is not None][-HISTORY:]
    if views is None or len(recent) < MIN_HISTORY or median(recent) <= 0:
        return None
    return views / median(recent)


def weighted_engagement(likes, comments, shares, saves, views) -> float | None:
    if None in (likes, comments, shares, saves) or not views:
        return None
    return (likes + 2 * comments + 4 * shares + 3 * saves) / views


def is_breakout(kind, *, views=None, history=None, subs=None, followers=None, likes=None, comments=None,
                shares=None, saves=None, age_days=None) -> bool | None:
    """Platform-specific breakout rule. None = cannot tell from the data we hold."""
    if kind == "instagram_reel":
        return None if views is None or not followers else views / followers >= 2.0
    o = outlier(views, history)
    if o is None:
        return None
    if kind == "youtube_long":
        return None if not subs else o >= 2.5 and views / subs >= 0.5
    if kind == "youtube_short":
        return None if age_days is None else o >= 3.0 and age_days <= 7
    if kind == "tiktok":
        we = weighted_engagement(likes, comments, shares, saves, views)
        return None if we is None else o >= 3.0 and we >= 0.12
    raise ValueError(f"unknown breakout kind: {kind}")


def explosivity(views, age_days, peers: list[tuple]) -> float | None:
    """How fast this video is going against the creator's usual: its views per day divided by the median
    views per day of their other videos (peers = (views, age in days) pairs). 3.0 = three times their usual.
    A video under a day old counts as one day old, so a few hours of data cannot produce a huge figure."""
    rates = [v / max(a, 1) for v, a in peers if v is not None and a is not None]
    if views is None or age_days is None or len(rates) < MIN_HISTORY or median(rates) <= 0:
        return None
    return (views / max(age_days, 1)) / median(rates)


def tier(ratio) -> str | None:
    """outlier: three times the usual or more. strong: one and a half times. Otherwise typical."""
    if ratio is None:
        return None
    return "outlier" if ratio >= 3.0 else "strong" if ratio >= 1.5 else "typical"


def velocity(readings: list[tuple]) -> float | None:
    """Views per hour between the first and last reading; readings = (time, views). Needs two readings."""
    known = sorted((parse_time(t), v) for t, v in readings if v is not None)
    if len(known) < 2:
        return None
    hours = (known[-1][0] - known[0][0]).total_seconds() / 3600
    return (known[-1][1] - known[0][1]) / hours if hours > 0 else None


def engagement_rate(likes, comments, shares, saves, views) -> float | None:
    """Reported interactions per view. Likes must be known (without them the figure would be a fraction of
    the truth); other counts a platform does not report are left out, not treated as zero. Platforms
    report different sets, so compare within a platform."""
    if likes is None or not views:
        return None
    return sum(x for x in (likes, comments, shares, saves) if x is not None) / views


def saves_per_1000(saves, reach) -> float | None:
    if saves is None or not reach:
        return None
    return saves / reach * 1000


def trial_decision(pairs: list[tuple]) -> str:
    """pairs = (baseline, test) per matched pair. Illustrative rule from the blueprint: repeat when
    the median paired improvement is positive and at least three of four pairs improve."""
    if len(pairs) < 4 or any(a is None or b is None for a, b in pairs):
        return "incomplete"
    diffs = [b - a for a, b in pairs]
    return "repeat" if median(diffs) > 0 and sum(d > 0 for d in diffs) >= 0.75 * len(diffs) else "stop"


def arbitrage(rows: list[dict], now: datetime, window_days=14, min_peers=3) -> list[str]:
    """Topics where 3+ tracked TikTok creators broke out recently and no tracked YouTube or
    Instagram creator has posted on it. rows: platform, account_id, topic, published_at, breakout.
    ponytail: groups by reviewed topic label; move to embedding clusters if labels prove too coarse."""
    since = now - timedelta(days=window_days)
    hot, covered = defaultdict(set), set()
    for r in rows:
        if not r.get("topic"):
            continue
        if r["platform"] != "tiktok":
            covered.add(r["topic"])
        elif r.get("breakout") and r.get("published_at") and parse_time(r["published_at"]) >= since:
            hot[r["topic"]].add(r["account_id"])
    return sorted(t for t, accounts in hot.items() if len(accounts) >= min_peers and t not in covered)
