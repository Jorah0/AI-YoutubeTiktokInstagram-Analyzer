"""SQLite storage. One file, one workspace.

ponytail: SQLite instead of the blueprints' PostgreSQL. It needs no server and fits a single
reviewer. Move to Postgres when two people need to write at once or embeddings (pgvector) are added;
the SQL below is plain enough to port.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .metrics import parse_time
from .taxonomy import OBSERVED, RIGHTS, Brief
from .urls import Ref

SCHEMA = """
CREATE TABLE IF NOT EXISTS brief (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL, handle TEXT, url TEXT NOT NULL,
    channel_id TEXT, language TEXT, format_family TEXT, is_owner INTEGER NOT NULL DEFAULT 0, follower_count INTEGER,
    UNIQUE (platform, external_id));
-- Which creators a brief is considering. A creator can be a candidate in several briefs.
CREATE TABLE IF NOT EXISTS brief_account (
    brief_id INTEGER NOT NULL, account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    PRIMARY KEY (brief_id, account_id));
CREATE TABLE IF NOT EXISTS post (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    platform TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT, published_at TEXT,
    duration_sec INTEGER, format_family TEXT, sponsored INTEGER NOT NULL DEFAULT 0,
    UNIQUE (platform, external_id));
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES post(id) ON DELETE CASCADE,
    source TEXT NOT NULL, rights TEXT NOT NULL, text TEXT NOT NULL, timestamp TEXT,
    retrieved_at TEXT NOT NULL, expires_at TEXT, labeled_version TEXT);
CREATE TABLE IF NOT EXISTS tag (
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE, dimension TEXT NOT NULL,
    value TEXT NOT NULL, model_version TEXT NOT NULL, review_state TEXT NOT NULL,
    PRIMARY KEY (evidence_id, dimension));
