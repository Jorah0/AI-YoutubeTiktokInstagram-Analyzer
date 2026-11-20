"""The run: discover -> refresh -> label -> rank, plus breakouts, experiments and the weekly digest."""
import json
import os
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import anthropic

from . import adapters, labeling, metrics, ranking
from . import db as store
from .taxonomy import RULES_VERSION
from .urls import parse_url

SAMPLE_POSTS = 5  # most recent relevant posts per creator
SAMPLE_DAYS = 90
STAGES = ["discover", "refresh", "label", "rank"]
MD_SPECIAL = re.compile(r"([\\`*_\[\]()!#|~$/])")


def md(text) -> str:
    """Make text we did not write (titles, captions, model output) inert inside Markdown: no markup, no
    HTML, no clickable link (escaping the slashes and the dot stops a bare address from being linked),
    and on one line so it cannot start a heading or a list."""
    text = " ".join(str(text).split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"(?i)\bwww\.", r"www\\.", MD_SPECIAL.sub(r"\\\1", text))


SITE = {"youtube": "site:youtube.com/@", "tiktok": "site:tiktok.com/@", "instagram": "site:instagram.com"}


# --- discovery -------------------------------------------------------------------------

def default_queries(brief) -> list[str]:
    """Several searches per brief: vary topic and viewer problem, as the blueprint asks."""
    terms = [t.replace("_", " ") for t in brief.topics[:3] + brief.audience_promises[:2]]
    return [f'{SITE[brief.platform]} "{t}"' for t in terms]


def import_links(db, brief_id: int, links: list[str]) -> dict:
    """Add creator links to the brief's cohort. Replaying the same links adds nothing."""
    brief = store.get_brief(db, brief_id)
    added, rejected = 0, []
    for link in links:
        try:
            ref = parse_url(link)
            if ref.platform != brief.platform:
                raise ValueError(f"{link} is not a {brief.platform} link")
            if ref.kind == "post":
                if not ref.handle:
                    raise ValueError(f"{link} is a post; paste the creator's profile link")
                ref = parse_url(f"https://www.tiktok.com/{ref.handle}")  # only TikTok post links carry the handle
            account_id = store.upsert_account(db, ref)
            if db.execute("SELECT is_owner FROM account WHERE id = ?", (account_id,)).fetchone()[0]:
                raise ValueError(f"{link} is your own channel, not a candidate")
            added += store.add_candidate(db, brief_id, account_id)
        except ValueError as exc:
            rejected.append(str(exc))
    return {"added": added, "rejected": rejected}


def discover(db, brief_id: int, queries: list[str] | None = None, search=None) -> int:
    brief = store.get_brief(db, brief_id)
    if search is None:
        key = os.environ["BRAVE_API_KEY"]
        search = lambda q: adapters.brave_search(q, key, lang=brief.language)  # noqa: E731
    added = 0
    for query in queries or default_queries(brief):
        hits = search(query)
        store.record_usage(db, "brave_requests", 1)
        added += import_links(db, brief_id, hits)["added"]
    return added


# --- YouTube refresh -------------------------------------------------------------------

