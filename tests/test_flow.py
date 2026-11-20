"""Storage, labeling validation, adapters and the full offline demo flow."""
from pathlib import Path

import httpx
import pytest

from creator_intel import adapters, db as store, labeling, pipeline
from creator_intel.taxonomy import Brief
from creator_intel.urls import parse_url

APP = str(Path(__file__).parent.parent / "app.py")


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def make_brief(db):
    return store.save_brief(db, Brief(
        name="Beginner sewing", platform="youtube", language="en", format_family="short",
        objective="grow subscribers",
        owner={"audience_promise": "learn_one_repair", "format_subtype": "demonstration",
               "topic": "sewing_repair", "delivery": "calm", "opening": "problem_first"},
        owner_setup="single_camera", topics=["sewing_repair", "clothing_care", "cosplay"],
        audience_promises=["learn_one_repair", "entertain"],
        related={"topic": [["sewing_repair", "clothing_care"]]}))


def add_reviewed_post(db, account_id, n, labels, source="reviewer_note"):
    post = store.upsert_post(db, account_id, parse_url(f"https://www.youtube.com/shorts/{account_id:03d}{n:08d}"),
                             published_at=f"2026-11-{10 + n:02d}T00:00:00+00:00")
    ev = store.add_evidence(db, post, source, "Shows the split hem, then demonstrates one stitch.", timestamp="00:02")
    for dim, value in labels.items():
        store.set_tag(db, ev, dim, value)
    return post, ev


GOOD = {"audience_promise": "learn_one_repair", "format_subtype": "demonstration", "topic": "clothing_care",
        "production_setup": "single_camera", "opening": "result_first"}


# --- storage ---------------------------------------------------------------------

def test_replaying_an_import_creates_no_duplicates(db):
    links = ["https://www.youtube.com/@SewSimple", "https://youtube.com/@sewsimple/shorts",
             "https://www.youtube.com/shorts/abcdefghijk", "https://youtu.be/abcdefghijk"]
    for _ in range(2):
        acc = store.upsert_account(db, parse_url(links[0]))
        store.upsert_account(db, parse_url(links[1]))
        store.upsert_post(db, acc, parse_url(links[2]))
        store.upsert_post(db, acc, parse_url(links[3]))
    assert db.execute("select count(*) from account").fetchone()[0] == 1
    assert db.execute("select count(*) from post").fetchone()[0] == 1


def test_upsert_keeps_existing_values_when_new_ones_are_missing(db):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"), language="en", format_family="short")
    store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    assert db.execute("select language from account where id=?", (acc,)).fetchone()[0] == "en"


def test_metric_snapshots_are_append_only(db):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    post = store.upsert_post(db, acc, parse_url("https://youtu.be/abcdefghijk"))
    store.add_snapshot(db, post, "owner_export", views=100)
    store.add_snapshot(db, post, "owner_export", views=250)
    assert [r[0] for r in db.execute("select views from metric_snapshot order by id")] == [100, 250]


def test_tag_values_outside_the_taxonomy_are_refused(db):
    brief_id = make_brief(db)
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    _, ev = add_reviewed_post(db, acc, 1, {})
    with pytest.raises(ValueError):
        store.set_tag(db, ev, "format_subtype", "interpretive_dance", brief=store.get_brief(db, brief_id))
    with pytest.raises(ValueError):
        store.set_tag(db, ev, "vibes", "good")


# --- the end-to-end demo flow (blueprint "first working demo") --------------------

def test_brief_to_shortlist_with_clickable_evidence(db):
    brief_id = make_brief(db)
    strong = store.upsert_account(db, parse_url("https://www.youtube.com/@strong"), language="en", format_family="short")
    weak = store.upsert_account(db, parse_url("https://www.youtube.com/@weak"), language="en", format_family="short")
    thin = store.upsert_account(db, parse_url("https://www.youtube.com/@thin"), language="en", format_family="short")
    other = store.upsert_account(db, parse_url("https://www.youtube.com/@longform"), language="en", format_family="long")
    for account_id in (strong, weak, thin):  # "other" belongs to a different brief
        store.add_candidate(db, brief_id, account_id)
    for n in range(3):
        add_reviewed_post(db, strong, n, GOOD)
        add_reviewed_post(db, weak, n, {**GOOD, "audience_promise": "entertain", "format_subtype": "vlog",
                                        "delivery": "energetic"})
        add_reviewed_post(db, other, n, GOOD)
    add_reviewed_post(db, thin, 0, GOOD)

    run_id = pipeline.rank(db, brief_id)
    cards, sparse = pipeline.get_shortlist(db, run_id)

    assert [c["handle"] for c in cards] == ["@strong", "@weak"]
    top = cards[0]
    assert round(top["score"], 1) == 88.2 and top["coverage"] == pytest.approx(0.85) and top["posts"] == 3
    assert len(top["evidence"]) == 3 and all(e["url"].startswith("https://www.youtube.com/") for e in top["evidence"])
    assert "delivery" in top["explanation"].lower()  # the unverified dimension is disclosed
    assert [c["handle"] for c in sparse] == ["@thin"]  # queued for review, not shortlisted
    assert "result_first" in pipeline.experiment_idea(db, run_id)


