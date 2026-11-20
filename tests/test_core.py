"""Pure-logic checks. The numbers come straight from the blueprints' worked examples."""
from datetime import datetime, timedelta, timezone

import pytest

from creator_intel import metrics, ranking
from creator_intel.urls import parse_url

OWNER = {"audience_promise": "learn_one_repair", "format_subtype": "demonstration",
         "topic": "sewing_repair", "delivery": "calm"}
RELATED = {"topic": [["sewing_repair", "clothing_care"]]}


# --- ranking (Creator Comparison blueprint p.4) ---------------------------------

def test_peer_a_scores_88_2_at_85_percent_coverage():
    peer = {"audience_promise": "learn_one_repair", "format_subtype": "demonstration",
            "topic": "clothing_care", "production_setup": "single_camera"}  # delivery unknown
    s = ranking.score(OWNER, "single_camera", peer, RELATED)
    assert s.coverage == pytest.approx(0.85)
    assert round(s.fit, 1) == 88.2
    assert s.match["delivery"] is None and s.match["topic"] == 0.5


def test_peer_b_topic_only_scores_20():
    peer = {"audience_promise": "entertain", "format_subtype": "vlog", "topic": "sewing_repair",
            "delivery": "energetic", "production_setup": "studio_crew"}
    s = ranking.score(OWNER, "single_camera", peer, RELATED)
    assert s.coverage == pytest.approx(1.0)
    assert round(s.fit, 1) == 20.0


def test_no_observed_dimensions_gives_no_fit_not_zero_division():
    s = ranking.score(OWNER, "single_camera", {}, {})
    assert s.fit is None and s.coverage == 0


def test_stretch_when_format_fits_but_setup_exceeds_budget():
    peer = {"format_subtype": "demonstration", "production_setup": "multi_camera"}
    s = ranking.score(OWNER, "phone_handheld", peer, {})
    assert s.stretch and s.match["feasibility"] == 0
    one_up = ranking.score(OWNER, "single_camera", peer, {})
    assert one_up.stretch and one_up.match["feasibility"] == 0.5


def test_aggregate_needs_two_supporting_posts_and_no_tie():
    posts = [{"topic": "a", "delivery": "calm"}, {"topic": "a", "delivery": "energetic"},
             {"topic": "b", "opening": "result_first"}]
    agg = ranking.aggregate(posts)
    assert agg == {"topic": "a"}  # delivery tied 1-1, opening has one supporter


def test_shortlist_gates_sparse_candidates_and_sorts_by_fit_coverage_posts():
    def cand(i, fit, cov, posts):
        return ranking.Cand(i, posts, ranking.Score(fit, cov, {}, False))
    top, sparse = ranking.shortlist([
        cand(1, 90, 0.85, 3), cand(2, 90, 1.0, 3), cand(3, 99, 0.79, 5), cand(4, 99, 1.0, 2),
        cand(5, 90, 1.0, 5), cand(6, 50, 1.0, 3), cand(7, 40, 1.0, 3), cand(8, 30, 1.0, 3),
    ])
    assert [c.account_id for c in top] == [5, 2, 1, 6, 7]  # max five
    assert {c.account_id for c in sparse} == {3, 4}  # low coverage, too few posts


def test_experiment_idea_comes_from_repeated_peer_opening():
    idea = ranking.suggest_experiment("problem_first", ["result_first", "result_first", "result_first", "question"])
    assert "result_first" in idea and "3" in idea
    assert ranking.suggest_experiment("result_first", ["result_first", "result_first"]) is None


# --- metrics (Growth Engine p.6, Creator Comparison p.6) -------------------------

def test_saves_per_1000_and_undefined_reach():
    assert metrics.saves_per_1000(18, 900) == 20
    assert metrics.saves_per_1000(18, 0) is None
    assert metrics.saves_per_1000(18, None) is None


def test_outlier_uses_median_of_last_15_and_needs_history():
    assert metrics.outlier(3000, [1000] * 20) == 3.0
    assert metrics.outlier(3000, [9e9] * 5 + [1000] * 15) == 3.0  # only the last 15 count
    assert metrics.outlier(3000, [1000, 1000]) is None
    assert metrics.outlier(None, [1000] * 15) is None