def refresh_youtube(db, key: str, now: datetime | None = None) -> int:
    """Sample each tracked channel's recent uploads and record a metric snapshot. Returns posts seen."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=SAMPLE_DAYS)
    seen = 0
    for acc in db.execute("SELECT * FROM account WHERE platform = 'youtube'").fetchall():
        try:  # one stale or broken channel must not stop the others; denied access still pauses the run
            seen += _refresh_channel(db, acc, key, since)
        except adapters.SourceError:
            store.record_usage(db, "youtube_channel_errors", 1)
    return seen


def _refresh_channel(db, acc, key: str, since: datetime) -> int:
    seen = 0
    channel = acc["channel_id"] or (acc["external_id"] if acc["external_id"].startswith("UC") else None)
    if not channel:
        channel = adapters.youtube_channel_id(acc["external_id"], key)
        store.record_usage(db, "youtube_units", 1)
        if not channel:
            return 0
        same = db.execute("SELECT id FROM account WHERE platform = 'youtube' AND id != ? "
                          "AND (external_id = ? OR channel_id = ?)", (acc["id"], channel, channel)).fetchone()
        if same:  # already tracked under its channel id: fold this row into that one
            store.merge_accounts(db, keep=same[0], drop=acc["id"])
            return 0
        db.execute("UPDATE account SET channel_id = ? WHERE id = ?", (channel, acc["id"]))
    feed = adapters.youtube_feed(channel)
    subscribers = adapters.youtube_subscribers(channel, key)
    store.record_usage(db, "youtube_units", 1)
    if subscribers is not None:
        db.execute("UPDATE account SET follower_count = ? WHERE id = ?", (subscribers, acc["id"]))
    recent = [e for e in feed
              if e["published_at"] and metrics.parse_time(e["published_at"]) >= since][:SAMPLE_POSTS]
    if not recent:
        return 0
    store.record_usage(db, "youtube_units", 1)
    for v in adapters.youtube_videos([e["id"] for e in recent], key):
        try:
            ref = parse_url(f"https://www.youtube.com/watch?v={v['id']}")
        except ValueError:
            continue  # an id we cannot validate is not stored
        post_id = store.upsert_post(db, acc["id"], ref,
                                    title=v["title"], published_at=v["published_at"],
                                    duration_sec=v["duration_sec"], format_family=v["format_family"])
        store.add_snapshot(db, post_id, "youtube_api", views=v["views"], likes=v["likes"], comments=v["comments"])
        has_title = db.execute("SELECT 1 FROM evidence WHERE post_id = ? AND source = 'youtube_api'", (post_id,))
        if v["title"] and not has_title.fetchone():
            store.add_evidence(db, post_id, "youtube_api", v["title"])
        seen += 1
    return seen


# --- ranking ----------------------------------------------------------------------------

def _new_run(db, brief_id: int) -> int:
    return db.execute("INSERT INTO run (brief_id, started_at) VALUES (?, ?) RETURNING id",
                      (brief_id, store.now_iso())).fetchone()[0]


def rank(db, brief_id: int, run_id: int | None = None) -> int:
    """Score every reviewed candidate of the brief from accepted, evidence-backed tags only. The cohort is
    the creators imported into this brief (the reviewer vouches for platform and language) and their
    non-sponsored posts in the brief's format family."""
    brief = store.get_brief(db, brief_id)
    standalone = run_id is None  # "Rank now": a run of its own, finished as soon as ranking is
    run_id = run_id or _new_run(db, brief_id)
    rows = db.execute(
        "SELECT a.id AS account_id, p.id AS post_id, e.id AS evidence_id, t.dimension, t.value "
        "FROM brief_account ba JOIN account a ON a.id = ba.account_id JOIN post p ON p.account_id = a.id "
        "JOIN evidence e ON e.post_id = p.id JOIN tag t ON t.evidence_id = e.id "
        "WHERE ba.brief_id = ? AND a.platform = ? AND a.is_owner = 0 "
        "AND (p.format_family IS NULL OR p.format_family = ?) AND p.sponsored = 0 AND t.review_state = 'accepted' "
        "ORDER BY p.published_at DESC, p.id DESC",
        (brief_id, brief.platform, brief.format_family)).fetchall()
    db.execute("DELETE FROM match WHERE run_id = ?", (run_id,))  # a resumed run starts its ranking clean
    db.execute("DELETE FROM match_evidence WHERE run_id = ?", (run_id,))

    posts = defaultdict(dict)  # account -> post -> dimension -> set of values (dicts keep newest-first order)
    used = defaultdict(set)
    for r in rows:
        by_post = posts[r["account_id"]]
        if r["post_id"] not in by_post and len(by_post) >= SAMPLE_POSTS:
            continue
        by_post.setdefault(r["post_id"], defaultdict(set))[r["dimension"]].add(r["value"])
        used[r["account_id"]].add(r["evidence_id"])

    for account_id, by_post in posts.items():
        # two notes on one post that disagree leave that dimension unknown for the post
        labels = [{d: next(iter(v)) for d, v in dims.items() if len(v) == 1} for dims in by_post.values()]
        profile = ranking.aggregate(labels)
        s = ranking.score(brief.owner, brief.owner_setup, profile, brief.related)
        detail = {"match": s.match, "profile": profile,
                  "explanation": ranking.explain(s, brief.owner, profile)}
        db.execute("INSERT OR REPLACE INTO match VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (run_id, brief_id, account_id, s.fit, s.coverage, len(by_post), int(s.stretch),
                    json.dumps(detail), RULES_VERSION))
        db.execute("DELETE FROM match_evidence WHERE run_id = ? AND account_id = ?", (run_id, account_id))
        db.executemany("INSERT INTO match_evidence VALUES (?, ?, ?)",
                       [(run_id, account_id, e) for e in sorted(used[account_id])])
    if standalone:
        db.execute("UPDATE run SET status = 'finished', done = ? WHERE id = ?", (json.dumps(["rank"]), run_id))
    return run_id