def test_unreviewed_and_rejected_tags_never_count(db):
    brief_id = make_brief(db)
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"), language="en", format_family="short")
    store.add_candidate(db, brief_id, acc)
    for n in range(3):
        _, ev = add_reviewed_post(db, acc, n, {})
        for dim, value in GOOD.items():
            store.set_tag(db, ev, dim, value, model_version="claude", review_state="needs_review")
    cards, sparse = pipeline.get_shortlist(db, pipeline.rank(db, brief_id))
    assert cards == [] and sparse == []  # no evidence-backed posts at all


def test_revoking_a_source_removes_evidence_tags_and_dependent_matches(db):
    brief_id = make_brief(db)
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"), language="en", format_family="short")
    keep = store.upsert_account(db, parse_url("https://www.youtube.com/@keep"), language="en", format_family="short")
    store.add_candidate(db, brief_id, acc)
    store.add_candidate(db, brief_id, keep)
    for n in range(3):
        add_reviewed_post(db, acc, n, GOOD, source="youtube_api")
        add_reviewed_post(db, keep, n, GOOD)
    run_id = pipeline.rank(db, brief_id)
    assert len(pipeline.get_shortlist(db, run_id)[0]) == 2

    store.revoke_source(db, "youtube_api")

    assert db.execute("select count(*) from evidence where source='youtube_api'").fetchone()[0] == 0
    assert db.execute("select count(*) from tag").fetchone()[0] == 3 * len(GOOD)
    assert [c["handle"] for c in pipeline.get_shortlist(db, run_id)[0]] == ["@keep"]


def test_expired_evidence_is_purged(db):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    post = store.upsert_post(db, acc, parse_url("https://youtu.be/abcdefghijk"))
    store.add_evidence(db, post, "reviewer_note", "old", expires_at="2026-01-01T00:00:00+00:00")
    store.add_evidence(db, post, "reviewer_note", "fresh", expires_at="2099-01-01T00:00:00+00:00")
    store.purge_expired(db, now="2026-11-19T00:00:00+00:00")
    assert [r[0] for r in db.execute("select text from evidence")] == ["fresh"]


# --- AI labeling: the model proposes, deterministic checks decide ------------------

def test_model_labels_are_validated_against_batch_taxonomy_and_evidence(db):
    brief = store.get_brief(db, make_brief(db))
    items = {
        1: {"source": "reviewer_note", "text": "Shows the split hem, then demonstrates one stitch."},
        2: {"source": "youtube_api", "text": "How to fix a hem in 30 seconds"},
    }
    out = labeling.Batch(items=[
        labeling.ItemLabels(evidence_id=1, labels=[
            labeling.Label(dimension="format_subtype", value="demonstration", evidence_quote="demonstrates one stitch"),
            labeling.Label(dimension="opening", value="problem_first", evidence_quote="this quote is invented"),
            labeling.Label(dimension="topic", value="quantum_physics", evidence_quote="split hem"),
            labeling.Label(dimension="audience_promise", value="unknown", evidence_quote=""),
        ]),
        labeling.ItemLabels(evidence_id=2, labels=[
            labeling.Label(dimension="delivery", value="calm", evidence_quote="fix a hem"),  # text can't show delivery
            labeling.Label(dimension="topic", value="sewing_repair", evidence_quote="FIX A  HEM"),
        ]),
        labeling.ItemLabels(evidence_id=99, labels=[  # not in the batch
            labeling.Label(dimension="topic", value="sewing_repair", evidence_quote="x")]),
    ])
    got = {(e, d): (v, state) for e, d, v, state in labeling.validate(items, out, brief)}
    assert got == {
        (1, "format_subtype"): ("demonstration", "accepted"),
        (1, "opening"): ("problem_first", "needs_review"),  # quote not in the evidence
        (1, "topic"): ("quantum_physics", "needs_review"),  # off-taxonomy
        (2, "topic"): ("sewing_repair", "accepted"),
    }