-- Append-only: never update a row here, or growth over time is lost.
CREATE TABLE IF NOT EXISTS metric_snapshot (
    id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES post(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL, source TEXT NOT NULL, rights TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER, reach INTEGER);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY, brief_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running',
    done TEXT NOT NULL DEFAULT '[]', log TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match (
    run_id INTEGER NOT NULL, brief_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    score REAL, coverage REAL NOT NULL, posts INTEGER NOT NULL, stretch INTEGER NOT NULL, detail TEXT NOT NULL,
    rules_version TEXT NOT NULL, PRIMARY KEY (run_id, account_id));
CREATE TABLE IF NOT EXISTS match_evidence (
    run_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS experiment (
    id INTEGER PRIMARY KEY, brief_id INTEGER NOT NULL, hypothesis TEXT NOT NULL, primary_metric TEXT NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 7, plan TEXT NOT NULL, decision TEXT);
CREATE TABLE IF NOT EXISTS observation (
    experiment_id INTEGER NOT NULL REFERENCES experiment(id) ON DELETE CASCADE, pair INTEGER NOT NULL,
    variant TEXT NOT NULL, post_url TEXT, value REAL, note TEXT, measured_at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, pair, variant));
-- A reviewer's accept / reject on a creator suggested for a brief (the launch-gate test). Kept per
-- brief, not per run: "is this a good peer for this channel" does not change when the ranking is redone.
CREATE TABLE IF NOT EXISTS judgment (
    brief_id INTEGER NOT NULL, account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL, accept INTEGER NOT NULL, at TEXT NOT NULL, PRIMARY KEY (brief_id, account_id, reviewer));
-- What a reviewer did with a label the model proposed: confirmed, corrected or rejected.
CREATE TABLE IF NOT EXISTS label_review (
    evidence_id INTEGER NOT NULL, dimension TEXT NOT NULL, model_value TEXT NOT NULL, outcome TEXT NOT NULL,
    at TEXT NOT NULL);
-- Videos a brief keeps an eye on: the owner's own, ones found by a theme search, a peer's channel.
CREATE TABLE IF NOT EXISTS catalog (
    brief_id INTEGER NOT NULL, post_id INTEGER NOT NULL REFERENCES post(id) ON DELETE CASCADE,
    origin TEXT NOT NULL, query TEXT, added_at TEXT NOT NULL, caption TEXT, PRIMARY KEY (brief_id, post_id));
-- The owner's own script drafts, with the labels they gave the draft and the last AI feedback.
CREATE TABLE IF NOT EXISTS script (
    id INTEGER PRIMARY KEY, brief_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
    declared TEXT NOT NULL DEFAULT '{}', feedback TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, source TEXT NOT NULL, units INTEGER NOT NULL,
    PRIMARY KEY (day, source));
CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

ACCOUNT_FIELDS = ("channel_id", "language", "format_family", "is_owner", "follower_count")
POST_FIELDS = ("title", "published_at", "duration_sec", "format_family", "sponsored")
METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves", "reach")
REVIEW_STATES = ("accepted", "needs_review", "rejected")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; writes below are single statements
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    if "caption" not in {column[1] for column in conn.execute("PRAGMA table_info(catalog)")}:
        # A workspace from before captions were kept apart from post.title. TikTok and Instagram titles can
        # only have come from the broker; a YouTube one may be the API's, so it waits for the next scrape.
        try:
            conn.execute("ALTER TABLE catalog ADD COLUMN caption TEXT")
        except sqlite3.OperationalError:
            return conn  # the dashboard and a command opened the file at the same moment: the other one is doing this
        conn.execute("UPDATE catalog SET caption = (SELECT p.title FROM post p WHERE p.id = catalog.post_id "
                     "AND p.platform != 'youtube')")
    return conn


def open_db() -> sqlite3.Connection:
    path = Path(os.environ.get("CI_DB", "data/creator_intel.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return connect(path)


def _upsert(db, table, allowed, base: dict, fields: dict) -> int:
    """Insert, or on a replayed import fill in only the values that are new. Column names come
    from the fixed tuples above, never from input."""
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ValueError(f"unknown {table} fields: {sorted(unknown)}")
    given = {k: v for k, v in fields.items() if v is not None}
    row = {**base, **given}
    cols = ", ".join(row)
    # Only what the caller actually supplied is updated. (COALESCE(excluded.x, x) would not do: for a
    # NOT NULL DEFAULT column, excluded.x is the default, which silently reset sponsored and is_owner.)
    keep = ", ".join(f"{c} = excluded.{c}" for c in given) or "id = id"
    sql = (f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))}) "
           f"ON CONFLICT (platform, external_id) DO UPDATE SET {keep} RETURNING id")
    return db.execute(sql, list(row.values())).fetchone()[0]


def upsert_account(db, ref: Ref, **fields) -> int:
    if ref.kind != "account":
        raise ValueError(f"not a creator link: {ref.url}")
    base = {"platform": ref.platform, "external_id": ref.external_id, "handle": ref.handle, "url": ref.url}
    return _upsert(db, "account", ACCOUNT_FIELDS, base, fields)


def upsert_post(db, account_id: int, ref: Ref, **fields) -> int:
    if ref.kind != "post":
        raise ValueError(f"not a post link: {ref.url}")
    fields.setdefault("format_family", ref.format_family)
    base = {"account_id": account_id, "platform": ref.platform, "external_id": ref.external_id, "url": ref.url}
    return _upsert(db, "post", POST_FIELDS, base, fields)


def add_candidate(db, brief_id: int, account_id: int) -> bool:
    """Put a creator in a brief's cohort. True when it was not there yet."""
    return db.execute("INSERT OR IGNORE INTO brief_account VALUES (?, ?)", (brief_id, account_id)).rowcount == 1