def latest_run(db, brief_id: int) -> int | None:
    return db.execute("SELECT max(run_id) FROM match WHERE brief_id = ?", (brief_id,)).fetchone()[0]


def all_cards(db, run_id: int) -> dict:
    """account id -> card, for every creator ranked in the run. Every card carries its evidence links."""
    cards = {}
    for m in db.execute(
            "SELECT m.*, a.handle, a.url, a.follower_count, (SELECT max(o.follower_count) FROM account o "
            "  WHERE o.is_owner = 1 AND o.platform = a.platform) AS owner_followers "
            "FROM match m JOIN account a ON a.id = m.account_id WHERE m.run_id = ?", (run_id,)):
        detail = json.loads(m["detail"])
        evidence = [dict(e) for e in db.execute(
            "SELECT e.id, p.url, e.text, e.timestamp, e.source FROM match_evidence me "
            "JOIN evidence e ON e.id = me.evidence_id JOIN post p ON p.id = e.post_id "
            "WHERE me.run_id = ? AND me.account_id = ? ORDER BY p.published_at DESC", (run_id, m["account_id"]))]
        cards[m["account_id"]] = {
            "account_id": m["account_id"], "handle": m["handle"] or m["url"], "url": m["url"], "score": m["score"],
            "coverage": m["coverage"], "posts": m["posts"], "stretch": bool(m["stretch"]), "evidence": evidence,
            "followers": m["follower_count"],
            "size_ratio": (m["follower_count"] / m["owner_followers"]
                           if m["follower_count"] and m["owner_followers"] else None),
            **detail}
    return cards


def get_shortlist(db, run_id: int) -> tuple[list[dict], list[dict]]:
    """(shortlist cards, sparse candidates needing more review)."""
    cards = all_cards(db, run_id)
    cands =[ranking.Cand(i, c["posts"], ranking.Score(c["score"], c["coverage"], c["match"], c["stretch"]))
             for i, c in cards.items()]
    top, sparse = ranking.shortlist(cands)
    return [cards[c.account_id] for c in top], [cards[c.account_id] for c in sparse]


def experiment_idea(db, run_id: int) -> str | None:
    brief_id = db.execute("SELECT brief_id FROM run WHERE id = ?", (run_id,)).fetchone()[0]
    cards, _ = get_shortlist(db, run_id)
    return ranking.suggest_experiment(store.get_brief(db, brief_id).owner.get("opening"),
                                      [c["profile"].get("opening") for c in cards])


# --- breakouts ---------------------------------------------------------------------------