def test_breakout_rules_per_platform():
    hist = [1000] * 15
    assert metrics.is_breakout("youtube_long", views=2500, history=hist, subs=5000) is True
    assert metrics.is_breakout("youtube_long", views=2500, history=hist, subs=6000) is False  # V/S < 0.5
    assert metrics.is_breakout("youtube_long", views=2500, history=hist) is None  # subs unknown
    assert metrics.is_breakout("youtube_short", views=3000, history=hist, age_days=6) is True
    assert metrics.is_breakout("youtube_short", views=3000, history=hist, age_days=9) is False
    # weighted engagement = (100 + 2*20 + 4*30 + 3*40) / 3000 = 12.67%
    tk = dict(views=3000, history=hist, likes=100, comments=20, shares=30, saves=40)
    assert metrics.is_breakout("tiktok", **tk) is True
    assert metrics.is_breakout("tiktok", **{**tk, "shares": 0}) is False
    assert metrics.is_breakout("instagram_reel", views=2000, followers=1000) is True
    assert metrics.is_breakout("instagram_reel", views=2000, followers=0) is None


def test_trial_decision_rule():
    assert metrics.trial_decision([(10, 12), (10, 11), (10, 13), (10, 9)]) == "repeat"
    assert metrics.trial_decision([(10, 12), (10, 11), (10, 9), (10, 9)]) == "stop"
    assert metrics.trial_decision([(10, 12), (10, 11), (10, 13), (10, None)]) == "incomplete"
    assert metrics.trial_decision([(10, 12)]) == "incomplete"


def test_arbitrage_flags_topics_hot_on_tiktok_and_absent_elsewhere():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    recent = (now - timedelta(days=3)).isoformat()
    rows = [{"platform": "tiktok", "account_id": i, "topic": "visible_mending", "published_at": recent, "breakout": True}
            for i in range(3)]
    rows += [{"platform": "tiktok", "account_id": i, "topic": "hemming", "published_at": recent, "breakout": True}
             for i in range(3)]
    rows += [{"platform": "youtube", "account_id": 9, "topic": "hemming", "published_at": recent, "breakout": False}]
    rows += [{"platform": "tiktok", "account_id": 1, "topic": "old", "published_at": "2026-01-01T00:00:00+00:00", "breakout": True}] * 3
    assert metrics.arbitrage(rows, now) == ["visible_mending"]


# --- urls (trust boundary: everything a reviewer or search broker hands us) ------

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/@SewSimple/videos", ("youtube", "account", "@sewsimple")),
    ("youtube.com/channel/UCabcdefghijklmnopqrstuv", ("youtube", "account", "UCabcdefghijklmnopqrstuv")),
    ("https://youtu.be/dQw4w9WgXcQ?t=3", ("youtube", "post", "dQw4w9WgXcQ")),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=x", ("youtube", "post", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/shorts/abcdefghijk", ("youtube", "post", "abcdefghijk")),
    ("https://www.tiktok.com/@Mend.It?lang=en", ("tiktok", "account", "@mend.it")),
    ("https://www.tiktok.com/@mend.it/video/7300000000000000000", ("tiktok", "post", "7300000000000000000")),
    ("https://www.instagram.com/fix_a_hem/", ("instagram", "account", "@fix_a_hem")),
    ("https://www.instagram.com/reel/Cxyz_12-AB/", ("instagram", "post", "Cxyz_12-AB")),
])
def test_parse_url(url, expected):
    ref = parse_url(url)
    assert (ref.platform, ref.kind, ref.external_id) == expected
    assert ref.url.startswith("https://")


def test_short_and_tiktok_urls_carry_format_and_owner_handle():
    assert parse_url("https://www.youtube.com/shorts/abcdefghijk").format_family == "short"
    assert parse_url("https://www.tiktok.com/@mend.it/video/7300000000000000000").handle == "@mend.it"


@pytest.mark.parametrize("bad", [
    "", "not a url", "https://evil.example/@x", "https://youtube.com.evil.example/@x",
    "javascript:alert(1)", "https://www.instagram.com/explore/tags/sewing/", "https://www.youtube.com/",
])
def test_parse_url_rejects_unsupported(bad):
    with pytest.raises(ValueError):
        parse_url(bad)
