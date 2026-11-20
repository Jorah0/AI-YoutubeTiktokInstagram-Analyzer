"""The analyzer: is the ranking good enough to trust (the launch gate), how are AI labels doing,
where is evidence thin, what does a run cost, and is each connector actually working."""
import csv
import json
import os
from collections import defaultdict

from . import adapters, labeling, pipeline, prepublish, ranking
from . import db as store
from .taxonomy import RIGHTS, WEIGHTS

REVIEWERS_NEEDED = 2
GATE_RATE = 0.80
CLAUDE_USD_PER_MTOK = (4.00, 20.00)  # input, output for claude-opus-5-5; change with CI_MODEL


# --- launch gate ----------------------------------------------------------------------------

def judge(db, run_id: int, account_id: int, reviewer: str, accept: bool) -> None:
    """One reviewer's verdict on one suggested creator. Judging again replaces that reviewer's verdict."""
    reviewer = reviewer.strip().lower()
    if not reviewer:
        raise ValueError("say who is judging")
    row = db.execute("SELECT brief_id FROM match WHERE run_id = ? AND account_id = ?", (run_id, account_id)).fetchone()
    if not row:
        raise ValueError(f"creator {account_id} was not ranked in run {run_id}")
    db.execute("INSERT OR REPLACE INTO judgment VALUES (?, ?, ?, ?, ?)",
               (row[0], account_id, reviewer, int(accept), store.now_iso()))


def suggestions(db, run_id: int) -> dict:
    """What the rule suggests, and what ranking by topic alone would have suggested from the same candidates."""
    cards = pipeline.all_cards(db, run_id)
    top, _ = pipeline.get_shortlist(db, run_id)

    def topic(card):
        m = card["match"].get("topic")
        return -1 if m is None else m

    enough = [c for c in cards.values() if c["posts"] >= ranking.MIN_POSTS]
    by_topic = sorted(enough, key=lambda c: (-topic(c), -c["posts"], c["account_id"]))[:ranking.MAX_PEERS]
    return {"rule": [c["account_id"] for c in top], "topic_only": [c["account_id"] for c in by_topic], "cards": cards}


def verdicts(db, run_id: int) -> dict:
    """account id -> {reviewer: accepted?} for the run's brief. Verdicts carry over when the brief is ranked again."""
    out = defaultdict(dict)
    for j in db.execute("SELECT account_id, reviewer, accept FROM judgment WHERE brief_id = "
                        "(SELECT brief_id FROM run WHERE id = ?)", (run_id,)):
        out[j["account_id"]][j["reviewer"]] = bool(j["accept"])
    return out


def evaluation(db, run_ids: list[int]) -> dict:
    """Accepted peers / suggestions for the rule and for topic-only ranking, across the given runs.
    A suggestion is accepted when at least two reviewers judged it and all of them accepted.
    gate: None until every suggestion has two verdicts; then True only when the rule reaches 80%,
    every suggestion links to evidence, and the rule beats topic-only."""
    totals = {"rule": [0, 0], "topic_only": [0, 0]}
    missing = judged = agreed = 0
    traceable = True
    for run_id in run_ids:
        s, seen = suggestions(db, run_id), verdicts(db, run_id)

        def settled(account_id):
            return len(seen.get(account_id, {})) >= REVIEWERS_NEEDED

        for account_id in set(s["rule"]) | set(s["topic_only"]):
            if not settled(account_id):
                missing += 1
                continue
            judged += 1
            agreed += len(set(seen[account_id].values())) == 1
        for name, tally in totals.items():
            tally[0] += len(s[name])
            tally[1] += sum(settled(a) and all(seen[a].values()) for a in s[name])
        traceable = traceable and all(s["cards"][a]["evidence"] for a in s["rule"])

    def report(tally):
        return {"suggested": tally[0], "accepted": tally[1], "rate": tally[1] / tally[0] if tally[0] else None}

    rule, base = report(totals["rule"]), report(totals["topic_only"])
    gate = None
    if not missing and rule["rate"] is not None:
        gate = rule["rate"] >= GATE_RATE and traceable and rule["rate"] > (base["rate"] or 0)
    return {"rule": rule, "topic_only": base, "agreement": agreed / judged if judged else None,
            "traceable": traceable, "missing": missing, "gate": gate}


