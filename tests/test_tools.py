"""Analyzer, viewer data, pre-publish checker, alerts and command-line tools."""
import csv
import shutil

import httpx
import pytest
from PIL import Image

from creator_intel import __main__ as cli
from creator_intel import adapters, analysis, db as store, labeling, pipeline, prepublish, ranking
from creator_intel.taxonomy import Brief
from creator_intel.urls import parse_url

OWNED = Brief(name="b", platform="youtube", topics=["sewing_repair", "clothing_care"],
              audience_promises=["learn_one_repair", "entertain"],
              related={"topic": [["sewing_repair", "clothing_care"]]},
              owner={"audience_promise": "learn_one_repair", "format_subtype": "demonstration",
                     "topic": "sewing_repair"})
STRONG = {"audience_promise": "learn_one_repair", "format_subtype": "demonstration", "topic": "clothing_care",
          "production_setup": "single_camera"}
OFF = {"audience_promise": "entertain", "format_subtype": "vlog", "topic": "sewing_repair",
       "production_setup": "single_camera"}


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def candidate(db, brief_id, handle, labels, posts=3, text="Shows the split hem, then one stitch."):
    pipeline.import_links(db, brief_id, [f"https://www.youtube.com/@{handle}"])
    acc = db.execute("select id from account where handle = ?", (f"@{handle}",)).fetchone()[0]
    for n in range(posts):
        post = store.upsert_post(db, acc, parse_url(f"https://youtu.be/{acc:03d}{n:08d}"),
                                 published_at=f"2026-11-{n + 1:02d}T00:00:00+00:00")
        ev = store.add_evidence(db, post, "reviewer_note", text)
        for dim, value in labels.items():
            store.set_tag(db, ev, dim, value)
    return acc


@pytest.fixture
def run(db):
    brief_id = store.save_brief(db, OWNED.model_copy())
    strong = [candidate(db, brief_id, f"s{i}", STRONG) for i in range(5)]
    off = [candidate(db, brief_id, f"t{i}", OFF) for i in range(2)]
    return {"brief": brief_id, "run": pipeline.rank(db, brief_id), "strong": strong, "off": off}


# --- analyzer: the week 3 launch gate ---------------------------------------------------------

def test_evaluation_compares_the_rule_with_topic_only_and_holds_the_gate_until_all_are_judged(db, run):
    s = analysis.suggestions(db, run["run"])
    assert set(s["rule"]) == set(run["strong"])
    assert s["topic_only"][:2] == run["off"] and len(s["topic_only"]) == 5

    pending = analysis.evaluation(db, [run["run"]])
    assert pending["gate"] is None and pending["missing"] == 7

    for reviewer in ("ana", "ben"):
        for acc in run["strong"]:
            analysis.judge(db, run["run"], acc, reviewer, True)
        for acc in run["off"]:
            analysis.judge(db, run["run"], acc, reviewer, False)
    done = analysis.evaluation(db, [run["run"]])
    assert done["rule"] == {"suggested": 5, "accepted": 5, "rate": 1.0}
    assert done["topic_only"] == {"suggested": 5, "accepted": 3, "rate": 0.6}
    assert done["agreement"] == 1.0 and done["traceable"] and done["gate"] is True and done["missing"] == 0

    analysis.judge(db, run["run"], run["strong"][0], "ben", False)  # reviewers now disagree on one creator
    split = analysis.evaluation(db, [run["run"]])
    assert split["rule"]["accepted"] == 4 and split["rule"]["rate"] == 0.8
    assert split["agreement"] == pytest.approx(6 / 7) and split["gate"] is True

    for acc in run["strong"][1:3]:
        analysis.judge(db, run["run"], acc, "ana", False)
    assert analysis.evaluation(db, [run["run"]])["gate"] is False  # 2 of 5 accepted: below 80%


def test_judgments_need_a_reviewer_and_a_creator_from_that_run(db, run):
    with pytest.raises(ValueError):
        analysis.judge(db, run["run"], run["strong"][0], "  ", True)
    with pytest.raises(ValueError):
        analysis.judge(db, run["run"], 9999, "ana", True)


