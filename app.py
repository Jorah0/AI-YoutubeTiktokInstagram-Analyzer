"""Reviewer screens: brief, candidates, review queue, shortlist, breakouts, experiments, data.

Run with: streamlit run app.py
"""
import json
import os
import re
import tempfile
from pathlib import Path

import anthropic
import launcher
import streamlit as st

from creator_intel import (adapters, analysis, assistant, broker, labeling, learner, pipeline, prepublish, ranking,
                           scripts, tracker)
from creator_intel import db as store
from creator_intel.taxonomy import DEFAULT_TAXONOMY, OBSERVED, RIGHTS, SETUP_LEVELS, Brief
from creator_intel.urls import parse_url

PLATFORMS = ["youtube", "tiktok", "instagram"]

st.set_page_config(
    page_title="Creator research", layout="wide",
    # Giving the page menu an entry makes it appear, and with it the Light / Dark theme chooser.
    menu_items={"About": "Creator research: find comparable creators, see the evidence, test one idea. "
                         "Runs on this computer only."})


def _try_unlock():
    """Runs once per value typed, before the page is drawn again, so the box can be emptied."""
    refused = launcher.unlock(st.session_state["unlock"])
    st.session_state["unlock"] = ""
    st.session_state["unlock_msg"] = refused
    if refused is None:
        st.session_state["unlocked"] = os.environ.get("CI_PASSWORD")


# With a password set, nothing below this is run for a browser that has not typed it. What a browser typed
# is remembered against the password itself, so changing the password asks everyone again.
def _locked() -> bool:
    password = os.environ.get("CI_PASSWORD")
    return bool(password) and st.session_state.get("unlocked") != password


def unlocked_only(action):
    """Wrap every on_click / on_change action in this. Streamlit runs a widget's action before the page,
    so before the gate below: a tab opened under an old password would otherwise get one more action."""
    def guarded(*args, **kwargs):
        if not _locked():
            return action(*args, **kwargs)
    return guarded


if _locked():
    st.title("Creator research")
    st.text_input("Password", type="password", key="unlock", on_change=_try_unlock)
    if st.session_state.get("unlock_msg"):
        st.error(st.session_state["unlock_msg"])
    st.caption("Forgotten? Delete the CI_PASSWORD line from the .env file beside the program and start it again.")
    st.stop()

db = store.open_db()

# Only the screen chosen in the sidebar is drawn, and Streamlit forgets the value of any input it did not
# draw. Writing each kept value back to itself at the start of a run stops that, so a half-written script
# or a typed name is still there when you come back to its screen. (Buttons and uploads cannot be kept.)
KEPT_INPUTS = ("script_pick", "script_title", "script_idea", "script_body", "script_opening", "script_format",
               "script_delivery", "reviewer", "tracker_origin", "tracker_tier", "scrape_mode",
               "scrape_limit", "scrape_target", "pp_platform", "pp_title", "viewer_creator", "ask_question", "ask_about", "rec_kind",
               "model_draft")
for _key in KEPT_INPUTS:
    if _key in st.session_state:
        st.session_state[_key] = st.session_state[_key]


def slugs(text: str) -> list[str]:
    out = (re.sub(r"\W+", "_", part.strip().lower()).strip("_") for part in text.split(","))
    return list(dict.fromkeys(s for s in out if s))


PLATFORM_NAME = tracker.PLATFORM_NAME


def ai_name() -> str:
    """Which model receives AI requests, safe to show: the name comes from a settings file."""
    return pipeline.md(labeling.describe())


def nice(value) -> str:
    return "(unknown)" if value is None else scripts.nice(value)


def pick(label, options, current=None, key=None, container=st):
    options = [None] + list(options)
    index = options.index(current) if current in options else 0
    return container.selectbox(label, options, index=index, format_func=nice, key=key)


def brief_form(b: Brief | None):
    b = b or Brief(name="", platform="youtube")
    with st.form("brief"):
        name = st.text_input("Channel or project name", b.name)
        c1, c2, c3 = st.columns(3)
        platform = c1.selectbox("Platform", PLATFORMS, index=PLATFORMS.index(b.platform))
        language = c2.text_input("Language code", b.language)
        fmt = c3.selectbox("Format", ["short", "long"], index=["short", "long"].index(b.format_family))
        objective = st.text_input("One growth objective", b.objective)
        constraints = st.text_area("Production constraints (time, equipment, budget)", b.constraints)
        topics = st.text_input("Topic labels reviewers may use (comma separated)", ", ".join(b.topics))
        promises = st.text_input("Audience promises: the need the content serves (comma separated)",
                                 ", ".join(b.audience_promises))
        related = st.text_area("Topics that count as related, one pair per line: a = b",
                               "\n".join(f"{x} = {y}" for x, y in b.related.get("topic", [])))
        st.markdown("**Your own channel**")
        o1, o2, o3 = st.columns(3)
        own_topic = o1.text_input("Your topic", b.owner.get("topic", ""))
        own_promise = o2.text_input("Your audience promise", b.owner.get("audience_promise", ""))
        setup = o3.selectbox("Your production setup", SETUP_LEVELS, index=SETUP_LEVELS.index(b.owner_setup),
                             format_func=nice)
        own_format = pick("Your format", DEFAULT_TAXONOMY["format_subtype"], b.owner.get("format_subtype"), container=o1)
        own_delivery = pick("Your delivery", DEFAULT_TAXONOMY["delivery"], b.owner.get("delivery"), container=o2)
        own_opening = pick("Your usual opening", DEFAULT_TAXONOMY["opening"], b.owner.get("opening"), container=o3)
        saved = st.form_submit_button("Save brief")
    if not saved:
        return
    if not name.strip():
        st.error("Give the brief a name.")
        return
    owner = {"topic": "".join(slugs(own_topic)[:1]), "audience_promise": "".join(slugs(own_promise)[:1]),
             "format_subtype": own_format, "delivery": own_delivery, "opening": own_opening}
    owner = {k: v for k, v in owner.items() if v}
    pairs = [slugs(line.replace("=", ",")) for line in related.splitlines()]
    new = Brief(id=b.id, name=name.strip(), platform=platform, language=language.strip() or "en", format_family=fmt,
                objective=objective, constraints=constraints, owner=owner, owner_setup=setup,
                topics=list(dict.fromkeys(slugs(topics) + slugs(own_topic)[:1])),
                audience_promises=list(dict.fromkeys(slugs(promises) + slugs(own_promise)[:1])),
                related={"topic": [p for p in pairs if len(p) == 2]})
    st.session_state["brief_id"] = store.save_brief(db, new)
    st.rerun()


def candidates_tab(brief: Brief):
    links = st.text_area("Creator profile links, one per line")
    if st.button("Import links") and links.strip():
        result = pipeline.import_links(db, brief.id, links.split())
        st.success(f"{result['added']} new candidates")
        for problem in result["rejected"]:
            st.text(problem)  # plain text: it echoes pasted input
    rows = db.execute(
        "SELECT a.handle, a.url, a.follower_count AS followers, count(DISTINCT p.id) AS posts, "
        "count(DISTINCT e.id) AS evidence FROM brief_account ba JOIN account a ON a.id = ba.account_id "
        "LEFT JOIN post p ON p.account_id = a.id LEFT JOIN evidence e ON e.post_id = p.id "
        "WHERE ba.brief_id = ? AND a.is_owner = 0 GROUP BY a.id", (brief.id,)).fetchall()
    st.dataframe([dict(r) for r in rows], width="stretch")
    st.caption("Run: search for new candidates, sample YouTube uploads, label permitted text, then rank. "
               "Stages without a key are skipped and say so.")
    if st.button("Run pipeline"):
        result = pipeline.run_all(db, brief.id)
        st.write(f"Run {result['run_id']}: {result['status']}")
        st.json(result["log"])


