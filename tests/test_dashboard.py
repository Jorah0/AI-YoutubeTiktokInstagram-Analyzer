"""The dashboard's plan-progress checklist and the launcher behind the exe."""
import os
import sys
from pathlib import Path

import pytest

import launcher
from creator_intel import analysis, db as store, demo, pipeline
from creator_intel.taxonomy import Brief
from creator_intel.urls import parse_url

ROOT = Path(__file__).parent.parent


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def done(steps):
    return {s["step"]: s["done"] for s in steps}


# --- plan progress -------------------------------------------------------------------------------

def test_a_new_brief_has_nothing_done_and_points_at_the_first_step(db, monkeypatch):
    for name in ("BRAVE_API_KEY", "YOUTUBE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    brief_id = store.save_brief(db, Brief(name="new", platform="youtube"))
    steps = analysis.progress(db, brief_id)
    assert not any(s["done"] for s in steps)
    assert [s["week"] for s in steps] == sorted(s["week"] for s in steps)
    assert analysis.next_step(steps)["step"] == steps[0]["step"] and "topic" in steps[0]["detail"]


def test_progress_follows_the_real_state_of_the_work(db, monkeypatch):
    for name in ("BRAVE_API_KEY", "YOUTUBE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    brief_id = demo.seed(db)
    first = done(analysis.progress(db, brief_id))
    assert first["Write the brief"] and first["Import ten creator links"]
    assert first["Review three posts for at least three creators"] and first["Rank and get a shortlist"]
    assert not first["Add your search and YouTube keys"] and not first["Have two reviewers judge every suggestion"]
    assert analysis.next_step(analysis.progress(db, brief_id))["step"] == "Add your search and YouTube keys"

    run_id = pipeline.latest_run(db, brief_id)
    s = analysis.suggestions(db, run_id)
    for reviewer in ("ana", "ben"):
        for account_id in set(s["rule"]) | set(s["topic_only"]):
            analysis.judge(db, run_id, account_id, reviewer, True)
    judged = done(analysis.progress(db, brief_id))
    assert judged["Have two reviewers judge every suggestion"]
    assert not judged["Pass the launch gate"]  # all accepted, but the rule did not beat topic-only

    owner = store.upsert_account(db, parse_url("https://www.youtube.com/@me"), is_owner=1)
    post = store.upsert_post(db, owner, parse_url("https://youtu.be/abcdefghijk"))
    store.add_snapshot(db, post, "owner_export", views=10)
    exp = pipeline.create_experiment(db, brief_id, "h", "m")
    later = done(analysis.progress(db, brief_id))
    assert later["Add your own channel and import your metrics"] and later["Start an experiment"]
    assert not later["Reach a decision on the experiment"]
    for pair in range(1, 5):
        pipeline.record_observation(db, exp, pair, "baseline", 10)
        pipeline.record_observation(db, exp, pair, "test", 12)
    pipeline.experiment_result(db, exp)
    assert done(analysis.progress(db, brief_id))["Reach a decision on the experiment"]


def test_overview_numbers(db):
    brief_id = demo.seed(db)
    got = analysis.overview(db, brief_id)
    assert (got["candidates"], got["reviewed_creators"], got["shortlist"], got["waiting_review"]) == (10, 5, 4, 0)
    assert got["gate"] is None and got["last_run"]["status"] == "finished"


# --- launcher ----------------------------------------------------------------------------------------

def test_env_file_sets_only_what_is_not_already_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_MODEL", "from-shell")
    for name in ("CI_WEBHOOK_URL", "BRAVE_API_KEY", "CI_NO_BROWSER"):
        monkeypatch.setenv(name, "x")  # so that whatever load_env sets is undone after the test
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_text('# comment\n\nCI_MODEL=from-file\nCI_WEBHOOK_URL = value \nBRAVE_API_KEY="a b"\nCI_NO_BROWSER=\n'
                   'not a setting\n')
    launcher.load_env(env)
    assert os.environ["CI_MODEL"] == "from-shell" and os.environ["CI_WEBHOOK_URL"] == "value"
    assert os.environ["BRAVE_API_KEY"] == "a b" and "CI_NO_BROWSER" not in os.environ
    launcher.load_env(tmp_path / "missing.env")  # no file is fine


def test_dashboard_is_served_to_this_computer_only(tmp_path):
    args = launcher.streamlit_args(tmp_path / "app.py", 8611, open_browser=False)
    assert args[:3] == ["streamlit", "run", str(tmp_path / "app.py")]
    assert "--server.address=127.0.0.1" in args and "--server.port=8611" in args
    assert "--server.headless=true" in args and "--global.developmentMode=false" in args
    assert "--server.headless=false" in launcher.streamlit_args(tmp_path / "app.py", 8611, open_browser=True)


def test_home_is_the_exe_folder_when_packaged_and_the_repo_otherwise(tmp_path, monkeypatch):
    assert launcher.home() == ROOT
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "CreatorResearch.exe"))
    assert launcher.home() == tmp_path