def test_coverage_gaps_show_which_dimension_reviewers_leave_unknown(db, run):
    gaps = analysis.coverage_gaps(db, run["run"])
    assert gaps["delivery"] == 7 and gaps["topic"] == 0


def test_label_quality_counts_what_happened_to_ai_labels(db, run):
    evs = [r[0] for r in db.execute("select id from evidence order by id limit 4")]
    for ev in evs:
        store.set_tag(db, ev, "opening", "result_first", model_version="claude", review_state="needs_review")
    store.set_tag(db, evs[0], "opening", "result_first")  # reviewer confirms
    store.set_tag(db, evs[1], "opening", "question")  # reviewer corrects
    store.set_tag(db, evs[2], "opening", "result_first", review_state="rejected")
    store.set_tag(db, evs[0], "duration_band", "30_60s", model_version="claude", review_state="accepted")
    assert analysis.label_quality(db) == {"auto_accepted": 1, "confirmed": 1, "corrected": 1, "rejected": 1,
                                          "waiting": 1}


def test_costs_come_from_recorded_usage(db, run, monkeypatch):
    monkeypatch.setenv("CI_BRAVE_USD_PER_REQUEST", "0.005")
    store.record_usage(db, "claude_input_tokens", 1_000_000)
    store.record_usage(db, "claude_output_tokens", 100_000)
    store.record_usage(db, "brave_requests", 10)
    got = analysis.costs(db, accepted=3)
    assert got["claude_usd"] == pytest.approx(6.0) and got["brave_usd"] == pytest.approx(0.05)
    assert got["per_accepted_peer"] == pytest.approx(6.05 / 3)
    assert analysis.costs(db, accepted=0)["per_accepted_peer"] is None


# --- viewer data --------------------------------------------------------------------------------

def test_creator_view_gathers_profile_posts_labels_and_metric_history(db, run):
    acc = run["strong"][0]
    post = db.execute("select id from post where account_id = ? order by id", (acc,)).fetchone()[0]
    store.add_snapshot(db, post, "owner_export", views=100, captured_at="2026-11-02T00:00:00+00:00")
    store.add_snapshot(db, post, "owner_export", views=250, captured_at="2026-11-05T00:00:00+00:00")
    view = analysis.creator_view(db, acc)
    assert view["handle"] == "@s0" and view["profile"]["topic"] == "clothing_care" and len(view["posts"]) == 3
    first = next(p for p in view["posts"] if p["id"] == post)
    assert first["labels"]["format_subtype"] == "demonstration"
    assert [(s["captured_at"][:10], s["views"]) for s in first["snapshots"]] == [("2026-11-02", 100), ("2026-11-05", 250)]


def test_cards_say_how_a_creators_audience_compares_with_yours(db, run):
    store.upsert_account(db, parse_url("https://www.youtube.com/@me"), is_owner=1, follower_count=1000)
    db.execute("update account set follower_count = 3000 where id = ?", (run["strong"][0],))
    db.execute("update account set follower_count = 50000 where id = ?", (run["strong"][1],))
    cards = pipeline.all_cards(db, run["run"])
    assert cards[run["strong"][0]]["size_ratio"] == 3.0 and cards[run["strong"][2]]["size_ratio"] is None
    assert "3" in ranking.size_note(3.0) and "outside" in ranking.size_note(50.0) and ranking.size_note(None) is None


def test_breakouts_can_be_judged_at_a_fixed_post_age(db):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    for n, views in enumerate([1000, 1000, 1000, 3000]):
        post = store.upsert_post(db, acc, parse_url(f"https://www.youtube.com/shorts/aaaaaaaaaa{n}"),
                                 published_at=f"2026-10-{n + 1:02d}T00:00:00+00:00")
        store.add_snapshot(db, post, "owner_export", views=views, captured_at=f"2026-10-{n + 7:02d}T00:00:00+00:00")
    store.add_snapshot(db, post, "owner_export", views=9000, captured_at="2026-11-15T00:00:00+00:00")
    latest = pipeline.breakouts(db)[-1]
    assert latest["views"] == 9000 and latest["breakout"] is False  # judged at day 42: past the 7-day window
    at_seven = pipeline.breakouts(db, at_days=7)[-1]
    assert at_seven["views"] == 3000 and at_seven["outlier"] == 3.0 and at_seven["breakout"] is True