def review_tab(brief: Brief):
    taxonomy = brief.taxonomy()
    # dicts, not sqlite rows: Streamlit copies widget options and rows cannot be copied
    accounts = [dict(r) for r in db.execute(
        "SELECT a.id, a.handle, a.url, a.follower_count FROM brief_account ba JOIN account a ON a.id = ba.account_id "
        "WHERE ba.brief_id = ? AND a.is_owner = 0 ORDER BY a.handle", (brief.id,))]
    if not accounts:
        st.info("Import creator links first.")
        return
    acc = st.selectbox("Creator", accounts, format_func=lambda a: a["handle"] or a["url"])
    st.link_button("Open profile", acc["url"])
    followers = st.number_input("Followers or subscribers shown on the profile (0 = unknown)", min_value=0, step=1,
                                value=acc["follower_count"] or 0, key=f"followers_{acc['id']}")
    if followers != (acc["follower_count"] or 0):
        db.execute("UPDATE account SET follower_count = ? WHERE id = ?", (followers or None, acc["id"]))

    with st.form("post", clear_on_submit=True):
        url = st.text_input("Add a post link for this creator")
        published = st.date_input("Published on", value=None)
        sponsored = st.checkbox("Sponsored (leave out of the sample)")
        if st.form_submit_button("Add post"):
            try:
                ref = parse_url(url)
                if ref.platform != brief.platform:
                    raise ValueError(f"That is not a {brief.platform} link.")
                if ref.handle and ref.handle != acc["handle"]:
                    raise ValueError(f"That post belongs to {ref.handle}, not {acc['handle']}.")
                store.upsert_post(db, acc["id"], ref, sponsored=int(sponsored),
                                  published_at=f"{published.isoformat()}T00:00:00+00:00" if published else None)
            except ValueError as exc:
                st.error(str(exc))

    posts = [dict(r) for r in db.execute(
        "SELECT id, url, title FROM post WHERE account_id = ? ORDER BY published_at DESC, id DESC", (acc["id"],))]
    if posts:
        post = st.selectbox("Post", posts, format_func=lambda p: p["title"] or p["url"])
        st.link_button("Open post", post["url"])
        with st.form("note", clear_on_submit=True):
            c1, c2 = st.columns([1, 4])
            timestamp = c1.text_input("At (mm:ss)")
            note = c2.text_input("What you observed, in your own words")
            cols = st.columns(len(OBSERVED))
            chosen = {dim: pick(nice(dim), taxonomy[dim], key=f"note_{dim}", container=col)
                      for dim, col in zip(OBSERVED, cols)}
            if st.form_submit_button("Save observation"):
                if not note.strip():
                    st.error("Write what you observed; a label needs evidence.")
                else:
                    ev = store.add_evidence(db, post["id"], "reviewer_note", note.strip(), timestamp or None)
                    for dim, value in chosen.items():
                        if value:
                            store.set_tag(db, ev, dim, value, brief=brief)
                    st.success("Saved.")

    queue = db.execute(
        "SELECT t.*, e.text, e.source, p.url FROM tag t JOIN evidence e ON e.id = t.evidence_id "
        "JOIN post p ON p.id = e.post_id WHERE t.review_state = 'needs_review' AND p.platform = ?",
        (brief.platform,)).fetchall()
    st.subheader(f"Needs review ({len(queue)})")
    for t in queue:
        key = f"{t['evidence_id']}_{t['dimension']}"
        with st.container(border=True):
            st.markdown(f"**{nice(t['dimension'])}** proposed as **{pipeline.md(nice(t['value']))}** by "
                        f"{pipeline.md(t['model_version'])} · [post]({t['url']})")
            st.caption(f"{t['source']}: {pipeline.md(t['text'])}")
            c1, c2, c3 = st.columns([2, 1, 1])
            value = pick("Correct label", taxonomy[t["dimension"]], t["value"], key=f"fix_{key}", container=c1)
            if c2.button("Accept", key=f"ok_{key}") and value:
                store.set_tag(db, t["evidence_id"], t["dimension"], value, brief=brief)
                st.rerun()
            if c3.button("Reject", key=f"no_{key}"):
                store.set_tag(db, t["evidence_id"], t["dimension"], t["value"], review_state="rejected")
                st.rerun()


def shortlist_tab(brief: Brief):
    if st.button("Rank now"):
        pipeline.rank(db, brief.id)
    run_id = pipeline.latest_run(db, brief.id)
    if not run_id:
        st.info("Nothing ranked yet. Review three posts per candidate, then rank.")
        return
    cards, sparse = pipeline.get_shortlist(db, run_id)
    for c in cards:
        with st.container(border=True):
            st.subheader(f"{c['handle']} · {c['score']:.0f}/100 fit · {c['coverage']:.0%} coverage · "
                         f"{c['posts']} posts reviewed" + (" · stretch" if c["stretch"] else ""))
            size = ranking.size_note(c["size_ratio"])
            st.write(c["explanation"] + (f" Audience: {size}." if size else ""))
            st.markdown(f"[Profile]({c['url']}) · evidence: " + " · ".join(
                f"[{pipeline.md(e['timestamp'] or 'note')}: {pipeline.md(e['text'][:60])}]({e['url']})"
                for e in c["evidence"]))
            with st.expander("Correct a label"):
                ids = [e["id"] for e in c["evidence"]]
                tags = db.execute(f"SELECT * FROM tag WHERE review_state = 'accepted' AND evidence_id IN "
                                  f"({', '.join('?' * len(ids))})", ids).fetchall()
                for t in tags:
                    if st.button(f"{nice(t['dimension'])} = {nice(t['value'])} is wrong",
                                 key=f"flag_{c['account_id']}_{t['evidence_id']}_{t['dimension']}"):
                        store.flag_tag(db, t["evidence_id"], t["dimension"])
                        pipeline.rank(db, brief.id)
                        st.rerun()
    if not cards:
        st.info("No candidate has three evidence-backed posts and 80% coverage yet.")
    if sparse:
        st.subheader("Needs more review")
        st.dataframe([{"creator": c["handle"], "posts reviewed": c["posts"], "coverage": f"{c['coverage']:.0%}",
                       "unverified": ", ".join(nice(d) for d, m in c["match"].items() if m is None)}
                      for c in sparse], width="stretch")
    idea = pipeline.experiment_idea(db, run_id)
    st.subheader("One idea to test")
    if not idea:
        st.write("No repeated pattern among the shortlist yet.")
        return
    st.info(idea)
    metric = st.text_input("Primary metric you can actually export (decide before the trial)",
                           "average percentage viewed at 7 days")
    if st.button("Start this experiment") and metric.strip():
        pipeline.create_experiment(db, brief.id, idea, metric.strip())
        st.success("Experiment created. See the Experiments tab.")


def breakouts_tab():
    same_age = st.checkbox("Judge every post at day 7 (needs a reading taken within a week of publishing)")
    found = pipeline.breakouts(db, at_days=7 if same_age else None)
    st.caption("Only metrics from sources that permit derived scores are judged. YouTube API data is excluded "
               "until CI_YOUTUBE_RIGHTS says otherwise. A blank verdict means the data cannot tell.")
    st.dataframe([{k: b[k] for k in ("handle", "platform", "title", "views", "outlier", "breakout", "topic", "url")}
                  for b in found], width="stretch")