def latest_runs(db) -> list[int]:
    return [r[0] for r in db.execute("SELECT max(run_id) FROM match GROUP BY brief_id")]


# --- label quality, coverage, cost -------------------------------------------------------------

def label_quality(db) -> dict:
    """What became of the labels the model proposed."""
    out = {"auto_accepted": 0, "confirmed": 0, "corrected": 0, "rejected": 0, "waiting": 0}
    for state, n in db.execute("SELECT review_state, count(*) FROM tag WHERE model_version != 'reviewer' "
                               "GROUP BY review_state"):
        if state == "accepted":
            out["auto_accepted"] = n
        elif state == "needs_review":
            out["waiting"] = n
    for outcome, n in db.execute("SELECT outcome, count(*) FROM label_review GROUP BY outcome"):
        out[outcome] += n
    return out


def coverage_gaps(db, run_id: int) -> dict:
    """Per scored dimension, how many ranked creators have it unknown. The biggest number is where
    more reviewing (or a new kind of evidence) would help most."""
    cards = pipeline.all_cards(db, run_id).values()
    return {dim: sum(c["match"].get(dim) is None for c in cards) for dim in WEIGHTS}


def costs(db, accepted: int | None = None) -> dict:
    used = dict(db.execute("SELECT source, sum(units) FROM usage GROUP BY source").fetchall())
    claude = (used.get("claude_input_tokens", 0) * CLAUDE_USD_PER_MTOK[0]
              + used.get("claude_output_tokens", 0) * CLAUDE_USD_PER_MTOK[1]) / 1e6
    try:
        brave_rate = float(os.environ.get("CI_BRAVE_USD_PER_REQUEST") or 0)
    except ValueError:
        brave_rate = 0.0  # a mistyped setting must not break the report
    brave = used.get("brave_requests", 0) * brave_rate
    total = claude + brave
    return {"claude_usd": claude, "brave_usd": brave, "total_usd": total, "youtube_units": used.get("youtube_units", 0),
            "per_accepted_peer": total / accepted if accepted else None}


# --- dashboard: where the brief stands, and what to do next -----------------------------------------

IN_BRIEF = ("FROM brief_account ba JOIN account a ON a.id = ba.account_id AND a.is_owner = 0 "
            "JOIN post p ON p.account_id = a.id JOIN evidence e ON e.post_id = p.id "
            "JOIN tag t ON t.evidence_id = e.id WHERE ba.brief_id = ?")


def overview(db, brief_id: int) -> dict:
    """The handful of numbers the dashboard leads with."""
    def one(sql):
        return db.execute(sql, (brief_id,)).fetchone()[0]

    run_id = pipeline.latest_run(db, brief_id)
    top, sparse = pipeline.get_shortlist(db, run_id) if run_id else ([], [])
    last = db.execute("SELECT id, status, started_at, log FROM run WHERE brief_id = ? ORDER BY id DESC LIMIT 1",
                      (brief_id,)).fetchone()
    return {
        "candidates": one("SELECT count(*) FROM brief_account ba JOIN account a ON a.id = ba.account_id "
                          "WHERE ba.brief_id = ? AND a.is_owner = 0"),
        "reviewed_creators": one(f"SELECT count(DISTINCT a.id) {IN_BRIEF} AND t.review_state = 'accepted'"),
        "waiting_review": one(f"SELECT count(*) {IN_BRIEF} AND t.review_state = 'needs_review'"),
        "shortlist": len(top), "needs_more_review": len(sparse), "run_id": run_id,
        "gate": evaluation(db, [run_id])["gate"] if run_id else None,
        "breakouts": sum(1 for b in pipeline.breakouts(db) if b["breakout"]),
        "last_run": dict(last) if last else None,
    }