# --- pre-publish checker --------------------------------------------------------------------------

def levels(findings):
    return {f["check"]: f["level"] for f in findings}


def test_title_checks():
    assert levels(prepublish.check_title("youtube", "x" * 101))["title length"] == "fail"
    assert levels(prepublish.check_title("youtube", "x" * 80))["title length"] == "warn"
    assert levels(prepublish.check_title("youtube", "Fix a split hem in 30 seconds"))["title length"] == "ok"
    assert levels(prepublish.check_title("instagram", "  "))["title length"] == "fail"
    with pytest.raises(ValueError):
        prepublish.check_title("myspace", "x")


def test_thumbnail_contrast_at_small_size_and_aspect():
    flat = Image.new("RGB", (1280, 720), (120, 120, 120))
    bold = Image.new("RGB", (1280, 720), (0, 0, 0))
    bold.paste((255, 255, 255), (0, 0, 640, 720))
    assert levels(prepublish.check_image("youtube", flat))["contrast at 120px"] == "fail"
    got = levels(prepublish.check_image("youtube", bold))
    assert got["contrast at 120px"] == "ok" and got["aspect ratio"] == "ok"
    assert levels(prepublish.check_image("tiktok", bold))["aspect ratio"] == "warn"  # landscape cover on a vertical feed


def test_safe_zone_overlay_marks_the_areas_the_app_covers():
    cover = Image.new("RGB", (1080, 1920), (10, 200, 10))
    marked = prepublish.safe_zone_overlay(cover)
    assert marked.size == cover.size
    assert marked.getpixel((540, 1880)) != (10, 200, 10)  # bottom caption band
    assert marked.getpixel((1040, 1300)) != (10, 200, 10)  # right-hand buttons
    assert marked.getpixel((300, 900)) == (10, 200, 10)  # the middle stays clear


def test_loudness_is_measured_when_ffmpeg_exists_and_unknown_when_it_does_not(tmp_path, monkeypatch):
    assert prepublish.parse_lufs("  Integrated loudness:\n    I:         -14.2 LUFS\n") == -14.2
    assert prepublish.parse_lufs("no summary here") is None
    if shutil.which("ffmpeg"):
        import subprocess
        tone = tmp_path / "tone.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(tone)],
                       check=True)
        assert -70 < prepublish.loudness(tone) < 0
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert prepublish.loudness(tmp_path / "anything.wav") is None
    assert levels(prepublish.check_audio(tmp_path / "anything.wav"))["loudness"] == "not checked"


def test_a_check_with_nothing_to_look_at_says_not_checked_instead_of_ok():
    got = levels(prepublish.check("tiktok", "A fine title"))
    assert got["title length"] == "ok" and got["thumbnail"] == "not checked" and got["loudness"] == "not checked"


# --- alerts ---------------------------------------------------------------------------------------

