"""Broker intake (Apify), the video catalog, explosivity and the improvement tracker."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from creator_intel import adapters, broker, db as store, metrics, tracker
from creator_intel.taxonomy import Brief

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
CREATORS = ["me", "ana", "ben", "cy", "dee"]

TT = {"id": "7300000000000000001", "webVideoUrl": "https://www.tiktok.com/@mend.it/video/7300000000000000001",
      "text": "Fix a hem fast #mending", "createTimeISO": "2026-11-10T10:00:00.000Z",
      "authorMeta": {"name": "mend.it", "fans": 12000}, "playCount": 50000, "diggCount": 4000, "commentCount": 120,
      "shareCount": 900, "collectCount": 700, "videoMeta": {"duration": 34}}
IG = {"shortCode": "Cxyz_12-AB", "url": "https://www.instagram.com/reel/Cxyz_12-AB/", "caption": "Hem fix",
      "timestamp": "2026-11-09T08:00:00.000Z", "ownerUsername": "fix_a_hem", "videoPlayCount": 20000,
      "videoViewCount": 15000, "likesCount": 900, "commentsCount": 30, "videoDuration": 28.5, "type": "Video"}
YT = {"id": "abcdefghijk", "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Fix a hem",
      "date": "10 months ago", "channelName": "Sew Simple", "channelUrl": "https://www.youtube.com/@SewSimple",
      "numberOfSubscribers": 5300, "viewCount": 8000, "likes": 300, "commentsCount": 12, "duration": "00:03:17"}


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def brief(db):
    return store.save_brief(db, Brief(name="b", platform="tiktok", topics=["mending"], audience_promises=["learn"]))


def video(handle, n, views, days_ago, **extra):
    vid = f"{CREATORS.index(handle) + 1}{n:018d}"
    return {"platform": "tiktok", "external_id": vid, "url": f"https://www.tiktok.com/@{handle}/video/{vid}",
            "handle": f"@{handle}", "profile_url": f"https://www.tiktok.com/@{handle}", "text": f"{handle} video {n}",
            "published_at": (NOW - timedelta(days=days_ago)).isoformat(), "duration_sec": 30, "views": views,
            "likes": None, "comments": None, "shares": None, "saves": None, "followers": None,
            "captured_at": NOW.isoformat(), **extra}


# --- what the broker sends, in our terms -------------------------------------------------------------

def test_each_platforms_items_are_read_into_one_shape():
    t = broker.normalize("tiktok", TT)
    assert (t["external_id"], t["handle"], t["views"], t["likes"], t["comments"], t["shares"], t["saves"]) == (
        "7300000000000000001", "@mend.it", 50000, 4000, 120, 900, 700)
    assert (t["published_at"], t["duration_sec"], t["followers"]) == ("2026-11-10T10:00:00+00:00", 34, 12000)
    assert t["url"] == "https://www.tiktok.com/@mend.it/video/7300000000000000001"
    i = broker.normalize("instagram", IG)
    assert (i["handle"], i["views"], i["duration_sec"], i["shares"], i["text"]) == ("@fix_a_hem", 20000, 28, None, "Hem fix")
    y = broker.normalize("youtube", YT)
    assert (y["handle"], y["views"], y["duration_sec"], y["followers"]) == ("@sewsimple", 8000, 197, 5300)
    assert y["published_at"] is None  # "10 months ago" is not a date; unknown stays unknown


@pytest.mark.parametrize("platform,item", [
    ("tiktok", {**TT, "webVideoUrl": "https://evil.example/@x/video/1"}),  # not a platform link
    ("tiktok", {"errorCode": "NOT_FOUND"}),
    ("instagram", {**IG, "url": "https://www.instagram.com/explore/tags/x/"}),
    ("youtube", {**YT, "channelUrl": "javascript:alert(1)"}),
    ("youtube", "not even a dict"),
])
def test_items_that_are_not_a_video_on_that_platform_are_dropped(platform, item):
    assert broker.normalize(platform, item) is None


def test_actor_inputs_use_each_scrapers_documented_fields():
    assert broker.actor_input("tiktok", search="visible mending", limit=15) == {
        "searchQueries": ["visible mending"], "resultsPerPage": 15}
    assert broker.actor_input("tiktok", channel="https://www.tiktok.com/@Mend.It", limit=5) == {
        "profiles": ["mend.it"], "resultsPerPage": 5}
    assert broker.actor_input("instagram", search="#Visible Mending", limit=10) == {
        "directUrls": ["https://www.instagram.com/explore/tags/visiblemending/"], "resultsType": "posts", "resultsLimit": 10}
    assert broker.actor_input("instagram", channel="instagram.com/fix_a_hem", limit=10)["directUrls"] == [
        "https://www.instagram.com/fix_a_hem/"]
    yt = broker.actor_input("youtube", channel="https://www.youtube.com/@SewSimple/videos", limit=8)
    assert yt == {"startUrls": [{"url": "https://www.youtube.com/@sewsimple"}], "maxResults": 8, "maxResultsShorts": 8}
    assert broker.actor_input("youtube", search="fix a hem", limit=8)["searchQueries"] == ["fix a hem"]
    for bad in ({"search": "x", "channel": "https://www.tiktok.com/@a"}, {}, {"channel": "https://www.youtube.com/@a"},
                {"search": "   "}):
        with pytest.raises(ValueError):
            broker.actor_input("tiktok", **bad)


def test_the_broker_call_sends_the_token_in_a_header_and_never_leaks_it():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[TT])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert broker.run_actor("clockworks~tiktok-scraper", {"profiles": ["a"]}, "TOKEN123", client=client) == [TT]
    assert seen[0].url.path == "/v2/actors/clockworks~tiktok-scraper/run-sync-get-dataset-items"
    assert seen[0].headers["authorization"] == "Bearer TOKEN123" and "TOKEN123" not in str(seen[0].url)
    assert json.loads(seen[0].read()) == {"profiles": ["a"]}

    for status, error, word in ((403, adapters.AccessDenied, "refused"), (408, adapters.SourceError, "five minutes"),
                                (500, adapters.SourceError, "500")):
        failing = httpx.Client(transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, json={"error": "x"})))
        with pytest.raises(error) as err:
            broker.run_actor("a~b", {}, "TOKEN123", client=failing)
        assert word in str(err.value) and "TOKEN123" not in str(err.value)
    odd = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"not": "a list"})))
    with pytest.raises(adapters.SourceError):
        broker.run_actor("a~b", {}, "TOKEN123", client=odd)


# --- storing what came back ------------------------------------------------------------------------------

def test_intake_catalogs_videos_once_and_keeps_every_reading(db, brief):
    assert broker.intake(db, brief, [video("ana", 1, 1000, 5, followers=900)], "theme", "mending") == 1
    assert broker.intake(db, brief, [video("ana", 1, 1800, 5)], "theme", "mending") == 0  # same video, a later reading
    assert db.execute("select count(*) from post").fetchone()[0] == 1
    assert [r[0] for r in db.execute("select views from metric_snapshot order by id")] == [1000, 1800]
    assert tuple(db.execute("select origin, query from catalog").fetchone()) == ("theme", "mending")
    acc = db.execute("select handle, follower_count, is_owner from account").fetchone()
    assert tuple(acc) == ("@ana", 900, 0)
    assert db.execute("select count(*) from evidence").fetchone()[0] == 0  # nothing is queued for AI at intake
    assert db.execute("select count(*) from brief_account").fetchone()[0] == 0  # a themed video is not a candidate

    broker.intake(db, brief, [video("me", 1, 500, 2)], "own")
    assert db.execute("select is_owner from account where handle = '@me'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        broker.intake(db, brief, [], "stolen")


def test_scrape_needs_a_token_caps_the_request_and_keeps_the_raw_answer(db, brief, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    with pytest.raises(broker.NoToken):
        broker.scrape(db, brief, "tiktok", search="mending")
    monkeypatch.setenv("APIFY_TOKEN", "TOKEN123")
    calls = []

    def run(actor, payload, token):
        calls.append((actor, payload, token))
        return [TT, {"errorCode": "x"}, TT]

    got = broker.scrape(db, brief, "tiktok", search="mending", limit=5000, run=run)
    assert got == {"fetched": 3, "usable": 2, "added": 1}
    assert calls[0][0] == "clockworks~tiktok-scraper" and calls[0][1]["resultsPerPage"] == broker.MAX_ITEMS
    raw = list((Path(db.execute("pragma database_list").fetchone()[2]).parent / "raw").glob("*.json"))
    assert len(raw) == 1 and "TOKEN123" not in raw[0].read_text(encoding="utf-8")
    assert dict(db.execute("select source, units from usage").fetchall())["broker_items"] == 3


# --- explosivity ----------------------------------------------------------------------------------------------

def test_explosivity_measures_speed_against_the_creators_own_usual():
    usual = [(1000, 1), (2000, 2), (3000, 3)]  # all 1000 views a day
    assert metrics.explosivity(6000, 2, usual) == 3.0
    assert metrics.explosivity(6000, 2, usual[:2]) is None  # two videos are not a baseline
    assert metrics.explosivity(None, 2, usual) is None and metrics.explosivity(6000, None, usual) is None
    assert metrics.explosivity(3000, 0.1, usual) == 3.0  # under a day old counts as one day, never divides by ~0
    assert [metrics.tier(x) for x in (3.0, 2.99, 1.5, 1.49, None)] == ["outlier", "strong", "strong", "typical", None]
    assert metrics.velocity([("2026-11-01T00:00:00+00:00", 100), ("2026-11-02T00:00:00+00:00", 2500)]) == 100.0
    assert metrics.velocity([("2026-11-01T00:00:00+00:00", 100)]) is None
    assert metrics.engagement_rate(40, 10, None, None, 1000) == 0.05
    assert metrics.engagement_rate(None, None, None, None, 1000) is None and metrics.engagement_rate(1, 1, 1, 1, 0) is None


def test_tracker_separates_outliers_from_merely_good_videos(db, brief):
    steady = [video("ana", n, 1000 * age, age, likes=50 * age) for n, age in enumerate((10, 8, 6), start=1)]
    broker.intake(db, brief, steady + [video("ana", 4, 9000, 3, likes=900, shares=300)], "theme", "mending")
    broker.intake(db, brief, [video("ben", 1, 400, 4)], "theme", "mending")  # one video: no baseline of their own
    broker.intake(db, brief, [video("me", 1, 500, 5)], "own")
    rows = {r["external_id"][-1] + r["handle"]: r for r in tracker.rows(db, brief, now=NOW)}
    hit = rows["4@ana"]
    assert hit["views_per_day"] == 3000 and hit["explosivity"] == 3.0 and hit["tier"] == "outlier"
    assert hit["compared_with"] == "their own videos" and hit["engagement"] == pytest.approx(1200 / 9000)
    assert hit["share_rate"] == pytest.approx(300 / 9000)
    assert rows["1@ana"]["tier"] == "typical" and rows["1@ana"]["explosivity"] == 1.0
    lone = rows["1@ben"]
    assert lone["explosivity"] is None and lone["compared_with"] == "the other creators in this catalog"
    assert lone["theme_ratio"] == pytest.approx(100 / 1000) and lone["tier"] == "typical"
    assert rows["1@me"]["origin"] == "own" and rows["1@me"]["share_rate"] is None  # not reported is not zero


def test_only_fast_videos_are_queued_for_ai_and_only_once(db, brief):
    steady = [video("ana", n, 1000 * age, age) for n, age in enumerate((10, 8, 6), start=1)]
    broker.intake(db, brief, steady + [video("ana", 4, 9000, 3, text="How I fixed this hem")], "theme", "mending")
    assert tracker.promote_for_ai(db, brief, now=NOW) == 1
    ev = db.execute("select source, text, rights from evidence").fetchall()
    assert [tuple(e) for e in ev] == [("broker", "How I fixed this hem", "store,ai,derive")]
    assert tracker.promote_for_ai(db, brief, now=NOW) == 0


# --- what to improve ----------------------------------------------------------------------------------------------

def row(origin, tier, per_day, engagement, share, duration):
    return {"origin": origin, "tier": tier, "views_per_day": per_day, "engagement": engagement, "share_rate": share,
            "duration_sec": duration, "title": "t", "handle": "@h", "url": "u"}


def test_improvements_compare_your_videos_with_the_outliers_and_say_when_there_is_too_little_to_go_on():
    mine = [row("own", "typical", 200, 0.02, 0.001, 120) for _ in range(3)]
    best = [row("theme", "outlier", 3000, 0.08, 0.02, 35) for _ in range(3)]
    table = {r["factor"]: r for r in tracker.improvements(mine + best + [row("theme", "typical", 900, 0.5, 0.5, 10)])}
    assert table["length"]["yours"] == "120 s" and table["length"]["outliers"] == "35 s"
    assert table["engagement"]["yours"] == "2.0%" and table["engagement"]["outliers"] == "8.0%"
    assert "4.0x" in table["engagement"]["gap"] and table["views per day"]["outliers"] == "3,000"
    assert tracker.improvements(mine + best[:2]) == [] and tracker.improvements(best) == []
    unknown = tracker.improvements(mine + [row("theme", "outlier", 3000, None, None, 35) for _ in range(3)])
    assert {r["factor"]: r["outliers"] for r in unknown}["engagement"] == "unknown"


def test_ai_summary_gets_the_numbers_and_permitted_captions_and_is_bounded(db, brief):
    steady = [video("ana", n, 1000 * age, age) for n, age in enumerate((10, 8, 6), start=1)]
    broker.intake(db, brief, steady + [video("ana", 4, 9000, 3, text="How I fixed this hem")], "theme", "mending")
    broker.intake(db, brief, [video("me", n, 300, 3, text=f"My video {n}") for n in (1, 2, 3)], "own")
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        return tracker.Advice(summary="Shorter openings.\x1b[2J", priorities=[
            tracker.Priority(factor="length", evidence="Outliers run 30 s.", change="Cut to 30 s.")] * 20)

    out = tracker.ai_improvements(db, brief, call=fake, now=NOW)
    assert "How I fixed this hem" in seen["user"] and "My video 1" in seen["user"] and '"explosivity": 3.0' in seen["user"]
    assert "\x1b" not in out["summary"] and len(out["priorities"]) <= 6


# --- commands and screens -------------------------------------------------------------------------------------------

def test_scrape_and_tracker_commands(tmp_path, monkeypatch, capsys):
    from creator_intel import __main__ as cli
    monkeypatch.setenv("CI_DB", str(tmp_path / "cli.db"))
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    assert cli.main(["demo"]) == 0
    assert cli.main(["scrape", "1", "--platform", "tiktok", "--search", "mending"]) == 2
    assert "APIFY_TOKEN" in capsys.readouterr().out
    conn = store.open_db()
    broker.intake(conn, 1, [video("ana", 1, 1000, 5)], "theme", "mending")
    conn.close()
    assert cli.main(["tracker", "1"]) == 0
    assert "@ana" in capsys.readouterr().out


def test_sidebar_navigation_reaches_every_screen_and_the_tracker_renders(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from creator_intel import demo
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    conn = store.open_db()
    brief_id = demo.seed(conn)
    broker.intake(conn, brief_id, [video("ana", n, 1000 * a, a) for n, a in enumerate((10, 8, 6), start=1)]
                  + [video("ana", 4, 15000, 3)], "theme", "mending")  # well clear of 3x whatever the clock says
    conn.close()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    screens = list(at.sidebar.radio(key="nav").options)
    assert screens[0] == "Dashboard" and {"Video tracker", "Script builder", "Shortlist", "Analysis"} <= set(screens)
    for screen in screens:
        at.sidebar.radio(key="nav").set_value(screen).run()
        assert not at.exception, (screen, at.exception)
    at.sidebar.radio(key="nav").set_value("Video tracker").run()
    assert {m.label: m.value for m in at.metric}["Outliers"] == "1"


# --- findings from the two reviews of this round ---------------------------------------------------------

def test_items_with_no_author_or_absurd_values_are_dropped_not_crashed_on():
    assert broker.normalize("tiktok", {**TT, "authorMeta": {}}) is None  # must not become a creator called "@none"
    assert broker.normalize("instagram", {k: v for k, v in IG.items() if k != "ownerUsername"}) is None
    huge = broker.normalize("tiktok", {**TT, "videoMeta": {"duration": float("inf")}, "playCount": 10 ** 30,
                                       "createTimeISO": "0001-01-01T00:00:00+14:00"})
    assert (huge["duration_sec"], huge["views"], huge["published_at"]) == (None, None, None)
    messy = broker.normalize("tiktok", {**TT, "text": "line one\nstrong 9.9x @fake‮​ tail"})
    assert messy["text"] == "line one strong 9.9x @fake tail"
    with pytest.raises(ValueError):
        from creator_intel.urls import parse_url
        parse_url("https://www.tiktok.com/@a/video/١٢٣٤٥٦")  # non-ASCII digits


def test_a_video_found_by_search_becomes_yours_when_your_channel_is_scraped(db, brief):
    broker.intake(db, brief, [video("me", 1, 500, 2)], "theme", "mending")
    broker.intake(db, brief, [video("me", 1, 600, 2)], "own")
    assert db.execute("select origin from catalog").fetchone()[0] == "own"
    broker.intake(db, brief, [video("me", 1, 700, 2)], "theme", "mending")  # and it stays yours
    assert db.execute("select origin from catalog").fetchone()[0] == "own"
    with pytest.raises(ValueError):
        broker.scrape(db, brief, "tiktok", search="mending", own=True)  # a search is never "my channel"


def test_a_later_reading_without_views_does_not_erase_what_is_known(db, brief):
    broker.intake(db, brief, [video("ana", 1, 5000, 5)], "theme")
    broker.intake(db, brief, [video("ana", 1, None, 5)], "theme")
    assert db.execute("select count(*) from metric_snapshot").fetchone()[0] == 1  # an empty reading is not stored
    assert tracker.rows(db, brief, now=NOW)[0]["views"] == 5000


def test_speed_is_measured_at_the_time_of_the_reading_not_today(db, brief):
    old = (NOW - timedelta(days=30)).isoformat()
    for n in (1, 2, 3):
        broker.intake(db, brief, [video("ana", n, 4000, 34, captured_at=old)], "theme")  # read when 4 days old
    assert tracker.rows(db, brief, now=NOW)[0]["views_per_day"] == 1000


def test_one_prolific_creator_does_not_set_the_bar_for_everyone(db, brief):
    broker.intake(db, brief, [video("ana", n, 100_000, 10) for n in range(1, 31)], "peer")  # 30 videos, 10,000 a day
    for who in ("ben", "cy"):
        broker.intake(db, brief, [video(who, n, 5000, 10) for n in (1, 2, 3)], "theme")  # 500 a day each
    broker.intake(db, brief, [video("dee", 1, 5000, 10)], "theme")
    lone = next(r for r in tracker.rows(db, brief, now=NOW) if r["handle"] == "@dee")
    assert lone["theme_ratio"] == 1.0  # against the middle creator (ana 10,000, ben 500, cy 500), not the middle video


def test_engagement_needs_likes_and_captions_respect_the_broker_rights(db, brief, monkeypatch):
    assert metrics.engagement_rate(None, 10, 5, 5, 1000) is None  # likes hidden: comments alone would mislead
    from creator_intel import taxonomy
    monkeypatch.setitem(taxonomy.RIGHTS, "broker", "store,derive")
    monkeypatch.setitem(tracker.RIGHTS, "broker", "store,derive")
    steady = [video("ana", n, 1000 * age, age) for n, age in enumerate((10, 8, 6), start=1)]
    broker.intake(db, brief, steady + [video("ana", 4, 9000, 3, text="Their caption")], "theme")
    broker.intake(db, brief, [video("me", 1, 300, 3, text="My caption")], "own")
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        return tracker.Advice(summary="s", priorities=[])

    tracker.ai_improvements(db, brief, call=fake, now=NOW)
    assert "Their caption" not in seen["user"] and "My caption" not in seen["user"]


def test_revoking_the_broker_removes_readings_catalog_rows_and_raw_files(db, brief, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    broker.scrape(db, brief, "tiktok", search="mending", run=lambda actor, payload, token: [TT] * 500)
    raw = Path(db.execute("pragma database_list").fetchone()[2]).parent / "raw"
    kept = json.loads(next(raw.glob("*.json")).read_text(encoding="utf-8"))
    assert len(kept["items"]) <= broker.MAX_ITEMS * 2  # an oversized answer is cut before anything is done with it
    store.revoke_source(db, "broker")
    assert db.execute("select count(*) from catalog").fetchone()[0] == 0
    assert db.execute("select count(*) from metric_snapshot").fetchone()[0] == 0 and not list(raw.glob("*.json"))


def test_unsaved_work_survives_a_trip_to_another_screen(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from creator_intel import demo
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    conn = store.open_db()
    demo.seed(conn)
    conn.close()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Script builder").run()
    at.text_input(key="script_title").set_value("Half-written idea")
    at.text_area(key="script_body").set_value("First line of a draft.")
    at.run()
    at.sidebar.radio(key="nav").set_value("Dashboard").run()
    at.sidebar.radio(key="nav").set_value("Script builder").run()
    assert not at.exception, at.exception
    assert at.text_input(key="script_title").value == "Half-written idea"
    assert at.text_area(key="script_body").value == "First line of a draft."