def experiments_tab(brief: Brief):
    rows = db.execute("SELECT * FROM experiment WHERE brief_id = ? ORDER BY id DESC", (brief.id,)).fetchall()
    if not rows:
        st.info("Create an experiment from the idea on the Shortlist tab.")
    for x in rows:
        with st.container(border=True):
            st.markdown(f"**Experiment {x['id']}** · metric: {x['primary_metric']} · window: {x['window_days']} days")
            st.write(x["hypothesis"])
            result = pipeline.experiment_result(db, x["id"])
            st.dataframe([{"pair": p["pair"], "publish first": p["first"], "baseline": a, "test": b}
                          for p, (a, b) in zip(json.loads(x["plan"]), result["pairs"])], width="stretch")
            st.markdown(f"Decision: **{result['decision']}** (directional evidence from a small trial)")
            with st.form(f"obs_{x['id']}", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                pair = c1.number_input("Pair", 1, len(result["pairs"]), step=1)
                variant = c2.selectbox("Variant", ["baseline", "test"])
                raw = c3.text_input("Value (blank if unavailable)")
                url = st.text_input("Post link")
                note = st.text_input("Note (topic change, promotion, why a value is missing)")
                if st.form_submit_button("Record"):
                    try:
                        value = float(raw) if raw.strip() else None
                    except ValueError:
                        st.error("Value must be a number or blank.")
                    else:
                        pipeline.record_observation(db, x["id"], int(pair), variant, value, url or None, note or None)
                        st.rerun()


# Settings that can be typed into the tool: (name, what it is for, is it a secret). All of these are read
# each time they are used, so a saved value works at once. The rights and scraper settings are read once at
# start; they stay in the .env file.
SETTINGS = [
    ("BRAVE_API_KEY", "Brave search key: finds candidate creators", True),
    ("YOUTUBE_API_KEY", "YouTube Data API key: samples uploads, views and subscribers", True),
    ("ANTHROPIC_API_KEY", "Anthropic key: Claude for labeling, feedback and summaries", True),
    ("APIFY_TOKEN", "Apify token: scrapes videos (each scrape costs credits)", True),
    ("CI_AI_URL", "Another AI server instead of Claude, e.g. `http://127.0.0.1:11434/v1`", False),
    ("CI_AI_MODEL", "The model to use on that server, e.g. llama3.1", False),
    ("CI_AI_KEY", "That server's key, if it wants one", True),
    ("CI_WEBHOOK_URL", "Discord or Slack webhook for the weekly alert", True),
    ("CI_PASSWORD", "Password to open this dashboard (you are asked for it straight after saving)", True),
]


def settings_form():
    env_file = launcher.home() / ".env"
    st.subheader("Keys and AI model")
    st.caption(f"Saved to the .env file beside the program ({pipeline.md(env_file)}) and in force at once. A key is "
               "never shown again after saving. Every key is optional: a stage without its key is skipped.")
    state = st.session_state

    def save():
        """Runs before the page is drawn again, so the boxes can be emptied: a key must not stay on screen."""
        changes = {**{name: state[f"set_{name}"] for name, _, _ in SETTINGS if state.get(f"set_{name}", "").strip()},
                   **{name: "" for name in state.get("settings_remove", [])}}
        try:
            if changes:
                dropped = launcher.save_env(env_file, changes)
                state["settings_msg"] = ("success", f"{len(changes)} setting(s) saved." + (
                    " The key of the previous AI server was removed, so it is not sent to the new address: "
                    "type the new server's key if it needs one." if dropped else ""))
            else:
                state["settings_msg"] = ("warning", "Nothing to save: type a value or choose something to remove.")
        except (ValueError, OSError) as exc:
            state["settings_msg"] = ("warning", pipeline.md(str(exc)) + ". Nothing was saved.")
        finally:  # whatever happened, nothing typed stays behind
            for name, _, _ in SETTINGS:
                state[f"set_{name}"] = ""
            state["settings_remove"] = []

    with st.form("settings"):
        for name, purpose, secret in SETTINGS:
            is_set = bool(os.environ.get(name))
            current = "" if secret or not is_set else os.environ[name]
            st.text_input(
                f"{purpose} · {'set' if is_set else 'not set'}", key=f"set_{name}", type="password" if secret else "default",
                placeholder=(f"now: {current}" if current else "set: leave empty to keep it" if is_set else "not set"))
        st.multiselect("Remove these settings", [name for name, _, _ in SETTINGS if os.environ.get(name)],
                       key="settings_remove")
        st.form_submit_button("Save settings", key="settings_save", on_click=unlocked_only(save))
    if state.get("settings_msg"):
        kind, text = state.pop("settings_msg")
        (st.success if kind == "success" else st.warning)(text)
    st.caption(("This dashboard asks for its password. The password sits in the .env file as plain text, like the "
                "keys: it keeps out other people using this computer, not someone who can read that file. "
                if os.environ.get("CI_PASSWORD") else
                "This dashboard has no login: anyone who can use this computer while it runs can change these. "
                "Set a password above to change that. ") +
               "A value also set in Windows itself wins again at the next start. "
               "A server that is not this computer must also be confirmed once on the Assistant screen. What each "
               "source may be used for (CI_YOUTUBE_RIGHTS, CI_BROKER_RIGHTS) is set in the .env file and read at start.")


def data_tab(brief: Brief):
    settings_form()
    with st.form("own", clear_on_submit=True):
        st.markdown("**Your own channel** (kept out of the candidates; its posts are what exported metrics match)")
        channel = st.text_input("Your channel link")
        own_posts = st.text_area("Your post links, one per line")
        if st.form_submit_button("Save"):
            try:
                owner = store.upsert_account(db, parse_url(channel), is_owner=1)
                for link in own_posts.split():
                    store.upsert_post(db, owner, parse_url(link))
                st.success("Saved.")
            except ValueError as exc:
                st.text(str(exc))
    upload = st.file_uploader("Owner metrics CSV (post_url, views, likes, comments, shares, saves, reach)", type="csv")
    if upload and st.button("Import metrics"):
        report = adapters.import_metrics_csv(db, upload)
        st.success(f"{report['imported']} rows imported")
        for line in report["skipped"]:
            st.text(line)  # plain text: it echoes file contents
    st.subheader("What each source permits")
    st.dataframe([{"source": s, "rights": r} for s, r in RIGHTS.items()], width="stretch")
    c1, c2 = st.columns(2)
    source = c1.selectbox("Access revoked or expired for", list(RIGHTS))
    sure = c1.checkbox("Remove everything this source supplied and every match built on it")
    if c1.button("Remove") and sure:
        st.success(f"Removed {store.revoke_source(db, source)} evidence items.")
    if c2.button("Remove expired evidence"):
        st.success(f"Removed {store.purge_expired(db)} items.")
    st.subheader("Usage")
    st.dataframe([dict(r) for r in db.execute("SELECT * FROM usage ORDER BY day DESC")], width="stretch")
    st.download_button("Download weekly digest (Markdown)", pipeline.digest(db, brief.id), "digest.md")


def dashboard_tab(brief: Brief):
    seen, steps = analysis.overview(db, brief.id), analysis.progress(db, brief.id)
    gate = "pending" if seen["gate"] is None else "passed" if seen["gate"] else "failed"  # short: must fit the tile
    cols = st.columns(6)
    for col, (label, value) in zip(cols, [
            ("Creators in the brief", seen["candidates"]), ("Creators reviewed", seen["reviewed_creators"]),
            ("On the shortlist", seen["shortlist"]), ("Labels waiting for you", seen["waiting_review"]),
            ("Breakout posts", seen["breakouts"]), ("Launch gate", gate)]):
        col.metric(label, value)

    finished = sum(s["done"] for s in steps)
    st.progress(finished / len(steps), text=f"Plan progress: {finished} of {len(steps)} steps done")
    todo = analysis.next_step(steps)
    if todo:
        st.info(f"Next: {todo['step']} (week {todo['week']}). {todo['detail']} Where: {todo['where']}.")
    else:
        st.success("Every step of the four-week plan is done.")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("The plan, step by step")
        st.dataframe([{"week": s["week"], "step": s["step"], "status": "done" if s["done"] else "to do",
                       "where things stand": s["detail"], "screen": s["where"]} for s in steps],
                     width="stretch", hide_index=True)
        st.markdown("**Growth tools** (beside the plan; scraping costs data-broker credits, so none is required)")
        st.dataframe([{"step": s["step"], "status": "done" if s["done"] else "to do", "where things stand": s["detail"],
                       "screen": s["where"]} for s in assistant.tool_steps(db, brief.id)], width="stretch", hide_index=True)
    with right:
        st.subheader("Creators worth studying")
        cards = pipeline.get_shortlist(db, seen["run_id"])[0] if seen["run_id"] else []
        if cards:
            st.dataframe([{"creator": c["handle"], "fit": round(c["score"]), "coverage": f"{c['coverage']:.0%}",
                           "posts": c["posts"], "note": "stretch" if c["stretch"] else ""} for c in cards],
                         width="stretch", hide_index=True)
            idea = pipeline.experiment_idea(db, seen["run_id"])
            st.caption("Idea to test: " + (idea or "no repeated pattern among the shortlist yet."))
        else:
            st.caption("Nothing ranked yet.")
        if seen["needs_more_review"]:
            st.caption(f"Creators waiting on more review before they can be ranked: {seen['needs_more_review']}.")

    st.subheader("Last run and connectors")
    run_col, doctor_col = st.columns(2)
    last = seen["last_run"]
    if last:
        run_col.write(f"Run {last['id']}: {last['status']}, started {last['started_at'][:16].replace('T', ' ')} UTC")
        notes = json.loads(last["log"])
        if notes:
            run_col.dataframe([{"stage": k, "result": v} for k, v in notes.items()], width="stretch", hide_index=True)
    else:
        run_col.caption("No run yet.")
    doctor_col.dataframe(analysis.doctor(db), width="stretch", hide_index=True)


SCRIPT_LABELS = (("script_opening", "opening", "opening"), ("script_format", "format_subtype", "format"),
                 ("script_delivery", "delivery", "delivery"))
SCRIPT_KEYS = ("script_pick", "script_title", "script_idea", "script_body", "script_feedback", "script_msg",
               *(key for key, _, _ in SCRIPT_LABELS))


def script_tab(brief: Brief):
    state = st.session_state
    if state.get("script_brief") != brief.id:  # a different brief: start with a clean desk
        for key in SCRIPT_KEYS:
            state.pop(key, None)
        state["script_brief"] = brief.id
    if "script_pick_next" in state:  # set by Save on the previous run; a widget's value can only change before it is drawn
        state["script_pick"] = state.pop("script_pick_next")
    for key, dim, _ in SCRIPT_LABELS:
        state.setdefault(key, brief.owner.get(dim))
    patterns = scripts.peer_patterns(db, brief.id)
    saved = {s["id"]: s for s in scripts.list_scripts(db, brief.id)}
    taxonomy = brief.taxonomy()

    def declared() -> dict:
        return {dim: state[key] for key, dim, _ in SCRIPT_LABELS if state.get(key)}

    def load():
        draft = saved.get(state.get("script_pick"))
        state["script_title"] = draft["title"] if draft else ""
        state["script_body"] = draft["body"] if draft else ""
        state["script_feedback"] = draft["feedback"] if draft else None
        for key, dim, _ in SCRIPT_LABELS:
            state[key] = (draft["declared"] if draft else brief.owner).get(dim)

    def start_outline():
        if state.get("script_body", "").strip():
            state["script_msg"] = ("warning", "Your draft is not empty. Clear it first to start from an outline.")
        else:
            state["script_body"] = scripts.outline(brief, patterns, state.get("script_idea", ""))

    def with_model(work):
        """Run something that calls Claude; turn the ways it can fail into a message instead of a crash."""
        try:
            work()
        except ValueError as exc:
            state["script_msg"] = ("warning", str(exc))
        except labeling.NoCredential as exc:
            state["script_msg"] = ("warning", f"AI is not set up: {exc}")
        except adapters.SourceError as exc:
            state["script_msg"] = ("error", str(exc))
        except anthropic.APIError as exc:
            state["script_msg"] = ("error", f"The AI service refused or failed ({type(exc).__name__}). Nothing was changed.")

    def run_feedback():
        def work():
            if not state.get("script_body", "").strip():
                raise ValueError("Write or paste a draft first.")
            state["script_feedback"] = scripts.ai_feedback(db, brief.id, state["script_body"], declared())
        with_model(work)

    def run_draft():
        def work():
            got = scripts.ai_draft(db, brief.id, state.get("script_idea", ""), declared())
            state["script_body"] = got["script"]
            state["script_title"] = state.get("script_title") or got["title"]
            state["script_feedback"] = None
            state["script_msg"] = ("success", "Draft written. The choices it made: " + " ".join(got["notes"]))
        with_model(work)

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Your draft")
        st.selectbox("Draft", [None, *saved], key="script_pick", on_change=unlocked_only(load),
                     format_func=lambda i: "New draft" if i is None else saved[i]["title"])
        st.text_input("Title", key="script_title")
        st.text_input("What is this video about? (used for the outline and the AI draft)", key="script_idea")
        for col, (key, dim, label) in zip(st.columns(3), SCRIPT_LABELS):
            # a saved draft may carry a label that has since left the brief's list: keep it selectable, not lost
            extra = [state[key]] if state.get(key) and state[key] not in taxonomy[dim] else []
            col.selectbox(f"Your {label}", [None, *taxonomy[dim], *extra], key=key, format_func=nice)
        st.text_area("Script", key="script_body", height=360, max_chars=scripts.MAX_DRAFT_CHARS,
                     placeholder="Write or paste the words you will say. Or start from an outline.")
        buttons = st.columns(5)
        buttons[0].button("Outline", on_click=unlocked_only(start_outline), width="stretch", help="Fill an empty draft with a skeleton shaped by the accepted creators")
        buttons[1].button("Analyze draft", type="primary", width="stretch")  # the analysis below is always current
        save_now = buttons[2].button("Save draft", width="stretch")
        buttons[3].button("AI feedback", on_click=unlocked_only(run_feedback), width="stretch")
        buttons[4].button("Draft with AI", on_click=unlocked_only(run_draft), width="stretch")
        st.caption("The two AI buttons send your draft, your brief and reviewers' notes on the compared creators to "
                   f"the AI model ({ai_name()}). Everything else on this screen stays on this computer.")
        if save_now:
            try:
                state["script_pick_next"] = scripts.save_script(
                    db, brief.id, state.get("script_title", ""), state.get("script_body", ""), declared(),
                    script_id=state.get("script_pick"), feedback=state.get("script_feedback"))
                st.rerun()
            except ValueError as exc:
                st.error(str(exc).capitalize() + ".")
        message = state.pop("script_msg", None)
        if message:
            getattr(st, message[0])(pipeline.md(message[1]))

    with right:
        st.subheader("What they do")
        st.caption(f"Based on {patterns['basis']}: {len(patterns['accepted'])} accepted, "
                   f"{len(patterns['rejected'])} rejected. A pattern needs two creators.")
        if patterns["common"]:
            st.dataframe([{"": nice(d), "accepted creators share": nice(f["label"]), "how many": f"{f['count']} of {f['of']}"}
                          for d, f in patterns["common"].items()], width="stretch", hide_index=True)
        else:
            st.info("No patterns yet. Rank the brief, then have reviewers accept or reject suggestions on the "
                    "Analysis screen.")
        if patterns["avoid"]:
            st.markdown("**Rejected creators share**")
            st.dataframe([{"": nice(d), "label": nice(f["label"]), "how many": f"{f['count']} of {f['of']}"}
                          for d, f in patterns["avoid"].items()], width="stretch", hide_index=True)
        with st.expander("Reviewers' notes on the accepted creators"):
            for peer in patterns["accepted"]:
                st.markdown(f"[{peer['handle']}]({peer['url']})")
                for note in peer["notes"]:
                    st.caption(pipeline.md(note))

    body = state.get("script_body", "")
    if not body.strip():
        return
    report = scripts.analyze(body, declared(), patterns, brief.format_family)
    stats = report["stats"]
    st.subheader("How this draft compares")
    for col, (label, value) in zip(st.columns(4), [
            ("Words", stats["words"]), ("Spoken length", f"{stats['seconds']} s"),
            ("Length band", nice(stats["band"])), ("First line", f"{stats['opening_words']} words")]):
        col.metric(label, value)
    st.dataframe(report["findings"], width="stretch", hide_index=True)

    feedback = state.get("script_feedback")
    if feedback:
        st.subheader("AI comparison")
        st.caption(f"Compared with {feedback.get('accepted_creators', 0)} creators, {feedback.get('basis', '')}. "
                   "Treat it as a second opinion; the table above is the part you can check.")
        st.write(pipeline.md(feedback["summary"]))
        if feedback["strengths"]:
            st.markdown("**Already in line with them**\n" + "\n".join(f"- {pipeline.md(s)}" for s in feedback["strengths"]))
        if feedback["gaps"]:
            st.dataframe([{"about": nice(g["dimension"]), "they": g["peers"], "your draft": g["script"],
                           "try": g["suggestion"]} for g in feedback["gaps"]], width="stretch", hide_index=True)
        if feedback["opening_alternatives"]:
            st.markdown("**Other ways to open**\n" + "\n".join(f"- {pipeline.md(o)}" for o in feedback["opening_alternatives"]))
        if feedback.get("dropped_copies"):
            st.caption(f"{feedback['dropped_copies']} suggested openings were removed for repeating a reviewer's note.")


# The sidebar menu, in the order the work usually goes: screen name -> one line on what it is for.
SCREENS = {
    "Dashboard": "Where you are in the plan",
    "Candidates": "Add creators to compare with",
    "Review": "Note what their posts show",
    "Shortlist": "Who fits, with the evidence",
    "Creator viewer": "Everything held on one creator",
    "Analysis": "Reviewers' verdicts and the launch gate",
    "Video tracker": "Scraped videos, outliers, what to improve",
    "Assistant": "Ask questions, train your own model",
    "Breakouts": "Posts far above a creator's usual",
    "Script builder": "Write your video against what works",
    "Pre-publish": "Check title, cover and sound",
    "Experiments": "Test one idea and decide",
    "Brief": "Your channel and what you can produce",
    "Data and access": "Your channel, imports, keys, removal",
}
SCRAPE_MODES = {"Videos on a theme": "theme", "Another creator's channel": "peer", "My own channel": "own"}


def tracker_tab(brief: Brief):
    state = st.session_state
    catalog, tuning = tracker.rows(db, brief.id), tracker.get_tuning(db, brief.id)
    mine = [r for r in catalog if r["origin"] == "own"]
    for col, (label, value) in zip(st.columns(4), [
            ("Videos tracked", len(catalog)), ("Yours", len(mine)),
            ("Outliers", sum(r["tier"] == "outlier" for r in catalog)),
            ("Strong", sum(r["tier"] == "strong" for r in catalog))]):
        col.metric(label, value)
    st.caption("Explosivity is a video's views per day against that creator's own usual. 3.0 or more is an outlier, "
               "1.5 is strong. A creator with fewer than three other videos is compared with the other creators in this list.")

    with st.expander("Scrape more videos (uses data-broker credits)", expanded=not catalog):
        has_token = bool(os.environ.get("APIFY_TOKEN"))
        if not has_token:
            st.info("Scraping needs an Apify token. Enter it on the Data and access screen.")
        c1, c2, c3 = st.columns([2, 2, 1])
        mode = SCRAPE_MODES[c1.selectbox("What to fetch", list(SCRAPE_MODES), key="scrape_mode")]
        chosen = c2.multiselect("Platforms", PLATFORMS, default=[p for p in PLATFORMS if p not in tuning["off_platforms"]],
                                format_func=PLATFORM_NAME.get, key=f"scrape_platforms_{brief.id}") if mode == "theme" else []
        if mode != "theme":
            c2.caption("The platform is read from the channel link.")
        limit = c3.number_input("How many (per platform)", 1, broker.MAX_ITEMS, 20, key="scrape_limit")
        target = st.text_input("Search words or hashtag" if mode == "theme" else "Channel link", key="scrape_target")
        st.caption("Each platform is one scrape: the request goes to Apify and is charged by them. Scraping is outside "
                   "the platforms' official APIs; whether your use is allowed is governed by their terms.")
        if st.button("Scrape", disabled=not has_token) and target.strip():
            try:
                with st.spinner("Scraping. This can take a few minutes for each platform."):
                    if mode == "theme":
                        if not chosen:
                            raise ValueError("choose at least one platform")
                        results = broker.scrape_many(db, brief.id, chosen, target, limit=int(limit))
                    else:
                        site = parse_url(target).platform
                        results = {site: broker.scrape(db, brief.id, site, channel=target, own=mode == "own", limit=int(limit))}
                state["tracker_failed"] = any("error" in got for got in results.values())
                state["tracker_msg"] = " ".join(
                    f"{PLATFORM_NAME[site]}: " + (f"not scraped ({pipeline.md(got['error'])})." if "error" in got else
                                                  f"{got['fetched']} items came back, {got['usable']} were videos we could "
                                                  f"read, {got['added']} are new to this list.")
                    for site, got in results.items())
                st.rerun()
            except (ValueError, broker.NoToken, adapters.AccessDenied, adapters.SourceError) as exc:
                st.error(pipeline.md(str(exc)))
    if state.get("tracker_msg"):
        (st.warning if state.pop("tracker_failed", False) else st.success)(state.pop("tracker_msg"))
    if not catalog:
        return

    f1, f2 = st.columns(2)
    origins = f1.multiselect("Show", ["own", "theme", "peer"], default=["own", "theme", "peer"], key="tracker_origin",
                             format_func={"own": "my videos", "theme": "theme searches", "peer": "other channels"}.get)
    tiers = f2.multiselect("Tier", ["outlier", "strong", "typical", None], default=["outlier", "strong", "typical", None],
                           key="tracker_tier", format_func=lambda t: t or "not enough data")
    shown = [r for r in catalog if r["origin"] in origins and r["tier"] in tiers]

    def pct(value):
        return None if value is None else f"{value:.1%}"

    st.dataframe([{
        "creator": r["handle"], "platform": PLATFORM_NAME[r["platform"]],
        "whose": {"own": "mine", "theme": "theme", "peer": "channel"}[r["origin"]],
        "tier": r["tier"] or "not enough data",
        "explosivity": None if r["explosivity"] is None else round(r["explosivity"], 1),
        "vs this list": None if r["theme_ratio"] is None else round(r["theme_ratio"], 1),
        "views a day": None if r["views_per_day"] is None else round(r["views_per_day"]),
        "views": r["views"], "engagement": pct(r["engagement"]), "shares per view": pct(r["share_rate"]),
        "length (s)": r["duration_sec"], "age (days)": None if r["age_days"] is None else round(r["age_days"]),
        "compared with": r["compared_with"], "video": r["title"], "link": r["url"]} for r in shown],
        width="stretch", hide_index=True, column_config={"link": st.column_config.LinkColumn("link", display_text="open")})
    fastest = [r for r in shown if (r["explosivity"] or r["theme_ratio"]) is not None][:15]
    if fastest:
        st.markdown("**Fastest against their usual**")
        st.bar_chart([{"video": f"{r['handle']} {(r['title'] or r['external_id'])[:30]}", "times the usual":
                       round(r["explosivity"] or r["theme_ratio"], 2)} for r in fastest], x="video", y="times the usual",
                     horizontal=True, sort="-times the usual")  # fastest at the top

    st.subheader("What to improve")
    with st.expander("Tailor this: which platforms count, which one comes first, which layers are compared"):
        t1, t2 = st.columns(2)
        on_platforms = t1.multiselect("Platforms to learn from", PLATFORMS, format_func=PLATFORM_NAME.get, key=f"tune_platforms_{brief.id}",
                                      default=[p for p in PLATFORMS if p not in tuning["off_platforms"]])
        firsts = [None] + on_platforms
        first = t2.selectbox("Put first", firsts, key=f"tune_priority_{brief.id}",
                             index=firsts.index(tuning["priority"]) if tuning["priority"] in firsts else 0,
                             format_func=lambda p: "No platform first" if p is None else PLATFORM_NAME[p],
                             help="Its videos are used alone once it has three of yours and three outliers.")
        on_layers = st.multiselect("Layers to compare", list(tracker.LAYERS), key=f"tune_layers_{brief.id}",
                                   default=[k for k in tracker.LAYERS if k not in tuning["off_layers"]],
                                   format_func=lambda k: tracker.LAYERS[k][0])
        st.caption("A platform or layer switched off is left out of the table below, the AI reading and the AI labeling queue.")
        if st.button("Save", key="tune_save"):
            try:
                tracker.set_tuning(db, brief.id, off_platforms=[p for p in PLATFORMS if p not in on_platforms],
                                   priority=first, off_layers=[k for k in tracker.LAYERS if k not in on_layers])
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc).capitalize() + ".")
    compared = tracker.compare(catalog, tuning)
    st.caption(f"Compared on {compared['basis']}. A layer is compared only when three of your videos and three "
               "outliers have the number; otherwise it says what is missing.")
    st.dataframe([{"layer": found["factor"], "checked?": found["status"],
                   "yours with it": found["yours_n"], "outliers with it": found["outliers_n"], "yours": found["yours"],
                   "outliers": found["outliers"], "gap": found["gap"] if found["status"] == "checked" else ""}
                  for found in compared["layers"]], width="stretch", hide_index=True)
    if compared["mine"] < tracker.MIN_GROUP or compared["outliers"] < tracker.MIN_GROUP:
        st.info("This comparison needs at least three of your own videos and three outliers from others. "
                "Scrape your channel and a theme or two.")
    b1, b2 = st.columns(2)
    if b1.button("Queue fast videos for AI labeling", help="Only outliers and strong videos are sent to the model, "
                 "when the pipeline next runs. This keeps the AI cost down."):
        st.success(f"{tracker.promote_for_ai(db, brief.id)} videos queued. Run the pipeline on the Candidates screen.")
    if b2.button("AI reading of the comparison", help="Sends the numbers above, your video titles and the fast "
                 f"videos' captions to the AI model ({ai_name()})."):
        try:
            state["tracker_advice"] = tracker.ai_improvements(db, brief.id)
        except ValueError as exc:
            st.warning(str(exc).capitalize() + ".")
        except labeling.NoCredential as exc:
            st.warning(f"AI is not set up: {exc}")
        except (adapters.SourceError, anthropic.APIError) as exc:
            st.error(f"The AI service refused or failed ({type(exc).__name__}).")
    advice = state.get("tracker_advice")
    if advice:
        st.caption(f"Based on {advice['based_on']['yours']} of your videos and {advice['based_on']['fast']} fast ones. "
                   "A second opinion on the numbers, not a finding.")
        st.write(pipeline.md(advice["summary"]))
        st.dataframe([{"about": p["factor"], "what the numbers show": p["evidence"], "try": p["change"]}
                      for p in advice["priorities"]], width="stretch", hide_index=True)