def test_webhook_posts_the_shape_each_service_expects_and_only_over_https():
    sent = []

    def handler(request):
        sent.append((str(request.url), request.read().decode()))
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapters.post_webhook("https://discord.com/api/webhooks/1/abc", "hello " + "x" * 3000, client=client)
    adapters.post_webhook("https://hooks.slack.com/services/T/B/C", "hello", client=client)
    assert sent[0][1].startswith('{"content":') and len(sent[0][1]) < 2100
    assert sent[1][1] == '{"text":"hello"}'
    for bad in ("http://discord.com/api/webhooks/1/abc", "https://evil.example/hook", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            adapters.post_webhook(bad, "x", client=client)
    assert len(sent) == 2


def test_webhook_errors_never_contain_the_secret_url():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(adapters.SourceError) as err:
        adapters.post_webhook("https://discord.com/api/webhooks/1/SECRETTOKEN", "x", client=client)
    assert "SECRETTOKEN" not in str(err.value)


# --- command-line tools ---------------------------------------------------------------------------------

def test_doctor_reports_each_connector_and_survives_denied_access(db, monkeypatch):
    for name in ("BRAVE_API_KEY", "YOUTUBE_API_KEY", "CI_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    quiet = {r["name"]: r["status"] for r in analysis.doctor(db)}
    assert quiet["Brave search"] == "no key set" and quiet["YouTube"] == "no key set"
    assert quiet["Database"].startswith("ok")

    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")

    def denied(*a, **k):
        raise adapters.AccessDenied("api.search.brave.com refused access (403)")

    monkeypatch.setattr(adapters, "brave_search", denied)
    monkeypatch.setattr(adapters, "youtube_videos", lambda ids, key: [{"id": ids[0], "views": 5}])
    monkeypatch.setattr(labeling, "check_access", lambda: "ok: stubbed")
    assert {r["name"]: r["status"] for r in analysis.doctor(db)}["Brave search"] == "key set (not tested)"
    live = {r["name"]: r["status"] for r in analysis.doctor(db, live=True)}
    assert "refused" in live["Brave search"] and live["YouTube"].startswith("ok")


def test_label_export_is_safe_to_open_in_a_spreadsheet(db, tmp_path):
    brief_id = store.save_brief(db, OWNED.model_copy())
    candidate(db, brief_id, "a", {"topic": "sewing_repair"}, posts=1, text='=HYPERLINK("https://evil.example","x")')
    out = tmp_path / "labels.csv"
    assert analysis.export_labels(db, brief_id, out) == 1
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert rows[0]["creator"] == "a" and rows[0]["value"] == "sewing_repair"
    assert rows[0]["evidence"].startswith("'=")


def test_cli_commands_run_against_a_fresh_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CI_DB", str(tmp_path / "cli.db"))
    for name in ("BRAVE_API_KEY", "YOUTUBE_API_KEY", "CI_WEBHOOK_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    def no_credential(db, brief_id):
        raise labeling.NoCredential("no Anthropic credential")

    monkeypatch.setattr(labeling, "label_pending", no_credential)  # never call the model from a test
    assert cli.main(["demo"]) == 0
    assert cli.main(["doctor"]) == 0
    assert cli.main(["evaluate"]) == 0
    assert cli.main(["export-labels", "1", str(tmp_path / "l.csv")]) == 0
    assert cli.main(["weekly", "1", "-o", str(tmp_path / "digest.md")]) == 0
    assert "fictional-mend-02" in (tmp_path / "digest.md").read_text(encoding="utf-8")
    assert cli.main(["prepublish", "--platform", "youtube", "--title", "x" * 120]) == 1
    assert "title length" in capsys.readouterr().out


def test_app_judging_and_draft_check_work_when_clicked(tmp_path, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from creator_intel import demo
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    conn = store.open_db()
    brief_id = demo.seed(conn)
    run_id = pipeline.latest_run(conn, brief_id)
    account_id = analysis.suggestions(conn, run_id)["rule"][0]

    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"), default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Analysis").run()
    assert not at.exception, at.exception
    at.text_input(key="reviewer").set_value("Ana").run()
    at.button(key=f"acc_{account_id}").click().run()
    assert not at.exception, at.exception
    assert [tuple(r) for r in conn.execute("select account_id, reviewer, accept from judgment")] == [
        (account_id, "ana", 1)]

    at.sidebar.radio(key="nav").set_value("Pre-publish").run()
    at.text_input(key="pp_title").set_value("x" * 120).run()
    next(b for b in at.button if b.label == "Check draft").click().run()
    assert not at.exception, at.exception
    conn.close()


# --- findings from the security review of this round ------------------------------------------------

def test_discord_alert_cannot_ping_everyone():
    sent = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: sent.append(r.read().decode()) or httpx.Response(204)))
    adapters.post_webhook("https://discord.com/api/webhooks/1/abc", "Shortlist: @everyone", client=client)
    assert '"allowed_mentions":{"parse":[]}' in sent[0]


def test_unreadable_and_oversized_images_fail_cleanly(tmp_path):
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really a png" * 10)
    assert levels(prepublish.check("youtube", "ok title", junk))["thumbnail"] == "fail"
    huge = tmp_path / "huge.png"
    Image.new("1", (8000, 8000)).save(huge)  # 64 million pixels in a few kilobytes
    found = prepublish.check("youtube", "ok title", huge)
    assert levels(found)["thumbnail"] == "fail" and "contrast at 120px" not in levels(found)


def test_ffmpeg_is_confined_to_local_files_and_given_no_keyboard(tmp_path, monkeypatch):
    import subprocess
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, "", "    I:         -15.0 LUFS\n")

    monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "bin" / "ffmpeg"))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert prepublish.loudness(tmp_path / "a.wav") == -15.0
    cmd = seen["cmd"]
    assert "-nostdin" in cmd and cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    assert cmd.index("-protocol_whitelist") < cmd.index("-i") and seen["kw"].get("shell") is not True


