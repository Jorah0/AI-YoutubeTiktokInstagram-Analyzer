"""Round four: one theme scraped on every platform, recommendations tailored platform by platform and
layer by layer, any AI model behind ask(), questions answered from the brief's own data, and a small
model trained on the catalog. Nothing here calls a network service or a model."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from creator_intel import adapters, assistant, broker, db as store, demo, labeling, learner, tracker
from creator_intel.taxonomy import Brief

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TT = {"webVideoUrl": "https://www.tiktok.com/@mend.it/video/7300000000000000001", "text": "Fix a hem fast",
      "createTimeISO": "2026-11-10T10:00:00.000Z", "authorMeta": {"name": "mend.it"}, "playCount": 50000}
YT = {"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Fix a hem", "date": "2026-11-01T10:00:00.000Z",
      "channelUrl": "https://www.youtube.com/@SewSimple", "viewCount": 8000}
DEFAULT_TUNING = {"off_platforms": [], "priority": None, "off_layers": []}


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def brief(db):
    return store.save_brief(db, Brief(name="b", platform="tiktok", topics=["mending"], audience_promises=["learn"]))


def video(platform, handle, n, views, days_ago, text=None, **extra):
    if platform == "tiktok":
        vid = f"{sum(map(ord, handle)):05d}{n:014d}"
        url, profile = f"https://www.tiktok.com/@{handle}/video/{vid}", f"https://www.tiktok.com/@{handle}"
    else:
        vid = f"{handle[:6]:_<6}{n:05d}"
        url, profile = f"https://www.youtube.com/watch?v={vid}", f"https://www.youtube.com/@{handle}"
    return {"platform": platform, "external_id": vid, "url": url, "handle": f"@{handle}", "profile_url": profile,
            "text": text or f"{handle} video {n}", "published_at": (NOW - timedelta(days=days_ago)).isoformat(),
            "duration_sec": 30, "views": views, "likes": None, "comments": None, "shares": None, "saves": None,
            "followers": None, "captured_at": NOW.isoformat(), **extra}


def learnable(db, brief_id, creators):
    """Each creator: three steady videos and one hit, and only the hit's caption says "quick hem trick"."""
    for i in range(creators):
        steady = [video("tiktok", f"c{i}", k, 1000 * age, age, text=f"daily sewing vlog {k}")
                  for k, age in enumerate((10, 8, 6), start=1)]
        broker.intake(db, brief_id, steady + [video("tiktok", f"c{i}", 4, 9000, 3, text="quick hem trick")], "theme", "mending")


# --- every platform in one go ---------------------------------------------------------------------------