def merge_accounts(db, keep: int, drop: int) -> None:
    """Two rows turned out to be the same creator (added once by handle, once by channel id)."""
    db.execute("UPDATE post SET account_id = ? WHERE account_id = ?", (keep, drop))
    db.execute("INSERT OR IGNORE INTO brief_account SELECT brief_id, ? FROM brief_account WHERE account_id = ?",
               (keep, drop))
    db.execute("DELETE FROM match_evidence WHERE account_id IN (?, ?)", (keep, drop))
    db.execute("DELETE FROM match WHERE account_id = ?", (keep,))  # built on a partial sample; rank again
    db.execute("DELETE FROM account WHERE id = ?", (drop,))


def find_post(db, ref: Ref) -> int | None:
    row = db.execute("SELECT id FROM post WHERE platform = ? AND external_id = ?",
                     (ref.platform, ref.external_id)).fetchone()
    return row[0] if row else None


def _rights(source: str) -> str:
    if source not in RIGHTS:
        raise ValueError(f"unknown source {source!r}; declare its rights in taxonomy.RIGHTS first")
    return RIGHTS[source]


def add_evidence(db, post_id: int, source: str, text: str, timestamp=None, expires_at=None) -> int:
    return db.execute(
        "INSERT INTO evidence (post_id, source, rights, text, timestamp, retrieved_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (post_id, source, _rights(source), text, timestamp, now_iso(), expires_at)).fetchone()[0]


def set_tag(db, evidence_id: int, dimension: str, value: str, model_version="reviewer",
            review_state="accepted", brief: Brief | None = None) -> None:
    """A reviewer's tag replaces whatever is there. A model's tag never replaces a reviewer's."""
    if dimension not in OBSERVED:
        raise ValueError(f"unknown dimension: {dimension}")
    if review_state not in REVIEW_STATES:
        raise ValueError(f"unknown review state: {review_state}")
    if brief is not None and value not in brief.taxonomy()[dimension]:
        raise ValueError(f"{value!r} is not an allowed {dimension} label")
    by_reviewer = model_version == "reviewer"
    if by_reviewer:  # remember what the reviewer did with a model's label, so label quality can be measured
        prev = db.execute("SELECT value, model_version FROM tag WHERE evidence_id = ? AND dimension = ?",
                          (evidence_id, dimension)).fetchone()
        if prev and prev["model_version"] != "reviewer":
            outcome = ("rejected" if review_state != "accepted" else
                       "confirmed" if value == prev["value"] else "corrected")
            db.execute("INSERT INTO label_review VALUES (?, ?, ?, ?, ?)",
                       (evidence_id, dimension, prev["value"], outcome, now_iso()))
    on_conflict = ("DO UPDATE SET value = excluded.value, model_version = excluded.model_version, "
                   "review_state = excluded.review_state"
                   + ("" if by_reviewer else " WHERE tag.model_version != 'reviewer'"))
    db.execute(f"INSERT INTO tag VALUES (?, ?, ?, ?, ?) ON CONFLICT (evidence_id, dimension) {on_conflict}",
               (evidence_id, dimension, value, model_version, review_state))


def flag_tag(db, evidence_id: int, dimension: str) -> None:
    """A reviewer doubts this label: send it back to the review queue. Who proposed it is kept, so
    what the reviewer finally decides is still counted against the right author."""
    db.execute("UPDATE tag SET review_state = 'needs_review' WHERE evidence_id = ? AND dimension = ?",
               (evidence_id, dimension))


def add_snapshot(db, post_id: int, source: str, captured_at: str | None = None, **values) -> None:
    """captured_at: when the numbers were read, any ISO time; stored in UTC so readings sort correctly.
    Defaults to now; pass it when importing older exports. Raises ValueError if it is not a time."""
    if captured_at is not None:
        captured_at = parse_time(captured_at).astimezone(timezone.utc).isoformat(timespec="seconds")
    unknown = set(values) - set(METRIC_FIELDS)
    if unknown:
        raise ValueError(f"unknown metrics: {sorted(unknown)}")
    cols = [c for c in METRIC_FIELDS if c in values]
    db.execute(f"INSERT INTO metric_snapshot (post_id, captured_at, source, rights{''.join(', ' + c for c in cols)}) "
               f"VALUES (?, ?, ?, ?{', ?' * len(cols)})",
               [post_id, captured_at or now_iso(), source, _rights(source)] + [values[c] for c in cols])


def _drop_evidence(db, where: str, params: tuple) -> int:
    """Delete evidence (tags cascade) and every match that was built on it."""
    accounts = [r[0] for r in db.execute(
        f"SELECT DISTINCT p.account_id FROM evidence e JOIN post p ON p.id = e.post_id WHERE {where}", params)]
    n = db.execute(f"DELETE FROM evidence AS e WHERE {where}", params).rowcount
    for account_id in accounts:
        db.execute("DELETE FROM match WHERE account_id = ?", (account_id,))
        db.execute("DELETE FROM match_evidence WHERE account_id = ?", (account_id,))
    return n


def revoke_source(db, source: str) -> int:
    """Access expired or was revoked: remove what the source gave us and what was derived from it."""
    touched = ("SELECT post_id FROM evidence WHERE source = ?1 "
               "UNION SELECT post_id FROM metric_snapshot WHERE source = ?1")
    db.execute(f"UPDATE post SET title = NULL, duration_sec = NULL, published_at = NULL WHERE id IN ({touched})",
               (source,))
    db.execute(f"DELETE FROM catalog WHERE post_id IN ({touched})", (source,))
    db.execute("DELETE FROM metric_snapshot WHERE source = ?", (source,))
    if source == "broker":
        # Only the broker fills the catalog, so all of it goes, including videos that came with a caption
        # but no numbers (they have no reading to be found by). A model trained on it is derived from it.
        db.execute("UPDATE post SET title = NULL WHERE id IN (SELECT post_id FROM catalog)")
        db.execute("DELETE FROM catalog")
        db.execute("DELETE FROM setting WHERE key LIKE 'model:%'")
    database =db.execute("PRAGMA database_list").fetchone()[2]
    if database and source.isalnum():  # raw answers kept beside the database, e.g. raw/broker-*.json
        for kept in (Path(database).parent / "raw").glob(f"{source}-*.json"):
            kept.unlink(missing_ok=True)
    return _drop_evidence(db, "e.source = ?", (source,))


def purge_expired(db, now: str | None = None) -> int:
    return _drop_evidence(db, "e.expires_at IS NOT NULL AND e.expires_at <= ?", (now or now_iso(),))


def save_brief(db, brief: Brief) -> int:
    if brief.id is None:
        brief.id = db.execute("INSERT INTO brief (data) VALUES ('{}') RETURNING id").fetchone()[0]
    if db.execute("UPDATE brief SET data = ? WHERE id = ?", (brief.model_dump_json(), brief.id)).rowcount != 1:
        raise ValueError(f"no brief {brief.id}")
    return brief.id


def get_brief(db, brief_id: int) -> Brief:
    row = db.execute("SELECT data FROM brief WHERE id = ?", (brief_id,)).fetchone()
    if not row:
        raise ValueError(f"no brief {brief_id}")
    return Brief.model_validate_json(row[0])


def list_briefs(db) -> list[Brief]:
    return [Brief.model_validate_json(r[0]) for r in db.execute("SELECT data FROM brief ORDER BY id")]


def record_usage(db, source: str, units: int) -> None:
    db.execute("INSERT INTO usage VALUES (?, ?, ?) ON CONFLICT (day, source) DO UPDATE SET units = units + excluded.units",
               (now_iso()[:10], source, units))


def get_setting(db, key: str, default=None):
    """A small stored value (per-brief tailoring, a trained model), or the default when there is none."""
    found = db.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return json.loads(found[0]) if found else default


def set_setting(db, key: str, value) -> None:
    db.execute("INSERT INTO setting VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (key, json.dumps(value)))