def breakouts(db, at_days: int | None = None) -> list[dict]:
    """One snapshot per post, judged against the same creator's earlier posts of the same format.
    Only snapshots whose source grants the "derive" right are scored.

    at_days=None uses each post's latest snapshot. at_days=7 uses the last snapshot taken within seven
    days of publishing, so every post is compared at the same age; posts with no snapshot that early
    (or no publish date) are left out. Use it once the weekly run has collected a few weeks of history."""
    early = "" if at_days is None else " AND julianday(captured_at) - julianday(p.published_at) <= ?"
    rows = db.execute(
        "SELECT p.id, p.url, p.title, p.platform, p.published_at, p.account_id, a.handle, a.follower_count, "
        "COALESCE(p.format_family, a.format_family) AS fmt, s.* FROM post p JOIN account a ON a.id = p.account_id "
        "JOIN metric_snapshot s ON s.id = (SELECT id FROM metric_snapshot WHERE post_id = p.id "
        f"  AND instr(',' || rights || ',', ',derive,') > 0{early} ORDER BY captured_at DESC, id DESC LIMIT 1) "
        "WHERE a.is_owner = 0 ORDER BY p.published_at IS NULL, p.published_at",  # undated posts last
        () if at_days is None else (at_days,)).fetchall()
    history, out = defaultdict(list), []
    for r in rows:
        kind = {"youtube": "youtube_short" if r["fmt"] == "short" else "youtube_long", "tiktok": "tiktok",
                "instagram": "instagram_reel"}[r["platform"]]
        past = history[(r["account_id"], r["fmt"])]
        age = None
        if r["published_at"]:
            age = (metrics.parse_time(r["captured_at"])
                   - metrics.parse_time(r["published_at"])).total_seconds() / 86400  # fractional: 7d 23h is not 7
        hit = metrics.is_breakout(kind, views=r["views"], history=past, subs=r["follower_count"],
                                  followers=r["follower_count"], likes=r["likes"], comments=r["comments"],
                                  shares=r["shares"], saves=r["saves"], age_days=age)
        topic = db.execute("SELECT t.value FROM tag t JOIN evidence e ON e.id = t.evidence_id WHERE e.post_id = ? "
                           "AND t.dimension = 'topic' AND t.review_state = 'accepted'", (r["id"],)).fetchone()
        out.append({"url": r["url"], "title": r["title"], "handle": r["handle"], "platform": r["platform"],
                    "account_id": r["account_id"], "published_at": r["published_at"], "views": r["views"],
                    "outlier": metrics.outlier(r["views"], past), "breakout": hit, "topic": topic[0] if topic else None})
        if r["views"] is not None:
            past.append(r["views"])
    return out


# --- experiments -----------------------------------------------------------------------------

def create_experiment(db, brief_id: int, hypothesis: str, primary_metric: str, pairs: int = 4) -> int:
    """Plan matched pairs with a random order per pair (which opening is published first)."""
    plan = [{"pair": n, "first": random.choice(["baseline", "test"])} for n in range(1, pairs + 1)]
    return db.execute("INSERT INTO experiment (brief_id, hypothesis, primary_metric, plan) VALUES (?, ?, ?, ?) "
                      "RETURNING id", (brief_id, hypothesis, primary_metric, json.dumps(plan))).fetchone()[0]


def record_observation(db, experiment_id: int, pair: int, variant: str, value: float | None,
                       post_url: str | None = None, note: str | None = None) -> None:
    """value None = the metric was unavailable; say why in note. It stays NULL, never zero."""
    if variant not in ("baseline", "test"):
        raise ValueError("variant must be 'baseline' or 'test'")
    db.execute("INSERT OR REPLACE INTO observation VALUES (?, ?, ?, ?, ?, ?, ?)",
               (experiment_id, pair, variant, post_url, value, note, store.now_iso()))


def experiment_result(db, experiment_id: int) -> dict:
    plan = json.loads(db.execute("SELECT plan FROM experiment WHERE id = ?", (experiment_id,)).fetchone()[0])
    seen = {(o["pair"], o["variant"]): o["value"] for o in db.execute(
        "SELECT pair, variant, value FROM observation WHERE experiment_id = ?", (experiment_id,))}
    pairs = [(seen.get((p["pair"], "baseline")), seen.get((p["pair"], "test"))) for p in plan]
    decision = metrics.trial_decision(pairs)
    db.execute("UPDATE experiment SET decision = ? WHERE id = ?", (decision, experiment_id))
    return {"pairs": pairs, "decision": decision}


# --- the run, resumable ---------------------------------------------------------------------