def progress(db, brief_id: int) -> list[dict]:
    """The plan's steps (docs/PLAN.md, weeks 1 to 4) with done / not done worked out from the data,
    so the dashboard can say what to do next. Each step names the screen where it is done."""
    brief = store.get_brief(db, brief_id)
    seen = overview(db, brief_id)

    def one(sql, *params):
        return db.execute(sql, params).fetchone()[0]

    missing = [f.replace("_", " ") for f in ("topic", "audience_promise", "format_subtype") if not brief.owner.get(f)]
    three_posts = one(f"SELECT count(*) FROM (SELECT a.id {IN_BRIEF} AND t.review_state = 'accepted' "
                      "GROUP BY a.id HAVING count(DISTINCT p.id) >= 3)", brief_id)
    keys = ["BRAVE_API_KEY"] + (["YOUTUBE_API_KEY"] if brief.platform == "youtube" else [])
    unset = [k for k in keys if not os.environ.get(k)]
    # Did a run of THIS brief search or sample? (The usage counters are shared by all briefs and by
    # `doctor --live`, so they cannot answer that.)
    logs = [json.loads(r[0]) for r in db.execute("SELECT log FROM run WHERE brief_id = ?", (brief_id,))]
    discovered = any("new candidates" in str(log.get("discover")) or "posts sampled" in str(log.get("refresh"))
                     for log in logs)
    ev = evaluation(db, [seen["run_id"]]) if seen["run_id"] else None
    own_metrics = one("SELECT count(*) FROM metric_snapshot s JOIN post p ON p.id = s.post_id "
                      "JOIN account a ON a.id = p.account_id WHERE a.is_owner = 1 AND a.platform = ? "
                      "AND s.source = 'owner_export'", brief.platform)
    experiments = one("SELECT count(*) FROM experiment WHERE brief_id = ?", brief_id)
    decided = one("SELECT count(*) FROM experiment WHERE brief_id = ? AND decision IN ('repeat', 'stop')", brief_id)

    def step(week, name, is_done, detail, where):
        return {"week": week, "step": name, "done": bool(is_done), "detail": detail, "where": where}

    return [
        step(1, "Write the brief", not missing,
             "Your own labels are set." if not missing else "Still missing: your " + ", ".join(missing) + ".", "Brief"),
        step(1, "Import ten creator links", seen["candidates"] >= 10, f"{seen['candidates']} of 10 added.", "Candidates"),
        step(1, "Review three posts for at least three creators", three_posts >= 3,
             f"{three_posts} creators have three reviewed posts.", "Review"),
        step(1, "Rank and get a shortlist", seen["shortlist"] > 0, f"{seen['shortlist']} on the shortlist.", "Shortlist"),
        step(2, "Add your search and YouTube keys", not unset,
             "Keys are set." if not unset else "Not set: " + ", ".join(unset) + " (enter them on the Data and access screen).",
             "Data and access"),
        step(2, "Run discovery with your keys", discovered,
             "A run of this brief has searched for candidates or sampled YouTube." if discovered
             else "No run of this brief has searched or sampled yet.", "Candidates, Run pipeline"),
        step(3, "Have two reviewers judge every suggestion", ev and ev["rule"]["suggested"] > 0 and not ev["missing"],
             "Rank first." if not ev else f"{ev['missing']} suggestions still need two verdicts.", "Analysis"),
        step(3, "Pass the launch gate", ev and ev["gate"] is True,
             "Needs 80% accepted and a better result than ranking by topic alone.", "Analysis"),
        step(4, "Add your own channel and import your metrics", own_metrics > 0,
             f"{own_metrics} readings imported for your own posts.", "Data and access"),
        step(4, "Start an experiment", experiments > 0, f"{experiments} started.", "Shortlist"),
        step(4, "Reach a decision on the experiment", decided > 0,
             f"{decided} decided. A decision needs four complete pairs.", "Experiments"),
    ]


def next_step(steps: list[dict]) -> dict | None:
    return next((s for s in steps if not s["done"]), None)


# --- viewer data ----------------------------------------------------------------------------------

def creator_view(db, account_id: int) -> dict:
    """Everything held about one creator: posts, the accepted labels on each, the evidence, metric history."""
    acc = db.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if acc is None:
        raise ValueError(f"no creator {account_id}")
    posts = []
    for p in db.execute("SELECT * FROM post WHERE account_id = ? ORDER BY published_at DESC, id DESC", (account_id,)):
        evidence = [dict(e) for e in db.execute(
            "SELECT id, source, text, timestamp FROM evidence WHERE post_id = ? ORDER BY id", (p["id"],))]
        values = defaultdict(set)
        for t in db.execute("SELECT t.dimension, t.value FROM tag t JOIN evidence e ON e.id = t.evidence_id "
                            "WHERE e.post_id = ? AND t.review_state = 'accepted'", (p["id"],)):
            values[t["dimension"]].add(t["value"])
        snapshots = [dict(s) for s in db.execute(
            "SELECT captured_at, source, views, likes, comments, shares, saves, reach FROM metric_snapshot "
            "WHERE post_id = ? ORDER BY captured_at, id", (p["id"],))]
        posts.append({"id": p["id"], "url": p["url"], "title": p["title"], "published_at": p["published_at"],
                      "sponsored": bool(p["sponsored"]), "evidence": evidence, "snapshots": snapshots,
                      "labels": {d: next(iter(v)) for d, v in values.items() if len(v) == 1}})
    return {"id": acc["id"], "handle": acc["handle"] or acc["url"], "url": acc["url"], "platform": acc["platform"],
            "followers": acc["follower_count"], "posts": posts,
            "profile": ranking.aggregate([p["labels"] for p in posts if not p["sponsored"]])}