def test_data_lives_beside_the_program_unless_told_otherwise(tmp_path, monkeypatch):
    monkeypatch.delenv("CI_DB", raising=False)
    monkeypatch.setattr(launcher, "home", lambda: tmp_path)
    launcher.prepare()
    assert Path(os.environ["CI_DB"]) == tmp_path / "data" / "creator_intel.db"
    monkeypatch.setenv("CI_DB", "elsewhere.db")
    launcher.prepare()
    assert os.environ["CI_DB"] == "elsewhere.db"


def test_arguments_run_a_command_instead_of_opening_the_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CI_DB", str(tmp_path / "cli.db"))
    monkeypatch.setattr(launcher, "home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "run_dashboard", lambda: pytest.fail("the dashboard must not start"))
    assert launcher.main(["demo"]) == 0 and launcher.main(["briefs"]) == 0
    assert "fictional demo" in capsys.readouterr().out


def test_free_port_is_usable():
    import socket
    port = launcher.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


# --- the dashboard screen ---------------------------------------------------------------------------------

def test_dashboard_screen_shows_progress_and_the_next_step(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    for name in ("BRAVE_API_KEY", "YOUTUBE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    conn = store.open_db()
    demo.seed(conn)
    conn.close()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    assert {m.label: m.value for m in at.metric}["Creators in the brief"] == "10"
    assert any("Add your search and YouTube keys" in m.value for m in at.info)


# --- findings from the security review of this round --------------------------------------------------

def test_env_file_can_only_set_this_programs_own_settings(tmp_path, monkeypatch):
    for name in ("BRAVE_API_KEY", "CI_WEBHOOK_URL", "ANTHROPIC_BASE_URL", "HTTPS_PROXY", "SSL_CERT_FILE",
                 "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("BRAVE_API_KEY=abc123 # my key\nCI_WEBHOOK_URL=https://discord.com/api/webhooks/1/x#y\n"
                   "export YOUTUBE_API_KEY=yt=with=equals\nANTHROPIC_BASE_URL=https://evil.example\n"
                   "HTTPS_PROXY=http://evil.example:8080\nSSL_CERT_FILE=evil.pem\n"
                   "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false\n")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    launcher.load_env(env)
    assert os.environ["BRAVE_API_KEY"] == "abc123"  # the trailing comment is not part of the key
    assert os.environ["CI_WEBHOOK_URL"] == "https://discord.com/api/webhooks/1/x#y"  # a # inside a value stays
    assert os.environ["YOUTUBE_API_KEY"] == "yt=with=equals"
    for name in ("ANTHROPIC_BASE_URL", "HTTPS_PROXY", "SSL_CERT_FILE", "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION"):
        assert name not in os.environ


def test_an_ffmpeg_sitting_in_the_current_folder_is_not_trusted(tmp_path, monkeypatch):
    import shutil

    from creator_intel import prepublish
    planted = tmp_path / "ffmpeg.exe"
    planted.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name, **kw: str(planted))
    assert prepublish.find_ffmpeg() is None and prepublish.loudness(tmp_path / "a.wav") is None
    monkeypatch.setattr(shutil, "which", lambda name, **kw: str(tmp_path / "tools" / "ffmpeg.exe"))
    assert prepublish.find_ffmpeg() == str(tmp_path / "tools" / "ffmpeg.exe")


def test_rebuilding_keeps_the_data_and_keys_of_someone_using_the_build_folder(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_exe", ROOT / "scripts" / "build_exe.py")
    build_exe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_exe)

    out = tmp_path / "dist" / "CreatorResearch"
    (out / "data").mkdir(parents=True)
    (out / "data" / "creator_intel.db").write_text("my work")
    (out / ".env").write_text("BRAVE_API_KEY=secret")
    with build_exe.kept_user_files(out, tmp_path / "stash"):
        import shutil
        shutil.rmtree(out)  # what PyInstaller --clean does to the output folder
        out.mkdir()
    assert (out / "data" / "creator_intel.db").read_text() == "my work"
    assert (out / ".env").read_text() == "BRAVE_API_KEY=secret" and not (tmp_path / "stash").exists()


# --- findings from the correctness review of this round -------------------------------------------------

def test_env_file_saved_by_powershell_as_utf16_still_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_WEBHOOK_URL", "x")
    monkeypatch.delenv("CI_WEBHOOK_URL")
    env = tmp_path / ".env"
    env.write_bytes("CI_WEBHOOK_URL=yes\r\n".encode("utf-16"))
    launcher.load_env(env)
    assert os.environ["CI_WEBHOOK_URL"] == "yes"
    env.write_bytes(b"\xff\xfe\x00\xd8\x00")  # unreadable: skipped, never a crash at startup
    launcher.load_env(env)


def test_env_values_with_quotes_and_comments(tmp_path, monkeypatch):
    for name in ("CI_WEBHOOK_URL", "BRAVE_API_KEY", "CI_MODEL"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_text('CI_WEBHOOK_URL="q w" # note\nBRAVE_API_KEY=pa"ss\nCI_MODEL=plain   # note\n')
    launcher.load_env(env)
    assert (os.environ["CI_WEBHOOK_URL"], os.environ["BRAVE_API_KEY"], os.environ["CI_MODEL"]) == ("q w", 'pa"ss', "plain")


def test_a_port_someone_else_is_listening_on_is_not_chosen():
    import socket
    with socket.socket() as other:
        other.bind(("0.0.0.0", 0))
        other.listen()
        taken = other.getsockname()[1]
        assert launcher.free_port(preferred=taken) != taken


def test_discovery_step_is_per_brief_and_ignores_connector_checks(db, monkeypatch):
    first = store.save_brief(db, Brief(name="one", platform="youtube"))
    second = store.save_brief(db, Brief(name="two", platform="youtube"))
    store.record_usage(db, "brave_requests", 1)  # what `doctor --live` leaves behind

    def discovery(brief_id):
        return done(analysis.progress(db, brief_id))["Run discovery with your keys"]

    assert not discovery(first) and not discovery(second)
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(pipeline, "discover", lambda db, brief_id: 3)
    monkeypatch.setattr(pipeline.labeling, "label_pending", lambda db, brief_id: 0)
    pipeline.run_all(db, first)
    assert discovery(first) and not discovery(second)


# --- night mode ----------------------------------------------------------------------------------------

def test_day_and_night_themes_both_travel_with_the_program_and_match_the_repo_config(tmp_path):
    args = launcher.streamlit_args(tmp_path / "app.py", 8611, open_browser=False)
    themed = [a for a in args if a.startswith("--theme.")]
    for mode in ("light", "dark"):
        for option in ("primaryColor", "backgroundColor", "secondaryBackgroundColor", "textColor"):
            assert any(a.startswith(f"--theme.{mode}.{option}=#") for a in themed), (mode, option)
    assert not any(a.startswith("--theme.base") for a in themed)  # a fixed base would take the choice away
    config = (ROOT / ".streamlit" / "config.toml").read_text()
    assert "[theme.light]" in config and "[theme.dark]" in config
    for flag in themed:  # `streamlit run app.py` reads the file, the exe gets flags: they must agree
        option, value = flag.rsplit(".", 1)[1].split("=")
        assert f'{option} = "{value}"' in config, flag


def test_the_broker_token_can_be_set_in_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("APIFY_TOKEN=abc\n")
    launcher.load_env(env)
    assert os.environ["APIFY_TOKEN"] == "abc"


def test_env_names_are_listed_one_by_one_and_some_count_only_with_what_they_belong_to(tmp_path, monkeypatch, capsys):
    names = ("APIFY_TOKEN", "CI_AI_URL", "CI_AI_MODEL", "CI_AI_KEY", "CI_BROKER_RIGHTS", "CI_YOUTUBE_RIGHTS",
             "YOUTUBE_API_KEY", "CI_APIFY_TIKTOK", "CI_SOMETHING_NEW", "CI_DB", "CI_BRAVE_USD_PER_REQUEST", "BRAVE_API_KEY")
    for name in names:
        monkeypatch.setenv(name, "x")  # so that whatever load_env sets is undone after the test
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_text("CI_AI_KEY=k\nCI_BROKER_RIGHTS=store,ai,derive\nCI_YOUTUBE_RIGHTS=store,ai,derive\n"
                   "CI_APIFY_TIKTOK=someone~actor\nCI_SOMETHING_NEW=1\nCI_AI_URL=https://collector.example/v1\n"
                   "CI_DB=elsewhere.db\nCI_BRAVE_USD_PER_REQUEST=0.01\n")
    launcher.load_env(env)
    # the two that stand alone; each of the others lacks what it belongs to
    assert [name for name in names if name in os.environ] == ["CI_BROKER_RIGHTS", "CI_DB"]
    said = capsys.readouterr().out
    assert "CI_SOMETHING_NEW" in said and "CI_AI_KEY" in said and "CI_AI_URL" in said and "k\n" not in said
    assert "collector.example" not in said  # names only, never values
    env.write_text("CI_AI_URL=http://127.0.0.1:11434/v1\nCI_AI_MODEL=llama3.1\nCI_AI_KEY=k\nAPIFY_TOKEN=t\n"
                   "CI_BROKER_RIGHTS=store\nCI_APIFY_TIKTOK=someone~actor\n")
    launcher.load_env(env)
    for name in ("CI_AI_URL", "CI_AI_MODEL", "CI_AI_KEY", "APIFY_TOKEN", "CI_BROKER_RIGHTS", "CI_APIFY_TIKTOK"):
        assert name in os.environ, name
        monkeypatch.delenv(name)  # put back what load_env set


def test_a_file_cannot_lean_on_secrets_it_did_not_bring_or_echo_one_or_stop_the_program(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APIFY_TOKEN", "the-owners-own-token")  # set in Windows, not in the file
    monkeypatch.setenv("CI_AI_KEY", "the-owners-own-key")
    for name in ("CI_APIFY_TIKTOK", "CI_AI_URL", "CI_AI_MODEL", "CI_WEBHOOK_URL", "CI_MODEL"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_bytes(b"CI_APIFY_TIKTOK=someone~actor\nAbCd1234SecretKeyXYZ==\nsk_live_pasted_by_mistake=1\n"
                    b"CI_WEBHOOK_URL=https://discord.com/x\x00y\nCI_MODEL=fine\n")
    launcher.load_env(env)  # must not raise: the program still has to start
    assert "CI_APIFY_TIKTOK" not in os.environ  # someone else's scraper may not run on a token the file did not bring
    assert "CI_WEBHOOK_URL" not in os.environ and os.environ["CI_MODEL"] == "fine"
    said = capsys.readouterr().out
    assert "AbCd1234" not in said and "sk_live" not in said and "2 line" in said and "CI_APIFY_TIKTOK" in said


def test_every_setting_the_program_reads_can_be_set_in_the_env_file():
    import re
    root = Path(__file__).parent.parent
    read = set()
    for source in [root / "app.py", root / "launcher.py", *(root / "creator_intel").glob("*.py")]:
        read |= set(re.findall(r'environ(?:\.get)?[\[(]\s*"([A-Z][A-Z0-9_]+)"', source.read_text(encoding="utf-8")))
    assert read and read <= set(launcher.ENV_NAMES), read - set(launcher.ENV_NAMES)


def test_narrowing_what_scraped_data_may_be_used_for_never_depends_on_a_token(tmp_path, monkeypatch):
    for name in ("CI_BROKER_RIGHTS", "APIFY_TOKEN"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_text("CI_BROKER_RIGHTS=store\n")  # videos scraped earlier are still in the database
    launcher.load_env(env)
    assert os.environ["CI_BROKER_RIGHTS"] == "store"


# --- keys entered in the tool itself --------------------------------------------------------------------------

SETTABLE = ("BRAVE_API_KEY", "YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "APIFY_TOKEN", "CI_AI_URL", "CI_AI_MODEL",
            "CI_AI_KEY", "CI_WEBHOOK_URL", "CI_PASSWORD")


def clean_env(monkeypatch):
    for name in SETTABLE:
        monkeypatch.setenv(name, "x")  # so that whatever is set during the test is undone after it
        monkeypatch.delenv(name)


def test_settings_saved_from_the_tool_land_in_the_env_file_and_load_back_the_same(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("# my notes\nCI_DB=keep.db\nBRAVE_API_KEY=old\n", encoding="utf-8")
    launcher.save_env(env, {"BRAVE_API_KEY": "abc #1 'x'", "CI_AI_URL": "http://127.0.0.1:11434/v1",
                            "CI_AI_MODEL": "llama3.1", "APIFY_TOKEN": " padded "})
    text = env.read_text(encoding="utf-8")
    assert "# my notes" in text and "CI_DB=keep.db" in text and "BRAVE_API_KEY=old" not in text
    assert os.environ["BRAVE_API_KEY"] == "abc #1 'x'" and os.environ["CI_AI_MODEL"] == "llama3.1"  # in force at once
    clean_env(monkeypatch)
    monkeypatch.setenv("CI_DB", "already-set")
    launcher.load_env(env)  # as the next start of the program will
    assert os.environ["BRAVE_API_KEY"] == "abc #1 'x'" and os.environ["APIFY_TOKEN"] == "padded"
    assert os.environ["CI_AI_URL"] == "http://127.0.0.1:11434/v1"
    launcher.save_env(env, {"BRAVE_API_KEY": "", "APIFY_TOKEN": None})
    assert "BRAVE_API_KEY" not in env.read_text(encoding="utf-8") and "BRAVE_API_KEY" not in os.environ
    assert "APIFY_TOKEN" not in os.environ and "CI_AI_MODEL=llama3.1" in env.read_text(encoding="utf-8")


def test_settings_that_cannot_work_are_refused_and_nothing_is_written(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    env = tmp_path / ".env"
    launcher.save_env(env, {"CI_AI_URL": "http://127.0.0.1:1/v1", "CI_AI_MODEL": "m"})
    before = env.read_text(encoding="utf-8")
    for bad in ({"HTTPS_PROXY": "http://evil.example"}, {"BRAVE_API_KEY": "two\nlines"}, {"CI_AI_MODEL": ""},
                {"CI_APIFY_TIKTOK": "someone~actor"}, {"BRAVE_API_KEY": "both ' and \" quotes"}):
        with pytest.raises(ValueError):
            launcher.save_env(env, bad)
        assert env.read_text(encoding="utf-8") == before and "HTTPS_PROXY" not in os.environ
    launcher.save_env(tmp_path / "new" / ".env", {"CI_AI_KEY": "k", "CI_AI_URL": "http://127.0.0.1:1/v1", "CI_AI_MODEL": "m"})
    assert (tmp_path / "new" / ".env").is_file()  # the folder is made when it is not there yet


def test_keys_can_be_entered_on_the_data_and_access_screen_and_are_never_shown(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    clean_env(monkeypatch)
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    monkeypatch.setattr(launcher, "home", lambda: tmp_path)
    conn = store.open_db()
    demo.seed(conn)
    conn.close()
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"), default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Data and access").run()
    at.text_input(key="set_BRAVE_API_KEY").set_value("brave-secret-value")
    at.text_input(key="set_CI_AI_KEY").set_value("orphan")  # no server named: must be refused, and nothing saved
    at.button(key="settings_save").click().run()
    assert not at.exception, at.exception
    assert at.warning and not (tmp_path / ".env").exists() and "BRAVE_API_KEY" not in os.environ
    at.text_input(key="set_CI_AI_KEY").set_value("")
    at.text_input(key="set_BRAVE_API_KEY").set_value("brave-secret-value")
    at.button(key="settings_save").click().run()
    assert not at.exception, at.exception
    assert "BRAVE_API_KEY=brave-secret-value" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert os.environ["BRAVE_API_KEY"] == "brave-secret-value"
    shown = " ".join(str(getattr(e, "value", "")) for kind in ("markdown", "caption", "text_input", "success", "dataframe")
                     for e in getattr(at, kind))
    assert "brave-secret-value" not in shown
    at.multiselect(key="settings_remove").set_value(["BRAVE_API_KEY"])
    at.button(key="settings_save").click().run()
    assert not at.exception and "BRAVE_API_KEY" not in os.environ


def test_a_pasted_value_cannot_smuggle_in_other_settings_through_odd_line_breaks(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    env = tmp_path / ".env"
    for breaker in (chr(0x2028), chr(0x2029), chr(0x85), chr(0x0c)):
        sneaky = "abc" + breaker + "CI_AI_URL=http://127.0.0.1:9/v1" + breaker + "CI_AI_MODEL=m"
        with pytest.raises(ValueError):
            launcher.save_env(env, {"BRAVE_API_KEY": sneaky})
        env.write_text("BRAVE_API_KEY=" + sneaky + chr(10), encoding="utf-8")  # the same thing written by hand
        launcher.load_env(env)
        assert "CI_AI_URL" not in os.environ and "CI_AI_MODEL" not in os.environ and "BRAVE_API_KEY" not in os.environ
        env.unlink()


def test_a_new_ai_server_address_never_inherits_the_old_servers_key(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    env = tmp_path / ".env"
    launcher.save_env(env, {"CI_AI_URL": "https://mine.example/v1", "CI_AI_MODEL": "m", "CI_AI_KEY": "my-key"})
    assert launcher.save_env(env, {"CI_AI_MODEL": "other-model"}) == []  # same server: the key stays
    assert os.environ["CI_AI_KEY"] == "my-key"
    dropped = launcher.save_env(env, {"CI_AI_URL": "http://127.0.0.1:9999/v1"})
    assert dropped == ["CI_AI_KEY"] and "CI_AI_KEY" not in os.environ and "my-key" not in env.read_text(encoding="utf-8")
    assert launcher.save_env(env, {"CI_AI_URL": "https://new.example/v1", "CI_AI_KEY": "new-key"}) == []
    assert os.environ["CI_AI_KEY"] == "new-key"  # typed together with the address: kept


def test_a_save_that_fails_leaves_no_copy_of_the_keys_behind(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("CI_DB=keep.db" + chr(10), encoding="utf-8")

    def locked(src, dst):
        raise PermissionError("the file is open in another program")

    monkeypatch.setattr(launcher.os, "replace", locked)
    with pytest.raises(OSError):
        launcher.save_env(env, {"BRAVE_API_KEY": "secret"})
    assert [p.name for p in tmp_path.iterdir()] == [".env"] and "BRAVE_API_KEY" not in os.environ
    assert env.read_text(encoding="utf-8") == "CI_DB=keep.db" + chr(10)


def test_a_hand_edited_file_does_not_block_saving_something_unrelated(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("CI_YOUTUBE_RIGHTS=store,derive" + chr(10) + "CI_BRAVE_USD_PER_REQUEST=0.01" + chr(10)
                   + "BRAVE_API_KEY=b" + chr(10), encoding="utf-8")
    launcher.save_env(env, {"APIFY_TOKEN": "t"})  # the lone rights line is the file's business, not this save's
    assert os.environ["APIFY_TOKEN"] == "t"
    with pytest.raises(ValueError) as refused:  # but this save would itself strand the price setting
        launcher.save_env(env, {"BRAVE_API_KEY": ""})
    assert "CI_BRAVE_USD_PER_REQUEST" in str(refused.value) and ".env" in str(refused.value)


# --- a password on the dashboard ------------------------------------------------------------------------------

def fresh_lock(monkeypatch, password=None):
    monkeypatch.setenv("CI_PASSWORD", "x")
    monkeypatch.delenv("CI_PASSWORD")
    if password:
        monkeypatch.setenv("CI_PASSWORD", password)
    monkeypatch.setattr(launcher, "_tries", {"fails": 0, "until": 0.0})


def test_wrong_passwords_are_refused_and_guessing_is_slowed_for_everyone(monkeypatch):
    fresh_lock(monkeypatch, "open sesame")
    assert launcher.unlock("open sesame", now=0) is None and launcher.unlock(" open sesame ", now=0) is None
    assert launcher.unlock("guess", now=0) == "Wrong password."
    assert "Wait" in launcher.unlock("open sesame", now=1)  # even the right one, from any browser, until the wait ends
    assert launcher.unlock("open sesame", now=2) is None
    waits = []
    for _ in range(12):  # each wrong try doubles the wait, up to five minutes
        start = launcher._tries["until"]
        launcher.unlock("guess", now=start)
        waits.append(launcher._tries["until"] - start)
    assert waits[:3] == [2, 4, 8] and waits[-1] == 300
    assert launcher.unlock("", now=10 ** 6) == "Wrong password."  # an empty box never opens it
    fresh_lock(monkeypatch)
    assert launcher.unlock("anything", now=0) is None  # no password set: nothing to ask


def test_with_a_password_set_nothing_is_drawn_until_it_is_typed(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    clean_env(monkeypatch)
    fresh_lock(monkeypatch, "open sesame")
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    monkeypatch.setattr(launcher, "home", lambda: tmp_path)
    conn = store.open_db()
    demo.seed(conn)
    conn.close()
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    assert not at.sidebar.radio and not at.sidebar.selectbox and not at.dataframe  # no menu, no brief, no data
    at.text_input(key="unlock").set_value("guess").run()
    assert at.error and not at.sidebar.radio and at.text_input(key="unlock").value == ""
    launcher._tries["until"] = 0.0  # the wait is over
    at.text_input(key="unlock").set_value("open sesame").run()
    assert not at.exception and at.sidebar.radio(key="nav").value == "Dashboard"
    at.sidebar.radio(key="nav").set_value("Data and access").run()
    shown = " ".join(str(getattr(e, "value", "")) for kind in ("markdown", "caption", "text_input") for e in getattr(at, kind))
    assert "open sesame" not in shown and "no login" not in shown
    at.text_input(key="set_CI_PASSWORD").set_value("new words here")  # changing it asks for it again, at once
    at.button(key="settings_save").click().run()
    assert "CI_PASSWORD=new words here" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert not at.sidebar.radio and at.text_input(key="unlock")


def test_a_tab_opened_under_the_old_password_cannot_act_once_the_password_has_changed(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    clean_env(monkeypatch)
    fresh_lock(monkeypatch, "open sesame")
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    monkeypatch.setattr(launcher, "home", lambda: tmp_path)
    conn = store.open_db()
    demo.seed(conn)
    conn.close()
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"), default_timeout=30).run()
    at.text_input(key="unlock").set_value("open sesame").run()
    at.sidebar.radio(key="nav").set_value("Data and access").run()
    monkeypatch.setenv("CI_PASSWORD", "changed elsewhere")  # the owner changed it in another browser
    at.text_input(key="set_BRAVE_API_KEY").set_value("planted")
    at.button(key="settings_save").click().run()  # a button's action runs before the page, so before the gate
    assert not at.exception, at.exception
    assert "BRAVE_API_KEY" not in os.environ and not (tmp_path / ".env").exists()
    assert not at.sidebar.radio and at.text_input(key="unlock")
    source = (Path(__file__).parent.parent / "app.py").read_text(encoding="utf-8")
    import re
    actions = re.findall(r"on_(?:click|change)=(\w+)", source)  # every action is wrapped, the password box aside
    assert len(actions) >= 6 and set(actions) == {"_try_unlock", "unlocked_only"}, actions


def test_wrong_tries_from_many_browsers_at_once_are_counted_one_at_a_time():
    import threading
    assert isinstance(launcher._tries_lock, type(threading.Lock()))


def test_without_a_password_the_dashboard_opens_as_before_and_says_it_has_no_login(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    clean_env(monkeypatch)
    fresh_lock(monkeypatch)
    monkeypatch.setenv("CI_DB", str(tmp_path / "app.db"))
    monkeypatch.setattr(launcher, "home", lambda: tmp_path)
    conn = store.open_db()
    demo.seed(conn)
    conn.close()
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"), default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Data and access").run()
    assert not at.exception and any("no login" in c.value for c in at.caption)