def run_all(db, brief_id: int, run_id: int | None = None) -> dict:
    """Run the stages in order. A stage that lacks its credential is skipped and says so. Denied access
    or exhausted quota pauses the run; call again with the same run_id to resume after the last finished stage."""
    brief = store.get_brief(db, brief_id)
    run_id = run_id or _new_run(db, brief_id)
    row = db.execute("SELECT done, log FROM run WHERE id = ? AND brief_id = ?", (run_id, brief_id)).fetchone()
    if row is None:
        raise ValueError(f"brief {brief_id} has no run {run_id}")
    done, log = json.loads(row["done"]), json.loads(row["log"])

    def do(stage):
        if stage == "discover":
            if not os.environ.get("BRAVE_API_KEY"):
                return "skipped: BRAVE_API_KEY not set"
            return f"{discover(db, brief_id)} new candidates"
        if stage == "refresh":
            if brief.platform != "youtube":
                return f"skipped: {brief.platform} has no automated connector; use reviewer notes and owner exports"
            if not os.environ.get("YOUTUBE_API_KEY"):
                return "skipped: YOUTUBE_API_KEY not set"
            return f"{refresh_youtube(db, os.environ['YOUTUBE_API_KEY'])} posts sampled"
        if stage == "label":
            try:
                return f"{labeling.label_pending(db, brief_id)} evidence items labeled"
            except labeling.NoCredential as exc:
                return f"skipped: {exc}"
        rank(db, brief_id, run_id)
        return "ranked"

    status = "finished"
    for stage in [s for s in STAGES if s not in done]:
        try:
            log[stage] = do(stage)
        except (adapters.AccessDenied, adapters.SourceError, anthropic.APIError) as exc:
            # our own errors carry host and status only; SDK errors are reduced to their type name
            reason = type(exc).__name__ if isinstance(exc, anthropic.APIError) else str(exc)
            log[stage], status = f"paused: {reason}", "paused"
            break
        done.append(stage)
        db.execute("UPDATE run SET done = ?, log = ? WHERE id = ?", (json.dumps(done), json.dumps(log), run_id))
    db.execute("UPDATE run SET status = ?, log = ? WHERE id = ?", (status, json.dumps(log), run_id))
    return {"run_id": run_id, "status": status, "log": log}


def weekly(db, brief_id: int, out=None) -> dict:
    """The scheduled job: run the stages, write the digest, and send a short alert when a webhook is set."""
    result = run_all(db, brief_id)
    text = digest(db, brief_id)
    if out:
        out.write_text(text, encoding="utf-8")
    result["alert"] = "no webhook set"
    hook = os.environ.get("CI_WEBHOOK_URL")
    if hook:
        run_id = latest_run(db, brief_id)
        cards = get_shortlist(db, run_id)[0] if run_id else []
        hits = sum(1 for b in breakouts(db) if b["breakout"])
        summary = (f"{store.get_brief(db, brief_id).name}: run {result['status']}. Shortlist: "
                   + (", ".join(c["handle"] for c in cards) or "none yet") + f". Breakout posts: {hits}.")
        try:
            adapters.post_webhook(hook, summary)
            result["alert"] = "sent"
        except (ValueError, adapters.SourceError) as exc:
            result["alert"] = f"not sent: {exc}"
    return result


# --- weekly digest ---------------------------------------------------------------------------

def digest(db, brief_id: int) -> str:
    brief = store.get_brief(db, brief_id)
    lines = [f"# {brief.name}: weekly creator research", ""]
    run_id = latest_run(db, brief_id)
    cards, sparse = get_shortlist(db, run_id) if run_id else ([], [])
    lines.append("## Creators worth studying")
    for c in cards:
        tag = " · stretch" if c["stretch"] else ""
        size = ranking.size_note(c["size_ratio"])
        lines += [f"- **[{c['handle']}]({c['url']})** · {c['score']:.0f}/100 fit · {c['coverage']:.0%} coverage · "
                  f"{c['posts']} posts reviewed{tag}", f"  {c['explanation']}" + (f" Audience: {size}." if size else ""),
                  "  Evidence: " + ", ".join(f"[{n}]({u})" for n, u in
                                             enumerate(dict.fromkeys(e["url"] for e in c["evidence"]), 1))]
    if not cards:
        lines.append("- None yet. Review at least three posts per candidate, then rank.")
    if sparse:
        lines.append(f"- Waiting on more review before they can be ranked: {len(sparse)}.")
    idea = experiment_idea(db, run_id) if run_id else None
    lines += ["", "## One idea to test", idea or "No repeated pattern among the shortlist yet."]
    found = breakouts(db)
    hits = [b for b in found if b["breakout"]]
    lines += ["", "## Breakout posts"]
    lines += [f"- [{md(b['title'] or 'post')}]({b['url']}) by {b['handle']}"
              + (f" · {b['outlier']:.1f}x their usual views" if b["outlier"] else "") for b in hits]
    if not hits:
        lines.append("- None detected (or no metrics we are permitted to score).")
    early = metrics.arbitrage(found, datetime.now(timezone.utc))
    if early:
        lines += ["", "## Hot on TikTok, not yet covered elsewhere", *[f"- {t}" for t in early]]
    return "\n".join(lines) + "\n"