def test_spreadsheet_guard_also_catches_formulas_behind_leading_whitespace():
    assert analysis._cell(" =1+1") == "' =1+1" and analysis._cell("\n@x") == "'\n@x"
    assert analysis._cell("plain text") == "plain text" and analysis._cell(12) == "12"


# --- findings from the correctness review of this round -----------------------------------------------

def test_flagging_an_ai_label_puts_it_back_in_the_queue_and_its_final_fate_is_counted(db, run):
    ev = db.execute("select id from evidence order by id").fetchone()[0]
    store.set_tag(db, ev, "opening", "result_first", model_version="claude", review_state="accepted")
    store.flag_tag(db, ev, "opening")  # the "is wrong" button on the shortlist
    assert analysis.label_quality(db)["waiting"] == 1 and analysis.label_quality(db)["rejected"] == 0
    store.set_tag(db, ev, "opening", "question")  # the reviewer then corrects it in the queue
    quality = analysis.label_quality(db)
    assert (quality["corrected"], quality["rejected"], quality["waiting"]) == (1, 0, 0)
    store.flag_tag(db, ev, "opening")  # flagging a reviewer's own label logs nothing more
    assert analysis.label_quality(db)["corrected"] == 1


def test_a_short_read_just_under_eight_days_after_publishing_is_outside_the_seven_day_window(db):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    for n, views in enumerate([1000, 1000, 1000, 3000]):
        post = store.upsert_post(db, acc, parse_url(f"https://www.youtube.com/shorts/aaaaaaaaaa{n}"),
                                 published_at=f"2026-10-{n + 1:02d}T00:00:00+00:00")
        store.add_snapshot(db, post, "owner_export", views=views, captured_at=f"2026-10-{n + 8:02d}T23:00:00+00:00")
    assert pipeline.breakouts(db)[-1]["breakout"] is False  # 7 days 23 hours


def test_reading_times_are_validated_and_stored_in_utc(db):
    acc = store.upsert_account(db, parse_url("https://www.youtube.com/@a"))
    post = store.upsert_post(db, acc, parse_url("https://youtu.be/abcdefghijk"))
    with pytest.raises(ValueError):
        store.add_snapshot(db, post, "owner_export", views=1, captured_at="yesterday")
    store.add_snapshot(db, post, "owner_export", views=1, captured_at="2026-11-02T02:00:00+02:00")
    assert db.execute("select captured_at from metric_snapshot").fetchone()[0] == "2026-11-02T00:00:00+00:00"
    assert db.execute("select likes from metric_snapshot").fetchone()[0] is None  # not supplied stays unknown


def test_verdicts_survive_a_new_ranking_of_the_same_brief(db, run):
    for reviewer in ("ana", "ben"):
        for acc in run["strong"] + run["off"]:
            analysis.judge(db, run["run"], acc, reviewer, acc in run["strong"])
    again = pipeline.rank(db, run["brief"])
    assert again != run["run"] and analysis.evaluation(db, [again])["gate"] is True


def test_cost_report_and_cli_survive_bad_settings_and_files(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CI_BRAVE_USD_PER_REQUEST", "abc")
    store.record_usage(db, "brave_requests", 10)
    assert analysis.costs(db)["brave_usd"] == 0
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image")
    code = cli.main(["prepublish", "--platform", "youtube", "--title", "fine", "--image", str(junk),
                     "--overlay", str(tmp_path / "o.png")])
    assert code == 1 and "overlay not written" in capsys.readouterr().out