def ai_try(work):
    """Run something that asks the AI model; the ways it can fail become a message instead of a crash."""
    try:
        return work()
    except ValueError as exc:
        st.warning(str(exc).capitalize() + ".")
    except labeling.NoCredential as exc:
        st.warning(f"AI is not set up: {exc}")
    except adapters.SourceError as exc:
        st.error(pipeline.md(str(exc)).capitalize() + ".")
    except anthropic.APIError as exc:
        st.error(f"The AI service refused or failed ({type(exc).__name__}).")
    return None


def assistant_tab(brief: Brief):
    state, kept_answer = st.session_state, f"ask_result_{brief.id}"
    st.caption(f"AI model in use: {ai_name()}. Any OpenAI-compatible server can take its place, including "
               "one on this computer: set it on the Data and access screen. "
               "\"Answer from my data\" uses no AI at all.")
    try:
        new_host = labeling.unconfirmed(db)
    except adapters.SourceError:
        new_host = None  # the address is not usable at all; the caption above says so
    if new_host:
        st.warning(f"The settings name an AI server that is not on this computer: **{pipeline.md(new_host)}**. Nothing is "
                   "sent to it until you confirm it. If you did not set this yourself, do not confirm: remove "
                   "CI_AI_URL from the .env file instead.")
        if st.button(f"Send AI requests to {new_host}", key="ai_trust"):
            labeling.trust(db)
            st.rerun()
    c1, c2 = st.columns([3, 1])
    question = c1.text_input("Your question", key="ask_question", placeholder=assistant.DEFAULT_QUESTION)
    about = c2.selectbox("About", ["Everything"] + list(assistant.ALSO), key="ask_about")
    about = None if about == "Everything" else about
    b1, b2 = st.columns(2)

    def keep(got):
        if got:
            state[kept_answer] = {**got, "at": store.now_iso()[11:16]}

    if b1.button("Answer from my data", key="ask_local", help="Shows the facts that match the question. No AI, no cost."):
        keep(assistant.answer(db, brief.id, question, about=about))
    if b2.button("Ask the AI model", key="ask_ai", help="Sends the question and the facts below (handles, labels and "
                 f"numbers; no captions, titles or notes) to {ai_name()}."):
        keep(ai_try(lambda: assistant.answer(db, brief.id, question, about=about, use_ai=True)))
    result = state.get(kept_answer)
    if result:
        st.caption(f"From the data as it stood at {result['at']} UTC. Ask again after scraping, training or tailoring.")
        if result["answer"]:
            st.write(pipeline.md(result["answer"]))
            st.caption("The model's answer. It was given the facts below and named these as the ones it used; "
                       "check it against them.")
        for name, lines in result["sections"].items():
            st.markdown(f"**{name}**")
            st.markdown("\n".join("- " + pipeline.md(line) for line in lines))

    st.subheader("What to do next")
    st.caption("Recommendations for one kind of content, each with the numbers it rests on. \"From my data\" uses the "
               "checked layers, the judged creators' patterns and your own model. The AI model also reads the written "
               "material it is permitted to (reviewers' notes, captions of fast videos), summarises it and adds its own.")
    r1, r2, r3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    kind = r1.selectbox("For", list(assistant.KINDS), format_func=lambda k: assistant.KINDS[k].capitalize(), key="rec_kind")
    kept_plan = f"rec_result_{brief.id}"
    if r2.button("From my data", key="rec_local"):
        state[kept_plan] = assistant.recommend(db, brief.id, kind)
    if r3.button("Summarise with AI", key="rec_ai", help=f"Sends the options, the facts and the permitted written "
                 f"material to {ai_name()}."):
        state[kept_plan] = ai_try(lambda: assistant.recommend(db, brief.id, kind, use_ai=True)) or state.get(kept_plan)
    plan = state.get(kept_plan)
    if plan:
        st.markdown(f"**For {assistant.KINDS[plan['kind']]}**")
        st.caption(f"From the data as it stood at {plan['at']} UTC. Ask again after scraping, training or tailoring.")
        if plan["note"]:
            st.info(plan["note"])
        if plan["summary"]:
            st.write(pipeline.md(plan["summary"]))
            st.caption("The AI model's summary and recommendations. Check them against the data's own options below.")
            if plan["ai_options"]:
                st.dataframe([{"do": o["do"], "why": o["why"]} for o in plan["ai_options"]], width="stretch", hide_index=True)
            else:
                st.caption("None of the model's recommendations were kept: each repeated a caption's or a note's wording.")
        if plan["options"]:
            st.dataframe([{"do": o["do"], "why": o["why"], "from": o["source"]} for o in plan["options"]],
                         width="stretch", hide_index=True)
        else:
            st.info("Nothing to recommend from the data yet: track more videos and review more creators.")
        if plan["unchecked"]:
            st.caption("Not compared, so nothing rests on it: " + "; ".join(pipeline.md(line) for line in plan["unchecked"]))

    st.subheader("Your own model")
    st.caption("A small model trained on this computer from this brief's tracked videos. It learns which traits the "
               "fast videos shared (caption words, caption length, video length, platform), is tested on videos it "
               "was not shown, and says so when it does no better than guessing.")
    if st.button("Train on the tracked videos", key="model_train"):
        report = learner.fit(db, brief.id)
        if not report["trained"]:
            st.warning(f"Not trained: {report['why']}.")
    kept = learner.load(db, brief.id)
    if kept:
        report = kept["report"]
        for col, (label, value) in zip(st.columns(3), [
                ("Videos learned from", report["examples"]), ("Right on videos it was not shown", f"{report['accuracy']:.0%}"),
                ("Always guessing scores", f"{report['baseline']:.0%}")]):
            col.metric(label, value)
        if report["useful"]:
            st.success("It does better than guessing. Its score is a resemblance to past fast videos, not a forecast.")
        else:
            st.warning("No better than guessing so far: treat its scores as noise. More tracked videos may help.")
        st.caption(f"Trained {report['trained_at'][:10]}, {'with' if report['with_captions'] else 'without'} caption "
                   "words. Train again after scraping more.")
        d1, d2, d3 = st.columns([3, 1, 1])
        draft = d1.text_input("A title or caption you are considering", key="model_draft")
        site = d2.selectbox("Platform", PLATFORMS, index=PLATFORMS.index(brief.platform), format_func=PLATFORM_NAME.get,
                            key="model_platform")
        seconds = d3.number_input("Length in seconds", 0, 7200, 0, key="model_seconds", help="0 = not decided yet")
        if draft.strip():
            got = learner.score(db, brief.id, draft, site, seconds or None)
            shown = "over 95%" if got["chance"] > 0.95 else "under 5%" if got["chance"] < 0.05 else f"{got['chance']:.0%}"
            st.write(f"Resembles the fast videos: **{shown}** (fast videos were {got['base']:.0%} of what it learned "
                     "from). Read it as a ranking between drafts, not as odds: this kind of model overstates "
                     "how sure it is.")
            for title, pulls in (("Pulls towards fast", got["up"]), ("Pulls away", got["down"])):
                if pulls:
                    st.caption(f"{title}: " + ", ".join(pipeline.md(name) for name, _ in pulls))

    st.markdown("**Teach a local AI model**")
    st.caption("The tracked videos as a training file (JSONL, chat format) for fine-tuning a model you run yourself. "
               "The file contains the scraped captions; keep it on this computer.")
    try:
        lines = learner.training_examples(db, brief.id)
    except ValueError as exc:
        st.info(str(exc).capitalize() + ".")
    else:
        if lines:
            st.download_button(f"Download {len(lines)} training examples", file_name=f"training-brief-{brief.id}.jsonl",
                               data="".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
        else:
            st.caption("Nothing to export yet: track some videos first.")


def candidates_of(brief: Brief) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT a.id, a.handle, a.url FROM brief_account ba JOIN account a ON a.id = ba.account_id "
        "WHERE ba.brief_id = ? AND a.is_owner = 0 ORDER BY a.handle", (brief.id,))]