# --- tools ---------------------------------------------------------------------------------------

def _cell(value) -> str:
    """Text that came from elsewhere must not run as a formula when the file is opened in a spreadsheet."""
    text = "" if value is None else str(value)
    risky = text[:1] in ("\t", "\r", "\n") or text.lstrip(" \t\r\n\x00")[:1] in ("=", "+", "-", "@")
    return "'" + text if text and risky else text


def export_labels(db, brief_id: int, path) -> int:
    """Every label on the brief's candidates, one row each, for reviewer calibration in a spreadsheet."""
    rows = db.execute(
        "SELECT ltrim(COALESCE(a.handle, a.external_id), '@') AS creator, p.url AS post_url, e.id AS evidence_id, e.source, e.text AS evidence, "
        "t.dimension, t.value, t.model_version AS labeled_by, t.review_state "
        "FROM brief_account ba JOIN account a ON a.id = ba.account_id JOIN post p ON p.account_id = a.id "
        "JOIN evidence e ON e.post_id = p.id JOIN tag t ON t.evidence_id = e.id "
        "WHERE ba.brief_id = ? ORDER BY a.handle, p.id, e.id, t.dimension", (brief_id,)).fetchall()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        out = csv.writer(f)
        out.writerow(["creator", "post_url", "evidence_id", "source", "evidence", "dimension", "value", "labeled_by",
                      "review_state"])
        out.writerows([_cell(v) for v in row] for row in rows)
    return len(rows)


def doctor(db, live: bool = False) -> list[dict]:
    """State of every connector. live=True makes one small real request per configured service
    (it spends a little quota), and reports denied access instead of failing."""
    def probe(env, usage, call):
        key = os.environ.get(env)
        if not key:
            return "no key set"
        if not live:
            return "key set (not tested)"
        try:
            store.record_usage(db, usage, 1)  # a live check spends real quota; keep the books honest
            return "ok: " + call(key)
        except (adapters.AccessDenied, adapters.SourceError) as exc:
            return str(exc)

    def ai_status():
        if not live:
            return "not tested"
        try:
            if labeling.unconfirmed(db):  # an unconfirmed server is sent nothing, not even a test with the key
                return "not confirmed for this workspace (Assistant screen, or the trust-ai command)"
        except adapters.SourceError as exc:
            return str(exc)
        return labeling.check_access()

    counts = [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("brief", "account", "evidence")]
    hook = os.environ.get("CI_WEBHOOK_URL")
    return [
        {"name": "Database", "status": "ok: {} briefs, {} creators, {} evidence items".format(*counts)},
        {"name": "Brave search", "status": probe(
            "BRAVE_API_KEY", "brave_requests", lambda k: f"{len(adapters.brave_search('site:youtube.com/@ sewing', k, count=1))} result(s)")},
        {"name": "YouTube", "status": probe(
            "YOUTUBE_API_KEY", "youtube_units", lambda k: f"{len(adapters.youtube_videos(['dQw4w9WgXcQ'], k))} video(s) returned")},
        {"name": "YouTube data may be used for", "status": RIGHTS["youtube_api"]},
        {"name": "AI model", "status": f"{labeling.describe()}: {ai_status()}"},
        {"name": "Data broker (Apify)", "status": "token set (not tested: a test scrape costs credits)"
            if os.environ.get("APIFY_TOKEN") else "no token set"},
        {"name": "Alerts webhook", "status": "set" if hook else "not set"},
        {"name": "ffmpeg (loudness check)", "status": "found" if prepublish.find_ffmpeg() else "not found"},
        {"name": "TikTok, Instagram", "status": "manual review and owner exports only (no connector)"},
    ]