def test_labeling_skips_restricted_evidence_caches_and_never_overwrites_reviewers(db):
    brief_id = make_brief(db)
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    _, ev = add_reviewed_post(db, acc, 1, {"topic": "cosplay"})
    post2 = store.upsert_post(db, acc, parse_url("https://youtu.be/abcdefghijk"))
    store.add_evidence(db, post2, "brave", "search snippet we may not process")  # rights: discovery only

    calls = []

    def fake_model(prompt_items):
        calls.append(prompt_items)
        return labeling.Batch(items=[labeling.ItemLabels(evidence_id=ev, labels=[
            labeling.Label(dimension="topic", value="sewing_repair", evidence_quote="split hem"),
            labeling.Label(dimension="format_subtype", value="demonstration", evidence_quote="demonstrates one stitch"),
        ])])

    assert labeling.label_pending(db, brief_id, call_model=fake_model) == 1
    assert [i["id"] for i in calls[0]] == [ev]  # the brave evidence never reached the model
    tags = dict(db.execute("select dimension, value from tag where evidence_id=?", (ev,)).fetchall())
    assert tags == {"topic": "cosplay", "format_subtype": "demonstration"}  # reviewer's topic kept
    labeling.label_pending(db, brief_id, call_model=fake_model)
    assert len(calls) == 1  # second run: nothing pending, no second call


# --- adapters ------------------------------------------------------------------------

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <entry><yt:videoId>abcdefghijk</yt:videoId><title>Fix a hem</title><published>2026-11-10T10:00:00+00:00</published></entry>
 <entry><yt:videoId>lmnopqrstuv</yt:videoId><title>Sew a button</title><published>2026-11-01T10:00:00+00:00</published></entry>