def viewer_tab(brief: Brief):
    accounts = candidates_of(brief)
    if not accounts:
        st.info("Import creator links first.")
        return
    acc = st.selectbox("Creator", accounts, format_func=lambda a: a["handle"] or a["url"], key="viewer_creator")
    view = analysis.creator_view(db, acc["id"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Posts tracked", len(view["posts"]))
    c2.metric("Posts with evidence", sum(1 for p in view["posts"] if p["evidence"]))
    c3.metric("Followers", view["followers"] or "unknown")
    st.link_button("Open profile", view["url"])
    st.markdown("**What their reviewed posts have in common** (a label needs two supporting posts)")
    st.dataframe([{"dimension": nice(d), "label": nice(view["profile"].get(d))} for d in OBSERVED], width="stretch")
    st.markdown("**Posts**")
    st.dataframe([{"post": p["title"] or p["url"], "published": (p["published_at"] or "")[:10],
                   "sponsored": p["sponsored"], **{nice(d): nice(p["labels"].get(d)) for d in OBSERVED}}
                  for p in view["posts"]], width="stretch")
    if not view["posts"]:
        return
    index = st.selectbox("Look at one post", range(len(view["posts"])), key=f"viewer_post_{acc['id']}",
                         format_func=lambda i: view["posts"][i]["title"] or view["posts"][i]["url"])
    post = view["posts"][index]
    st.link_button("Open post", post["url"])
    for e in post["evidence"]:
        st.caption(f"{e['source']} {pipeline.md(e['timestamp'] or '')}: {pipeline.md(e['text'])}")
    if post["snapshots"]:
        st.markdown("**Views over time** (every reading is kept)")
        st.line_chart([{"read on": s["captured_at"][:10], "views": s["views"]} for s in post["snapshots"]],
                      x="read on", y="views")
    else:
        st.caption("No metrics recorded for this post.")


def analysis_tab(brief: Brief):
    run_id = pipeline.latest_run(db, brief.id)
    if not run_id:
        st.info("Rank first; there is nothing to analyse yet.")
        return
    ev = analysis.evaluation(db, analysis.latest_runs(db))

    def pct(rate):
        return "n/a" if rate is None else f"{rate:.0%}"

    st.subheader("Launch gate (all briefs)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Ranking rule accepted ({ev['rule']['accepted']} of {ev['rule']['suggested']})", pct(ev["rule"]["rate"]))
    c2.metric(f"Topic-only accepted ({ev['topic_only']['accepted']} of {ev['topic_only']['suggested']})",
              pct(ev["topic_only"]["rate"]))
    c3.metric("Reviewer agreement", pct(ev["agreement"]))
    c4.metric("Gate", "not decided" if ev["gate"] is None else "passed" if ev["gate"] else "not passed")
    st.caption("Passes when at least 80% of the rule's suggestions are accepted by two reviewers, every suggestion "
               f"links to evidence, and the rule beats ranking by topic alone. Verdicts still needed: {ev['missing']}.")

    st.subheader("Judge this brief's suggestions")
    st.caption("Verdicts belong to the brief and carry over when it is ranked again.")
    reviewer = st.text_input("Your name (each reviewer judges alone)", key="reviewer").strip().lower()
    s, seen = analysis.suggestions(db, run_id), analysis.verdicts(db, run_id)
    for account_id in sorted(set(s["rule"]) | set(s["topic_only"])):  # mixed, so you cannot tell which method chose it
        card, mine = s["cards"][account_id], seen.get(account_id, {}).get(reviewer)
        c1, c2, c3 = st.columns([5, 1, 1])
        c1.markdown(f"[{card['handle']}]({card['url']}) · {len(seen.get(account_id, {}))} verdicts"
                    + ("" if mine is None else f" · you said **{'accept' if mine else 'reject'}**"))
        if c2.button("Accept", key=f"acc_{account_id}", disabled=not reviewer):
            analysis.judge(db, run_id, account_id, reviewer, True)
            st.rerun()
        if c3.button("Reject", key=f"rej_{account_id}", disabled=not reviewer):
            analysis.judge(db, run_id, account_id, reviewer, False)
            st.rerun()

    left, right = st.columns(2)
    left.subheader("AI labels")
    left.dataframe([{"outcome": nice(k), "labels": n} for k, n in analysis.label_quality(db).items()], width="stretch")
    right.subheader("Where evidence is thin")
    right.caption("Creators in this ranking with the dimension unknown.")
    right.bar_chart([{"dimension": nice(d), "unknown": n} for d, n in analysis.coverage_gaps(db, run_id).items()],
                    x="dimension", y="unknown")
    cost = analysis.costs(db, ev["rule"]["accepted"])
    per = "n/a" if cost["per_accepted_peer"] is None else f"${cost['per_accepted_peer']:.2f}"
    st.subheader("Cost so far")
    st.write(f"Claude ${cost['claude_usd']:.2f} · Brave ${cost['brave_usd']:.2f} · YouTube quota units "
             f"{cost['youtube_units']} · per accepted creator {per}")
    st.subheader("Connectors")
    st.dataframe(analysis.doctor(db), width="stretch")


def prepublish_tab(brief: Brief):
    st.caption("Checks your own draft on this computer. Thresholds are starting points; tune them to your channel.")
    platform = st.selectbox("Platform", PLATFORMS, index=PLATFORMS.index(brief.platform), key="pp_platform")
    title = st.text_input("Title or caption", key="pp_title")
    vertical = st.checkbox("Vertical 9:16 cover", value=brief.format_family == "short" or platform != "youtube")
    image = st.file_uploader("Thumbnail or cover frame", type=["png", "jpg", "jpeg", "webp"])
    audio = st.file_uploader("Audio or video, for loudness (needs ffmpeg)", type=["wav", "mp3", "m4a", "mp4", "mov"])
    if not st.button("Check draft"):
        return
    # ffmpeg reads a file, so the upload goes into a folder of its own that is removed afterwards;
    # a crafted media file can then reach nothing else on disk.
    with tempfile.TemporaryDirectory() as folder:
        temp = None
        if audio is not None:
            suffix = Path(audio.name).suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", Path(audio.name).suffix) else ""
            temp = Path(folder) / f"draft{suffix}"
            temp.write_bytes(audio.getbuffer())
        findings = prepublish.check(platform, title, image, temp, vertical=vertical)
    st.dataframe(findings, width="stretch")
    if image is not None and vertical and not any(f["check"] == "thumbnail" for f in findings):
        st.markdown("**Areas the app draws over** (keep faces and text out of the red)")
        st.image(prepublish.safe_zone_overlay(prepublish.open_image(image)), width=270)


briefs = {b.id: b for b in store.list_briefs(db)}
options = [None] + list(briefs)
current = st.session_state.get("brief_id", options[-1])
brief_id = st.sidebar.selectbox("Brief", options, index=options.index(current) if current in options else 0,
                                format_func=lambda i: "New brief" if i is None else briefs[i].name)
st.session_state["brief_id"] = brief_id
brief = briefs.get(brief_id)
if brief is not None:
    with st.sidebar:
        st.caption(f"{PLATFORM_NAME[brief.platform]} · {brief.format_family} form · {brief.language}")
        if brief.objective:
            st.caption(f"Goal: {pipeline.md(brief.objective)}")
        st.radio("Go to", list(SCREENS), key="nav", captions=list(SCREENS.values()))
        st.divider()
        upcoming = analysis.next_step(analysis.progress(db, brief.id))
        st.markdown("**Next step**")
        st.write(f"{upcoming['step']} ({upcoming['where']})" if upcoming else "The four-week plan is complete.")
        st.caption(f"AI model: {ai_name()}")
        st.caption("Night mode: the ⋮ menu at the top right, then Dark.")
        with st.expander("How the screens fit together"):
            st.markdown(
                "1. **Brief**: who you are and what you can produce.\n"
                "2. **Candidates** and **Review**: add creators, note what their posts show.\n"
                "3. **Shortlist**: who fits, with the evidence.\n"
                "4. **Analysis**: reviewers accept or reject; the launch gate.\n"
                "5. **Script builder**: write your own video against what accepted creators do.\n"
                "6. **Pre-publish**: check the title, cover and sound.\n"
                "7. **Experiments**: test one idea and decide.\n\n"
                "Alongside: **Video tracker** scrapes and compares videos; **Assistant** answers questions from "
                "all of the above and trains your own model.")

if brief is None:
    st.title("New brief")
    brief_form(None)
else:
    screens = {
        "Dashboard": dashboard_tab, "Candidates": candidates_tab, "Review": review_tab, "Shortlist": shortlist_tab,
        "Creator viewer": viewer_tab, "Analysis": analysis_tab, "Video tracker": tracker_tab,
        "Assistant": assistant_tab, "Breakouts": lambda _brief: breakouts_tab(), "Script builder": script_tab, "Pre-publish": prepublish_tab,
        "Experiments": experiments_tab, "Brief": brief_form, "Data and access": data_tab}
    assert list(screens) == list(SCREENS), "the sidebar menu and the screens have drifted apart"
    chosen = st.session_state.get("nav", "Dashboard")
    st.title(chosen)
    screens[chosen](brief)