def test_one_theme_is_scraped_on_each_chosen_platform_and_a_failure_keeps_the_rest(db, brief, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")

    def run(actor, payload, token):
        if "instagram" in actor:
            raise adapters.SourceError("api.apify.com answered 500")
        return [TT] if "tiktok" in actor else [YT]

    got = broker.scrape_many(db, brief, ["tiktok", "instagram", "youtube", "tiktok", "vimeo"], "mending", run=run)
    assert list(got) == ["tiktok", "instagram", "youtube", "vimeo"]
    assert got["tiktok"]["added"] == 1 and got["youtube"]["added"] == 1
    assert "500" in got["instagram"]["error"] and "unknown platform" in got["vimeo"]["error"]
    assert db.execute("select count(*) from catalog").fetchone()[0] == 2
    monkeypatch.delenv("APIFY_TOKEN")
    with pytest.raises(broker.NoToken):
        broker.scrape_many(db, brief, ["tiktok"], "mending", run=run)


def test_a_creator_without_history_is_compared_with_creators_on_the_same_platform_only(db, brief):
    for handle, per_day in (("ana", 100), ("ben", 100), ("cy", 100), ("dee", 300)):
        broker.intake(db, brief, [video("tiktok", handle, 1, per_day * 4, 4)], "theme", "mending")
    for handle in ("yta", "ytb", "ytc"):  # another platform's view counts are another scale
        broker.intake(db, brief, [video("youtube", handle, 1, 400_000, 4)], "theme", "mending")
    dee = next(r for r in tracker.rows(db, brief) if r["handle"] == "@dee")
    assert dee["theme_ratio"] == 3.0 and dee["tier"] == "outlier"


# --- tailoring: platforms and layers ------------------------------------------------------------------------

def test_tuning_is_kept_per_brief_and_refuses_nonsense(db, brief):
    assert tracker.get_tuning(db, brief) == DEFAULT_TUNING
    tracker.set_tuning(db, brief, off_platforms=["youtube"], priority="tiktok", off_layers=["length"])
    assert tracker.get_tuning(db, brief) == {"off_platforms": ["youtube"], "priority": "tiktok", "off_layers": ["length"]}
    other = store.save_brief(db, Brief(name="o", platform="youtube"))
    assert tracker.get_tuning(db, other) == DEFAULT_TUNING
    for bad in ({"off_platforms": ["vimeo"]}, {"priority": "youtube", "off_platforms": ["youtube"]},
                {"off_layers": ["luck"]}, {"off_platforms": ["youtube", "tiktok", "instagram"]}, {"priority": "vimeo"}):
        with pytest.raises(ValueError):
            tracker.set_tuning(db, brief, **bad)
    assert tracker.get_tuning(db, brief)["priority"] == "tiktok"  # a refused change leaves the old one in place


def row(platform, origin, tier, per_day, **more):
    return {"platform": platform, "origin": origin, "tier": tier, "views_per_day": per_day, "engagement": None,
            "share_rate": None, "save_rate": None, "reach": None, "duration_sec": None, "title_words": None, **more}


def layer(compared, key):
    return next(found for found in compared["layers"] if found["key"] == key)


def test_recommendations_use_only_the_platforms_left_on_and_the_one_put_first():
    mine = [row("tiktok", "own", "typical", 100)] * 3 + [row("youtube", "own", "typical", 900)] * 3
    best = ([row("tiktok", "theme", "outlier", 2000)] * 3 + [row("youtube", "theme", "outlier", 50000)] * 3
            + [row("instagram", "theme", "outlier", 7000)] * 2)
    everything = layer(tracker.compare(mine + best), "speed")
    assert (everything["yours"], everything["outliers"]) == ("500", "7,000")
    no_youtube = tracker.compare(mine + best, {**DEFAULT_TUNING, "off_platforms": ["youtube"]})
    assert (layer(no_youtube, "speed")["yours"], layer(no_youtube, "speed")["outliers"]) == ("100", "2,000")
    assert (no_youtube["mine"], no_youtube["outliers"]) == (3, 5)
    first = tracker.compare(mine + best, {**DEFAULT_TUNING, "priority": "tiktok"})
    assert layer(first, "speed")["outliers"] == "2,000" and layer(first, "speed")["yours"] == "100"
    assert "TikTok" in first["basis"]
    thin = tracker.compare(mine + best, {**DEFAULT_TUNING, "priority": "instagram"})
    assert layer(thin, "speed")["outliers"] == "7,000" and "2 outliers" in thin["basis"]  # says why it was not used alone


def test_each_layer_says_whether_it_could_be_checked_and_a_switched_off_layer_is_left_out():
    mine = [row("tiktok", "own", "typical", 100, engagement=0.02, duration_sec=60)] * 3
    best = ([row("tiktok", "theme", "outlier", 3000, engagement=0.08, duration_sec=30)] * 2
            + [row("tiktok", "theme", "outlier", 3000, duration_sec=30)])
    tuning = {**DEFAULT_TUNING, "off_layers": ["length"]}
    got = {found["key"]: found for found in tracker.compare(mine + best, tuning)["layers"]}
    assert got["speed"]["status"] == "checked" and (got["speed"]["yours_n"], got["speed"]["outliers_n"]) == (3, 3)
    assert got["engagement"]["status"].startswith("not checked") and "2" in got["engagement"]["status"]
    assert got["shares"]["status"].startswith("not checked") and got["shares"]["outliers"] == "unknown"
    assert got["length"]["status"] == "switched off"
    table = {r["factor"]: r for r in tracker.improvements(mine + best, tuning)}
    assert "length" not in table and table["engagement"]["gap"].startswith("not checked")
    assert "30.0x" in table["views per day"]["gap"]
    assert {r["factor"] for r in tracker.improvements(mine + best)} >= {"length", "views per day", "engagement", "share rate"}


def test_the_ai_reading_respects_the_tailoring(db, brief):
    learnable(db, brief, 1)
    broker.intake(db, brief, [video("tiktok", "me", n, 300, 3) for n in (1, 2, 3)], "own")
    broker.intake(db, brief, [video("youtube", "meyt", n, 300, 3, text="my long video") for n in (1, 2, 3)], "own")
    tracker.set_tuning(db, brief, off_platforms=["youtube"], off_layers=["length"])
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        return tracker.Advice(summary="s", priorities=[])

    tracker.ai_improvements(db, brief, call=fake)
    assert "my long video" not in seen["user"] and "duration_sec" not in seen["user"] and "youtube" not in seen["user"]
    assert "quick hem trick" in seen["user"]


# --- any AI model behind ask() ----------------------------------------------------------------------------

class Reply:
    content = b""

    def __init__(self, body=None, status=200):
        self.body, self.status_code = body, status

    def json(self):
        if self.body is None:
            raise ValueError("not json")
        return self.body


def said(text, **usage):
    return Reply({"choices": [{"message": {"content": text}}], "usage": usage})


@pytest.fixture
def local_ai(monkeypatch):
    monkeypatch.setenv("CI_AI_URL", "http://127.0.0.1:11434/v1/")
    monkeypatch.setenv("CI_AI_MODEL", "llama3.1")
    monkeypatch.setenv("CI_AI_KEY", "local-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")


def test_any_openai_compatible_server_can_stand_in_for_claude(db, local_ai, monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return said('<think>{"answer": 1}</think>Here you go:\n```json\n{"answer": "Post shorter.", "used": ["Video tracker"]}\n```',
                    prompt_tokens=120, completion_tokens=30)

    monkeypatch.setattr(labeling.httpx, "post", post)
    got = labeling.ask(db, "system words", "user words", assistant.Answer)
    assert got.answer == "Post shorter." and got.used == ["Video tracker"]
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen["headers"] == {"Authorization": "Bearer local-key"}  # and never the Anthropic key
    assert "anthropic-secret" not in json.dumps(seen["json"]) + json.dumps(seen["headers"])
    assert seen["json"]["model"] == "llama3.1" and seen["json"]["messages"][1] == {"role": "user", "content": "user words"}
    system = seen["json"]["messages"][0]["content"]
    assert system.startswith("system words") and '"answer"' in system  # the shape to answer in is spelled out
    assert dict(db.execute("select source, units from usage").fetchall()) == {
        "other_ai_input_tokens": 120, "other_ai_output_tokens": 30}
    assert labeling.describe() == "llama3.1 at 127.0.0.1:11434" and labeling.model_name() == "llama3.1"


def test_a_local_model_that_answers_badly_or_not_at_all_is_an_error_not_a_guess(db, local_ai, monkeypatch):
    for reply in (said("I cannot help with that."), said('{"answer": 5}'), Reply({"choices": []}), Reply(None),
                  Reply({"error": "boom"}, status=500), said("[1, 2]")):
        monkeypatch.setattr(labeling.httpx, "post", lambda url, reply=reply, **kw: reply)
        with pytest.raises(adapters.SourceError):
            labeling.ask(db, "s", "u", assistant.Answer)
    monkeypatch.setattr(labeling.httpx, "post", lambda url, **kw: Reply({}, status=401))
    with pytest.raises(labeling.NoCredential):
        labeling.ask(db, "s", "u", assistant.Answer)

    def down(url, **kw):
        raise httpx.ConnectError("refused local-key")

    monkeypatch.setattr(labeling.httpx, "post", down)
    with pytest.raises(adapters.SourceError) as failed:
        labeling.ask(db, "s", "u", assistant.Answer)
    assert "127.0.0.1" in str(failed.value) and "local-key" not in str(failed.value)


def test_the_ai_address_must_be_this_computer_or_encrypted_and_needs_a_model_name(db, local_ai, monkeypatch):
    def never(url, **kw):
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(labeling.httpx, "post", never)
    for bad in ("http://example.com/v1", "ftp://127.0.0.1/v1", "https://", "http://user:pw@127.0.0.1:1/v1",
                "http://127.0.0.1.example.com/v1"):
        monkeypatch.setenv("CI_AI_URL", bad)
        with pytest.raises(adapters.SourceError):
            labeling.ask(db, "s", "u", assistant.Answer)
        assert "pw" not in labeling.describe()
    monkeypatch.setenv("CI_AI_URL", "https://api.example.com/v1")
    monkeypatch.setenv("CI_AI_MODEL", "reviewer")
    assert labeling.model_name() == "other"  # a model can never sign its labels as a person
    monkeypatch.delenv("CI_AI_MODEL")
    with pytest.raises(labeling.NoCredential):
        labeling.ask(db, "s", "u", assistant.Answer)
    monkeypatch.delenv("CI_AI_URL")
    assert labeling.describe().startswith("Claude") and labeling.model_name() == labeling.MODEL


# --- questions answered from the brief's own data ---------------------------------------------------------

def test_questions_are_answered_from_the_briefs_own_numbers_without_captions_or_notes(db):
    brief_id = demo.seed(db)
    learnable(db, brief_id, 2)
    sections = assistant.facts(db, brief_id)
    assert {"Plan", "Shortlist", "Launch gate", "Video tracker", "Script patterns", "Own model"} <= set(sections)
    blob = json.dumps(sections)
    notes = [r[0] for r in db.execute("select text from evidence")]
    assert notes and not any(note in blob for note in notes) and "quick hem trick" not in blob
    got = assistant.answer(db, brief_id, "Which videos are outliers?")
    assert got["answer"] is None and list(got["sections"])[0] == "Video tracker"
    assert any("outlier" in line for line in got["sections"]["Video tracker"])
    assert set(assistant.answer(db, brief_id, "zzzz")["sections"]) == set(sections)  # nothing matched: show it all
    assert list(assistant.answer(db, brief_id, "", about="Shortlist")["sections"]) == ["Shortlist"]
    with pytest.raises(ValueError):
        assistant.answer(db, brief_id, "x", about="Nope")


def test_the_ai_answer_is_built_on_the_same_facts_and_is_bounded(db):
    brief_id = demo.seed(db)
    learnable(db, brief_id, 2)
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        return assistant.Answer(answer="Do this.\x1b[2J" + "y" * 5000, used=["Video tracker", "Made up"])

    got = assistant.answer(db, brief_id, "q" * 2000, call=fake)
    assert "\x1b" not in got["answer"] and len(got["answer"]) <= 1500
    assert got["used"] == ["Video tracker"] and list(got["sections"]) == ["Video tracker"]
    assert "q" * 501 not in seen["user"] and "quick hem trick" not in seen["user"] and '"Launch gate"' in seen["user"]
    assistant.answer(db, brief_id, "", about="Shortlist", call=fake)
    assert '"Shortlist"' in seen["user"] and '"Launch gate"' not in seen["user"]


# --- a model of your own, trained on the catalog --------------------------------------------------------------

def test_own_model_refuses_until_there_is_enough_and_then_reports_how_well_it_does(db, brief):
    learnable(db, brief, 3)
    early = learner.fit(db, brief)
    assert early["trained"] is False and "30" in early["why"] and early["examples"] == 12
    assert learner.score(db, brief, "quick hem trick") is None
    learnable(db, brief, 12)
    report = learner.fit(db, brief)
    assert report["trained"] and (report["examples"], report["fast"]) == (48, 12)
    assert report["baseline"] == 0.75 and report["accuracy"] > 0.9 and report["useful"] and report["with_captions"]
    hit = learner.score(db, brief, "A QUICK hem trick!", platform="tiktok", duration_sec=30)
    miss = learner.score(db, brief, "daily sewing vlog")
    assert hit["chance"] > 0.5 > miss["chance"] and hit["base"] == 0.25
    assert any("hem" in name for name, _ in hit["up"]) and any("vlog" in name for name, _ in miss["down"])
    assert learner.load(db, brief)["report"]["examples"] == 48


def test_a_model_that_does_no_better_than_guessing_says_so():
    report = learner.evaluate([({f"word:w{i}"}, i % 4 == 0, i) for i in range(40)])  # every caption unique: nothing to learn
    assert report["accuracy"] <= report["baseline"] == 0.75 and report["useful"] is False


def test_captions_stay_out_of_the_model_and_the_training_file_without_the_ai_right(db, brief, monkeypatch):
    learnable(db, brief, 12)
    lines = learner.training_examples(db, brief)
    assert len(lines) == 48 and "quick hem trick" in json.dumps(lines)
    assert {line["messages"][-1]["content"] for line in lines} == {"outlier", "typical"}
    assert [m["role"] for m in lines[0]["messages"]] == ["system", "user", "assistant"]
    monkeypatch.setitem(learner.RIGHTS, "broker", "store,derive")
    with pytest.raises(ValueError):
        learner.training_examples(db, brief)
    assert learner.fit(db, brief)["with_captions"] is False
    assert not any(name.startswith("word:") for name in learner.load(db, brief)["model"]["counts"])


def test_revoking_the_broker_removes_the_model_trained_on_its_data_and_keeps_your_tailoring(db, brief):
    learnable(db, brief, 12)
    learner.fit(db, brief)
    tracker.set_tuning(db, brief, off_layers=["length"])
    store.revoke_source(db, "broker")
    assert learner.load(db, brief) is None and learner.score(db, brief, "quick hem trick") is None
    assert tracker.get_tuning(db, brief)["off_layers"] == ["length"]


# --- commands and screens -------------------------------------------------------------------------------------

def test_ask_learn_export_and_all_platform_scrape_commands(tmp_path, monkeypatch, capsys):
    from creator_intel import __main__ as cli
    monkeypatch.setenv("CI_DB", str(tmp_path / "cli.db"))
    for name in ("APIFY_TOKEN", "CI_AI_URL"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["demo"]) == 0
    capsys.readouterr()
    assert cli.main(["ask", "1", "who is on the shortlist?"]) == 0
    assert "Shortlist" in capsys.readouterr().out
    assert cli.main(["learn", "1"]) == 0
    assert "Not trained" in capsys.readouterr().out
    assert cli.main(["scrape", "1", "--platform", "all", "--channel", "https://www.tiktok.com/@a"]) == 2
    assert cli.main(["scrape", "1", "--platform", "all", "--search", "mending"]) == 2
    assert "APIFY_TOKEN" in capsys.readouterr().out
    out = tmp_path / "train.jsonl"
    assert cli.main(["export-training", "1", str(out)]) == 0 and out.read_text(encoding="utf-8") == ""
    conn = store.open_db()
    learnable(conn, 1, 1)
    conn.close()
    assert cli.main(["export-training", "1", str(out)]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4


def test_assistant_screen_answers_and_the_tracker_keeps_the_tailoring(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    for name in ("APIFY_TOKEN", "CI_AI_URL"):
        monkeypatch.delenv(name, raising=False)
    conn = store.open_db()
    brief_id = demo.seed(conn)
    learnable(conn, brief_id, 3)
    broker.intake(conn, brief_id, [video("tiktok", "me", n, 300, 3) for n in (1, 2, 3)], "own")
    conn.close()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Assistant").run()
    assert not at.exception, at.exception
    at.text_input(key="ask_question").set_value("which videos are outliers?")
    at.button(key="ask_local").click().run()
    assert not at.exception, at.exception
    assert any("Video tracker" in m.value for m in at.markdown)
    at.button(key="model_train").click().run()
    assert not at.exception, at.exception
    assert any("30" in w.value for w in at.warning)  # 12 examples: says how many it needs

    at.sidebar.radio(key="nav").set_value("Video tracker").run()
    assert not at.exception, at.exception
    layers = at.multiselect(key=f"tune_layers_{brief_id}")
    layers.set_value([key for key in layers.value if key != "length"])
    at.button(key="tune_save").click().run()
    assert not at.exception, at.exception
    conn = store.open_db()
    assert tracker.get_tuning(conn, brief_id)["off_layers"] == ["length"]
    conn.close()


# --- findings from the two reviews of this round ---------------------------------------------------------

def test_the_models_self_test_never_shows_it_a_creator_it_trained_on():
    import random
    for seed in range(20):  # a signature word per creator and a coin-flip label: nothing that carries to a new creator
        flips = random.Random(seed)
        found = []
        for creator in range(12):
            fast = flips.random() < 0.4
            found += [({f"word:sig{creator}", "platform:tiktok"}, fast, creator)] * 3
        report = learner.evaluate(found)
        assert report["useful"] is False and report["accuracy"] <= report["baseline"] + 0.1, seed


def test_training_needs_several_creators_not_one_prolific_one(db, brief):
    steady = [video("tiktok", "solo", k, 1000 * 10, 10, text=f"daily sewing vlog {k}") for k in range(1, 31)]
    hits = [video("tiktok", "solo", k, 90000, 3, text="quick hem trick") for k in range(31, 41)]
    broker.intake(db, brief, steady + hits, "theme", "mending")
    report = learner.fit(db, brief)
    assert report["trained"] is False and "creators" in report["why"]


def test_the_platform_put_first_is_used_on_both_sides_or_on_neither():
    mine = [row("tiktok", "own", "typical", 100)] * 3 + [row("youtube", "own", "typical", 900)] * 3
    best = [row("tiktok", "theme", "outlier", 2000)] * 2 + [row("youtube", "theme", "outlier", 50000)] * 3
    got = tracker.compare(mine + best, {**DEFAULT_TUNING, "priority": "tiktok"})
    assert (got["mine"], got["outliers"]) == (6, 5)  # not your TikTok videos against everybody's outliers
    assert layer(got, "speed")["yours"] == "500" and "2 outliers" in got["basis"]


def test_switching_the_caption_layer_off_keeps_captions_from_the_ai_reading(db, brief):
    learnable(db, brief, 1)
    broker.intake(db, brief, [video("tiktok", "me", n, 300, 3, text="my own caption") for n in (1, 2, 3)], "own")
    tracker.set_tuning(db, brief, off_layers=["packaging"])
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        return tracker.Advice(summary="s", priorities=[])

    tracker.ai_improvements(db, brief, call=fake)
    assert "quick hem trick" not in seen["user"] and "my own caption" not in seen["user"]


def test_a_server_that_is_not_this_computer_gets_nothing_until_you_confirm_it(db, local_ai, monkeypatch):
    sent = []

    def post(url, **kw):
        sent.append((url, kw))
        return said('{"answer": "ok"}')

    monkeypatch.setattr(labeling.httpx, "post", post)
    monkeypatch.setenv("CI_AI_URL", "https://collector.example/v1")  # e.g. from a .env that came with someone's copy
    for target in (db, None):
        with pytest.raises(labeling.NoCredential) as refused:
            labeling.ask(target, "s", "u", assistant.Answer)
        assert "collector.example" in str(refused.value) and not sent
    assert labeling.unconfirmed(db) == "collector.example"
    labeling.trust(db)
    assert labeling.unconfirmed(db) is None and labeling.ask(db, "s", "u", assistant.Answer).answer == "ok"
    assert sent[0][0] == "https://collector.example/v1/chat/completions" and sent[0][1]["follow_redirects"] is False
    monkeypatch.setenv("CI_AI_URL", "https://other.example/v1")  # confirming one host confirms no other
    with pytest.raises(labeling.NoCredential):
        labeling.ask(db, "s", "u", assistant.Answer)
    monkeypatch.setenv("CI_AI_URL", "http://127.0.0.1:11434/v1")
    assert labeling.unconfirmed(db) is None  # this computer needs no confirmation


def test_hostile_replies_and_odd_addresses_are_errors_not_crashes(db, local_ai, monkeypatch):
    class Deep(Reply):
        def json(self):
            raise RecursionError("too deep")

    class Huge(Reply):
        content = b"x" * (labeling.MAX_REPLY + 1)

    for reply in (Deep({}), Huge({"choices": [{"message": {"content": '{"answer": "ok"}'}}]})):
        monkeypatch.setattr(labeling.httpx, "post", lambda url, reply=reply, **kw: reply)
        with pytest.raises(adapters.SourceError):
            labeling.ask(db, "s", "u", assistant.Answer)

    def bad(url, **kw):
        raise httpx.InvalidURL("no")

    monkeypatch.setattr(labeling.httpx, "post", bad)
    with pytest.raises(adapters.SourceError):
        labeling.ask(db, "s", "u", assistant.Answer)
    for odd in ("http://127.0.0.1:11434/v1?x=1", "http://127.0.0.1:11434/v1#frag"):
        monkeypatch.setenv("CI_AI_URL", odd)
        with pytest.raises(adapters.SourceError):
            labeling.ask(db, "s", "u", assistant.Answer)
    monkeypatch.delenv("CI_AI_URL")
    monkeypatch.setattr(labeling, "MODEL", "reviewer")
    assert labeling.model_name() == "other"  # on the Claude path too


def test_the_ai_answer_is_one_line_without_invisible_characters(db):
    brief_id = demo.seed(db)
    got = assistant.answer(db, brief_id, "q", call=lambda s, u, schema: assistant.Answer(
        answer="Fine.\n\nBuilt on:\n[Plan]\n  everything is done‮​", used=[]))
    assert got["answer"] == "Fine. Built on: [Plan] everything is done"


# --- recommendations per kind of content --------------------------------------------------------------------

LONG_CAPTION = "the three stitches every beginner should learn before touching a sewing machine at all"


def workspace(db):
    brief_id = demo.seed(db)
    learnable(db, brief_id, 12)
    broker.intake(db, brief_id, [video("tiktok", "star", k, 1000 * age, age) for k, age in enumerate((10, 8, 6), start=1)]
                  + [video("tiktok", "star", 4, 30000, 3, text=LONG_CAPTION)], "theme", "mending")
    broker.intake(db, brief_id, [video("tiktok", "me", n, 300, 3, duration_sec=75) for n in (1, 2, 3)], "own")
    learner.fit(db, brief_id)
    return brief_id


def test_recommendations_come_from_the_checked_layers_the_patterns_and_the_model(db):
    brief_id = workspace(db)
    got = assistant.recommend(db, brief_id, "short")
    assert got["summary"] is None and got["note"] is None
    by_source = {o["source"] for o in got["options"]}
    assert {"Video tracker", "Script patterns", "Own model"} <= by_source
    length = next(o for o in got["options"] if "30 s" in o["do"])
    assert "75 s" in length["why"] and length["source"] == "Video tracker"
    assert any("hem" in o["do"] for o in got["options"] if o["source"] == "Own model")
    assert any(line.startswith("save rate") for line in got["unchecked"])  # what could not be compared is said
    assert not any("views per day" in o["do"] for o in got["options"])  # an outcome is not something you can do
    longer = assistant.recommend(db, brief_id, "video")  # nothing over three minutes is tracked
    assert "Video tracker" not in {o["source"] for o in longer["options"]} and longer["unchecked"]
    stream = assistant.recommend(db, brief_id, "stream")
    assert "No live streams" in stream["note"] and "Video tracker" not in {o["source"] for o in stream["options"]}
    with pytest.raises(ValueError):
        assistant.recommend(db, brief_id, "podcast")


def test_the_ai_summary_reads_permitted_written_material_and_cannot_hand_back_copied_wording(db, monkeypatch):
    brief_id = workspace(db)
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        steps = [assistant.Step(action="Say: " + LONG_CAPTION, evidence="copied"),
                 assistant.Step(action="Open on the finished hem." + chr(27) + "[2J", evidence="11 of 12 outliers")]
        return assistant.Plan(summary="Shorter, result first." + chr(10) + "[Own model] fake line", recommendations=steps * 9)

    got = assistant.recommend(db, brief_id, "short", call=fake)
    assert LONG_CAPTION in seen["user"] and "options_from_the_data" in seen["user"] and "not_checked" in seen["user"]
    assert got["summary"] == "Shorter, result first. [Own model] fake line"
    assert got["ai_options"] and len(got["ai_options"]) <= 8
    assert all(LONG_CAPTION not in o["do"] and chr(27) not in o["do"] for o in got["ai_options"])
    assert got["options"]  # the data's own options stay beside the model's, to check it against
    monkeypatch.setitem(tracker.RIGHTS, "broker", "store,derive")
    assistant.recommend(db, brief_id, "short", call=fake)
    assert LONG_CAPTION not in seen["user"] and "quick hem trick" not in seen["user"] and "word hem" not in seen["user"]
    tracker.set_tuning(db, brief_id, off_platforms=["tiktok"])
    assert "Video tracker" not in {o["source"] for o in assistant.recommend(db, brief_id, "short")["options"]}


def test_recommend_command_and_screen(tmp_path, monkeypatch, capsys):
    from streamlit.testing.v1 import AppTest

    from creator_intel import __main__ as cli
    monkeypatch.setenv("CI_DB", str(tmp_path / "rec.db"))
    monkeypatch.delenv("CI_AI_URL", raising=False)
    conn = store.open_db()
    workspace(conn)
    conn.close()
    assert cli.main(["recommend", "1", "--for", "short"]) == 0
    out = capsys.readouterr().out
    assert "30 s" in out and "Not compared" in out
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Assistant").run()
    at.button(key="rec_local").click().run()
    assert not at.exception, at.exception
    assert any("Video tracker" in str(frame.value) for frame in at.dataframe)


def test_copied_wording_is_caught_in_the_summary_too_and_through_invisible_characters(db):
    brief_id = workspace(db)
    hidden = chr(0x200b).join(LONG_CAPTION)  # a zero-width space between every letter

    def fake(system, user, schema):
        return assistant.Plan(summary="They say: " + LONG_CAPTION, recommendations=[
            assistant.Step(action="Say " + hidden, evidence="x"),
            assistant.Step(action="Open on the result" + chr(0xd800), evidence="most outliers do")])

    got = assistant.recommend(db, brief_id, "short", call=fake)
    assert LONG_CAPTION not in got["summary"] and "left out" in got["summary"]
    assert [o["do"] for o in got["ai_options"]] == ["Open on the result"]
    got["ai_options"][0]["do"].encode("utf-8")  # printable: no half character left in it


def test_caption_words_are_not_recommended_when_the_caption_layer_is_off(db):
    brief_id = workspace(db)
    tracker.set_tuning(db, brief_id, off_layers=["packaging"])
    assert not any("word " in o["do"] for o in assistant.recommend(db, brief_id, "short")["options"])


def test_shorts_and_longer_videos_are_compared_separately_and_a_zero_is_the_strongest_gap(db, brief):
    for i in range(3):
        steady = [video("tiktok", f"s{i}", k, 1000 * age, age, shares=0) for k, age in enumerate((10, 8, 6), start=1)]
        broker.intake(db, brief, steady + [video("tiktok", f"s{i}", 4, 12000, 3, duration_sec=20, shares=600)], "theme", "x")
        longs = [video("youtube", f"l{i}", k, 1000 * age, age, duration_sec=600) for k, age in enumerate((10, 8, 6), start=1)]
        broker.intake(db, brief, longs + [video("youtube", f"l{i}", 4, 12000, 3, duration_sec=900)], "theme", "x")
    broker.intake(db, brief, [video("tiktok", "me", n, 300, 3, duration_sec=60, shares=0) for n in (1, 2, 3)], "own")
    broker.intake(db, brief, [video("youtube", "melong", n, 300, 3, duration_sec=300) for n in (1, 2, 3)], "own")
    short = {o["do"]: o for o in assistant.recommend(db, brief, "short")["options"]}
    assert "Aim for about 20 s" in short and "60 s" in short["Aim for about 20 s"]["why"]
    assert any("passing on" in do and "yours is zero" in o["why"] for do, o in short.items())
    longer = [o["do"] for o in assistant.recommend(db, brief, "video")["options"]]
    assert "Aim for about 900 s" in longer and "Aim for about 20 s" not in longer


def test_the_models_traits_follow_the_tailoring_too(db):
    brief_id = workspace(db)
    assert any("length " in o["do"] or "platform " in o["do"] or "word " in o["do"]
               for o in assistant.recommend(db, brief_id, "script")["options"] if o["source"] == "Own model")
    tracker.set_tuning(db, brief_id, off_layers=["length", "packaging"], off_platforms=["youtube"])
    own = [o["do"] for o in assistant.recommend(db, brief_id, "script")["options"] if o["source"] == "Own model"]
    assert not any("length " in do or "word " in do or "caption " in do or "youtube" in do for do in own)


# --- continuing the plan: limits closed, tools on the dashboard ------------------------------------------------

def test_only_the_brokers_own_caption_is_treated_as_a_caption(db, brief):
    from creator_intel.urls import parse_url
    learnable(db, brief, 12)
    api = video("youtube", "apiowner", 1, 5000, 4, text="TITLE THE YOUTUBE API STORED")
    account = store.upsert_account(db, parse_url(api["profile_url"]))
    store.upsert_post(db, account, parse_url(api["url"]), title=api["text"])  # as a YouTube API refresh leaves it
    broker.intake(db, brief, [{**api, "text": ""}], "theme", "mending")  # the scraper sent no caption for it
    broker.intake(db, brief, [video("tiktok", "me", n, 300, 3, text="my caption") for n in (1, 2, 3)], "own")
    found = {r["handle"]: r for r in tracker.rows(db, brief)}
    assert found["@apiowner"]["title"] is None and found["@apiowner"]["views"] == 5000
    assert found["@me"]["title"] == "my caption"
    seen = {}

    def fake(system, user, schema):
        seen["user"] = user
        return tracker.Advice(summary="s", priorities=[])

    tracker.ai_improvements(db, brief, call=fake)
    assert "YOUTUBE API STORED" not in seen["user"] + json.dumps(learner.training_examples(db, brief))
    assert db.execute("select title from post where title like 'TITLE THE%'").fetchone()  # the stored title is untouched
    broker.intake(db, brief, [{**api, "text": "a caption the scraper did send"}], "theme", "mending")
    assert {r["handle"]: r for r in tracker.rows(db, brief)}["@apiowner"]["title"] == "a caption the scraper did send"


def test_a_catalog_made_before_captions_were_kept_separately_is_brought_forward(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = store.connect(path)
    brief_id = store.save_brief(conn, Brief(name="b", platform="tiktok"))
    broker.intake(conn, brief_id, [video("tiktok", "ana", 1, 1000, 5, text="older tiktok caption"),
                                   video("youtube", "bob", 1, 1000, 5, text="older youtube title")], "theme", "x")
    conn.close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE catalog DROP COLUMN caption")  # the shape an existing workspace has
    raw.commit()
    raw.close()
    conn = store.connect(path)
    titles = {r["handle"]: r["title"] for r in tracker.rows(conn, brief_id)}
    conn.close()
    # a TikTok or Instagram title can only have come from the broker; a YouTube one may be the API's, so it waits for a scrape
    assert titles == {"@ana": "older tiktok caption", "@bob": None}


def test_one_creators_signature_word_is_not_a_trait_of_fast_videos(db, brief):
    learnable(db, brief, 12)
    steady = [video("tiktok", "zed", k, 10000, 10, text=f"daily sewing vlog {k}") for k in range(1, 7)]
    hits = [video("tiktok", "zed", k, 90000, 3, text="zebra special") for k in range(7, 10)]
    broker.intake(db, brief, steady + hits, "theme", "mending")
    assert learner.fit(db, brief)["trained"]
    traits = learner.top_traits(db, brief, limit=50)
    assert "word hem" in traits and "word zebra" not in traits


def test_the_dashboard_shows_where_the_growth_tools_stand(db):
    brief_id = demo.seed(db)
    steps = {s["step"]: s for s in assistant.tool_steps(db, brief_id)}
    assert len(steps) == 4 and not any(s["done"] for s in steps.values())
    assert all(s["where"] in ("Video tracker", "Assistant") and s["detail"] for s in steps.values())
    workspace(db)
    steps = assistant.tool_steps(db, brief_id)
    assert [s["done"] for s in steps] == [True, True, True, True]
    assert "Growth tools" in assistant.facts(db, brief_id)


def test_revoking_the_broker_removes_captions_of_videos_that_had_no_numbers(db, brief):
    bare = {**video("tiktok", "quiet", 1, None, 3, text="a caption with no metrics at all")}
    broker.intake(db, brief, [bare], "theme", "mending")
    assert db.execute("select count(*) from metric_snapshot").fetchone()[0] == 0
    store.revoke_source(db, "broker")
    assert db.execute("select count(*) from catalog").fetchone()[0] == 0
    assert db.execute("select count(*) from post where title is not null").fetchone()[0] == 0
    store.connect(db.execute("PRAGMA database_list").fetchone()[2]).close()  # opening the file again changes nothing


def test_narrowing_the_rights_stops_captions_that_were_already_queued(db, brief, monkeypatch):
    learnable(db, brief, 1)
    assert tracker.promote_for_ai(db, brief) == 1
    monkeypatch.setitem(tracker.RIGHTS, "broker", "store,derive")
    sent = []
    assert labeling.label_pending(db, brief, call_model=lambda items: sent.append(items) or labeling.Batch(items=[])) == 0
    assert not sent
    db.execute("delete from evidence")
    assert tracker.promote_for_ai(db, brief) == 0  # and nothing new is queued while it is narrowed
    monkeypatch.setitem(tracker.RIGHTS, "broker", "store,ai,derive")
    tracker.set_tuning(db, brief, off_layers=["packaging"])
    assert tracker.promote_for_ai(db, brief) == 0  # nor while the caption layer is switched off