</feed>"""


def test_parse_youtube_feed_and_videos():
    assert adapters.parse_feed(FEED)[0] == {"id": "abcdefghijk", "title": "Fix a hem",
                                           "published_at": "2026-11-10T10:00:00+00:00"}
    payload = {"items": [{"id": "abcdefghijk", "snippet": {"title": "Fix a hem", "publishedAt": "2026-11-10T10:00:00Z"},
                          "contentDetails": {"duration": "PT45S"},
                          "statistics": {"viewCount": "1200", "commentCount": "7"}},  # likes hidden
                         {"id": "lmnopqrstuv", "snippet": {}, "contentDetails": {"duration": "PT1H2M3S"},
                          "statistics": {}}]}
    a, b = adapters.parse_videos(payload)
    assert (a["views"], a["likes"], a["comments"], a["duration_sec"], a["format_family"]) == (1200, None, 7, 45, "short")
    assert (b["views"], b["duration_sec"], b["format_family"]) == (None, 3723, "long")


def test_http_get_retries_transient_failures_then_succeeds():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503 if len(seen) < 3 else 200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert adapters.http_get("https://api.example/x", client=client, sleep=lambda s: None) == {"ok": True}
    assert len(seen) == 3


def test_http_get_does_not_retry_denied_access_or_leak_the_key():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "denied"})))
    with pytest.raises(adapters.AccessDenied) as err:
        adapters.http_get("https://api.example/x", params={"key": "SECRET123"}, client=client, sleep=lambda s: None)
    assert "SECRET123" not in str(err.value)


def test_discovery_keeps_only_supported_creator_links_and_dedupes(db):
    brief_id = make_brief(db)
    hits = ["https://www.youtube.com/@SewSimple", "https://www.youtube.com/@sewsimple/videos",
            "https://www.reddit.com/r/sewing", "https://www.youtube.com/shorts/abcdefghijk"]
    added = pipeline.discover(db, brief_id, ["q"], search=lambda q: hits)
    assert added == 1
    assert pipeline.discover(db, brief_id, ["q"], search=lambda q: hits) == 0  # replay adds nothing
    assert [r[0] for r in db.execute("select handle from account")] == ["@sewsimple"]


def test_metrics_csv_import_keeps_missing_values_null_with_reason(db, tmp_path):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@me"), is_owner=1)
    store.upsert_post(db, acc, parse_url("https://youtu.be/abcdefghijk"))
    f = tmp_path / "export.csv"
    f.write_text("post_url,views,likes,saves\nhttps://youtu.be/abcdefghijk,1200,,n/a\nhttps://youtu.be/zzzzzzzzzzz,5,5,5\n")
    report = adapters.import_metrics_csv(db, f)
    row = db.execute("select views, likes, saves, source from metric_snapshot").fetchone()
    assert tuple(row) == (1200, None, None, "owner_export")
    assert report == {"imported": 1, "skipped": ["row 3: unknown post https://youtu.be/zzzzzzzzzzz"]}


# --- the resumable run, experiments, breakouts, the demo and the app ------------------------

def test_run_pauses_on_denied_access_and_resumes_after_the_last_finished_stage(db, monkeypatch):
    brief_id = make_brief(db)
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(labeling, "label_pending", lambda db, brief_id: 0)
    calls = []

    def search(query, key, **kw):
        calls.append(query)
        if len(calls) == 1:
            raise adapters.AccessDenied("api.search.brave.com refused access (403)")
        return ["https://www.youtube.com/@found"]

    monkeypatch.setattr(adapters, "brave_search", search)
    first = pipeline.run_all(db, brief_id)
    assert first["status"] == "paused" and "refused" in first["log"]["discover"]
    assert db.execute("select done from run where id=?", (first["run_id"],)).fetchone()[0] == "[]"

    second = pipeline.run_all(db, brief_id, first["run_id"])
    assert second["run_id"] == first["run_id"] and second["status"] == "finished"
    assert second["log"]["refresh"].startswith("skipped") and second["log"]["rank"] == "ranked"
    n = len(calls)
    pipeline.run_all(db, brief_id, first["run_id"])  # a finished run repeats nothing
    assert len(calls) == n


def test_experiment_keeps_missing_values_null_and_decides_only_when_complete(db):
    brief_id = make_brief(db)
    exp = pipeline.create_experiment(db, brief_id, "result first beats problem first", "avg % viewed at 7 days")
    for pair, (a, b) in enumerate([(40, 46), (41, 44), (39, 45), (42, None)], start=1):
        pipeline.record_observation(db, exp, pair, "baseline", a)
        pipeline.record_observation(db, exp, pair, "test", b, note=None if b else "export missing")
    assert pipeline.experiment_result(db, exp)["decision"] == "incomplete"
    pipeline.record_observation(db, exp, 4, "test", 41)  # the replacement measurement
    assert pipeline.experiment_result(db, exp) == {"pairs": [(40, 46), (41, 44), (39, 45), (42, 41)], "decision": "repeat"}
    with pytest.raises(ValueError):
        pipeline.record_observation(db, exp, 1, "control", 1)


def test_breakouts_score_only_metrics_we_may_derive_from(db):
    acc = store.upsert_account(db, parse_url("https://www.instagram.com/fix_a_hem/"), follower_count=1000)
    scored = store.upsert_post(db, acc, parse_url("https://www.instagram.com/reel/AAAAAAAAAAA/"))
    quiet = store.upsert_post(db, acc, parse_url("https://www.instagram.com/reel/BBBBBBBBBBB/"))
    locked = store.upsert_post(db, acc, parse_url("https://www.instagram.com/reel/CCCCCCCCCCC/"))
    store.add_snapshot(db, scored, "owner_export", views=2500)
    store.add_snapshot(db, quiet, "owner_export", views=900)
    store.add_snapshot(db, locked, "youtube_api", views=99999)  # store-only rights: must not be scored
    got = {b["url"][-12:-1]: b["breakout"] for b in pipeline.breakouts(db)}
    assert got == {"AAAAAAAAAAA": True, "BBBBBBBBBBB": False}


def test_demo_seed_produces_a_ranked_shortlist_and_digest(db):
    from creator_intel import demo
    brief_id = demo.seed(db)
    demo.seed(db)  # seeding twice must not duplicate creators or posts
    assert db.execute("select count(*) from account").fetchone()[0] == 10
    cards, sparse = pipeline.get_shortlist(db, pipeline.latest_run(db, brief_id))
    assert [(c["handle"], round(c["score"], 1), c["stretch"]) for c in cards] == [
        ("@fictional-mend-02", 100.0, False), ("@fictional-mend-03", 94.1, True),
        ("@fictional-mend-01", 88.2, False), ("@fictional-mend-04", 30.0, False)]
    assert [c["handle"] for c in sparse] == ["@fictional-mend-05"]
    text = pipeline.digest(db, brief_id)
    assert "@fictional-mend-02" in text and "result_first" in text and "](https://www.youtube.com/" in text


def test_app_renders_every_screen_without_errors(tmp_path, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    from creator_intel import demo
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    empty = testing.AppTest.from_file(APP, default_timeout=30).run()
    assert not empty.exception and empty.title[0].value == "New brief"
    conn = store.open_db()
    brief_id = demo.seed(conn)
    pipeline.create_experiment(conn, brief_id, "h", "m")
    conn.close()
    at = testing.AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Shortlist").run()
    assert not at.exception, at.exception
    assert any("@fictional-mend-02 · 100/100 fit · 100% coverage · 3 posts reviewed" in s.value for s in at.subheader)
