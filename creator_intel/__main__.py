"""Command line: python -m creator_intel <command>. The reviewer screens live in app.py."""
import argparse
import json
import sys
from pathlib import Path

import anthropic

from . import adapters, analysis, assistant, broker, demo, labeling, learner, pipeline, prepublish, scripts, tracker
from . import db as store


def _percent(rate) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def _print_evaluation(ev: dict) -> None:
    for name, label in (("rule", "Ranking rule"), ("topic_only", "Topic only")):
        r = ev[name]
        print(f"{label}: {r['accepted']} of {r['suggested']} suggestions accepted ({_percent(r['rate'])})")
    print(f"Reviewer agreement: {_percent(ev['agreement'])}")
    print(f"Every suggestion links to evidence: {'yes' if ev['traceable'] else 'no'}")
    if not ev["rule"]["suggested"]:
        print("Launch gate: nothing to judge yet. Rank a brief first.")
    elif ev["gate"] is None:
        print(f"Launch gate: not decided. {ev['missing']} suggestions still need two reviewers' verdicts.")
    else:
        print("Launch gate: " + ("PASSED" if ev["gate"] else "NOT PASSED (needs 80% accepted and better than topic only)"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="creator_intel", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="load a fictional brief with reviewed candidates")
    sub.add_parser("briefs", help="list briefs")
    run = sub.add_parser("run", help="discover -> refresh -> label -> rank")
    run.add_argument("brief", type=int)
    run.add_argument("--resume", type=int, metavar="RUN_ID", help="continue a paused run")
    wk = sub.add_parser("weekly", help="the scheduled job: run, write the digest, send the alert if a webhook is set")
    wk.add_argument("brief", type=int)
    wk.add_argument("-o", "--out", type=Path, help="where to write the digest")
    dg = sub.add_parser("digest", help="weekly Markdown digest")
    dg.add_argument("brief", type=int)
    dg.add_argument("-o", "--out", type=Path)
    links = sub.add_parser("import-links", help="add creator links from a text file, one per line")
    links.add_argument("brief", type=int)
    links.add_argument("file", type=Path)
    csv_ = sub.add_parser("import-metrics", help="import an owner metrics CSV")
    csv_.add_argument("file", type=Path)
    ex = sub.add_parser("export-labels", help="every label on a brief's candidates, as a spreadsheet for calibration")
    ex.add_argument("brief", type=int)
    ex.add_argument("file", type=Path)
    sub.add_parser("evaluate", help="launch gate: accepted suggestions, rule versus topic-only, reviewer agreement")
    doc = sub.add_parser("doctor", help="state of every connector")
    doc.add_argument("--live", action="store_true", help="make one small real request per configured service")
    pp = sub.add_parser("prepublish", help="check a draft's title, thumbnail and loudness before posting")
    pp.add_argument("--platform", required=True, choices=sorted(prepublish.TITLE_LIMITS))
    pp.add_argument("--title", required=True)
    pp.add_argument("--image", type=Path, help="thumbnail or cover frame")
    pp.add_argument("--audio", type=Path, help="audio or video file (needs ffmpeg)")
    pp.add_argument("--vertical", action="store_true", help="a 9:16 cover (YouTube Shorts)")
    pp.add_argument("--overlay", type=Path, help="write the cover with the app-covered areas marked")
    sc = sub.add_parser("script-check", help="compare a script draft with what the judged creators do")
    sc.add_argument("brief", type=int)
    sc.add_argument("file", type=Path, help="a text file with the words you will say")
    sc.add_argument("--opening", help="the label you give your own opening, e.g. result_first")
    sc.add_argument("--format", dest="format_subtype", help="the label you give your own format")
    sc.add_argument("--delivery")
    sc.add_argument("--ai", action="store_true", help="also ask Claude for a comparison (sends the draft)")
    scr = sub.add_parser("scrape", help="fetch videos through the data broker into the brief's catalog (costs credits)")
    scr.add_argument("brief", type=int)
    scr.add_argument("--platform", required=True, choices=sorted(broker.ACTORS) + ["all"],
                     help="all = the same --search on every platform left on for this brief (one charge each)")
    scr.add_argument("--search", help="theme words or a hashtag")
    scr.add_argument("--channel", help="a channel link")
    scr.add_argument("--own", action="store_true", help="the channel is yours")
    scr.add_argument("--limit", type=int, default=20)
    trk = sub.add_parser("tracker", help="catalogued videos by explosivity, and what to improve")
    trk.add_argument("brief", type=int)
    ask = sub.add_parser("ask", help="answer a question from the brief's own data")
    ask.add_argument("brief", type=int)
    ask.add_argument("question")
    ask.add_argument("--about", help="one section only, e.g. \"Video tracker\"")
    ask.add_argument("--ai", action="store_true", help="have the AI model answer from the same data")
    lrn = sub.add_parser("learn", help="train your own model on the brief's tracked videos")
    lrn.add_argument("brief", type=int)
    ext = sub.add_parser("export-training", help="the tracked videos as a JSONL file for fine-tuning a local AI model")
    ext.add_argument("brief", type=int)
    ext.add_argument("file", type=Path)
    rec = sub.add_parser("recommend", help="what to do next for a script, a short, a longer video or a stream")
    rec.add_argument("brief", type=int)
    rec.add_argument("--for", dest="kind", default="short", choices=list(assistant.KINDS))
    rec.add_argument("--ai", action="store_true", help="have the AI model summarise and add its own recommendations")
    sub.add_parser("trust-ai", help="confirm the server in CI_AI_URL as where this workspace's AI requests may go")
    rv = sub.add_parser("revoke", help="remove everything a source supplied, and matches built on it")
    rv.add_argument("source")
    sub.add_parser("purge", help="remove expired evidence")
    a = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a codepage that mangles the digest
    except (AttributeError, ValueError):
        pass

    if a.cmd == "prepublish":  # needs no database
        findings = prepublish.check(a.platform, a.title, a.image, a.audio, vertical=a.vertical or None)
        for f in findings:
            print(f"{f['level']:12} {f['check']}: {f['message']}")
        if a.overlay:
            try:
                if not a.image:
                    raise ValueError("--overlay needs --image")
                prepublish.safe_zone_overlay(prepublish.open_image(a.image)).save(a.overlay)
                print(f"overlay written to {a.overlay}")
            except (ValueError, OSError) as exc:
                print(f"overlay not written: {exc}")
        return 1 if any(f["level"] == "fail" for f in findings) else 0

    db = store.open_db()
    if a.cmd == "demo":
        print(f"Demo brief {demo.seed(db)} ready. Start the app: streamlit run app.py")
    elif a.cmd == "briefs":
        for b in store.list_briefs(db):
            print(b.id, b.platform, b.name)
    elif a.cmd in ("run", "weekly"):
        result = pipeline.run_all(db, a.brief, a.resume) if a.cmd == "run" else pipeline.weekly(db, a.brief, a.out)
        for stage, note in result["log"].items():
            print(f"{stage}: {note}")
        if "alert" in result:
            print(f"alert: {result['alert']}")
        print(f"run {result['run_id']} {result['status']}")
        return 0 if result["status"] == "finished" else 2
    elif a.cmd == "digest":
        text = pipeline.digest(db, a.brief)
        a.out.write_text(text, encoding="utf-8") if a.out else sys.stdout.write(text)
    elif a.cmd == "import-links":
        print(pipeline.import_links(db, a.brief, a.file.read_text(encoding="utf-8").split()))
    elif a.cmd == "import-metrics":
        print(adapters.import_metrics_csv(db, a.file))
    elif a.cmd == "export-labels":
        print(f"{analysis.export_labels(db, a.brief, a.file)} labels written to {a.file}")
    elif a.cmd == "evaluate":
        ev = analysis.evaluation(db, analysis.latest_runs(db))
        _print_evaluation(ev)
        quality = analysis.label_quality(db)
        print("AI labels: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in quality.items()))
        cost = analysis.costs(db, ev["rule"]["accepted"])
        per = "n/a" if cost["per_accepted_peer"] is None else f"${cost['per_accepted_peer']:.2f}"
        print(f"Cost so far: ${cost['total_usd']:.2f} ({cost['youtube_units']} YouTube quota units); "
              f"per accepted creator: {per}")
    elif a.cmd == "doctor":
        for row in analysis.doctor(db, live=a.live):
            print(f"{row['name']}: {row['status']}")
    elif a.cmd == "script-check":
        text = a.file.read_text(encoding="utf-8-sig")
        declared = {k: v for k in ("opening", "format_subtype", "delivery") if (v := getattr(a, k))}
        patterns = scripts.peer_patterns(db, a.brief)
        report = scripts.analyze(text, declared, patterns, store.get_brief(db, a.brief).format_family)
        print(f"Compared with {len(patterns['accepted'])} creators: {patterns['basis']}.")
        print(f"{report['stats']['words']} words, about {report['stats']['seconds']} s spoken.")
        for f in report["findings"]:
            print(f"{f['level']:12} {f['check']}: {f['message']}")
        if a.ai:
            try:
                got = scripts.ai_feedback(db, a.brief, text, declared)
            except (labeling.NoCredential, adapters.SourceError) as exc:
                print(f"AI comparison not run: {exc}")
            except anthropic.APIError as exc:
                print(f"AI comparison not run: the service refused or failed ({type(exc).__name__})")
            else:
                print("\n" + got["summary"])
                for g in got["gaps"]:
                    print(f"- {g['dimension']}: {g['suggestion']}")
        return 1 if any(f["level"] == "fail" for f in report["findings"]) else 0
    elif a.cmd == "scrape":
        try:
            if a.platform == "all":
                if not a.search or a.channel or a.own:
                    raise ValueError("--platform all works with --search only; a channel link is on one platform")
                off = tracker.get_tuning(db, a.brief)["off_platforms"]
                results = broker.scrape_many(db, a.brief, [p for p in tracker.PLATFORMS if p not in off], a.search, a.limit)
            else:
                results = {a.platform: broker.scrape(db, a.brief, a.platform, search=a.search, channel=a.channel,
                                                     own=a.own, limit=a.limit)}
        except (ValueError, broker.NoToken, adapters.AccessDenied, adapters.SourceError) as exc:
            print(f"Not scraped: {exc}")
            return 2
        for platform, got in results.items():
            print(f"{platform}: " + (f"not scraped: {got['error']}" if "error" in got else
                                     f"{got['fetched']} items came back, {got['usable']} readable videos, "
                                     f"{got['added']} new to the catalog."))
        if all("error" in got for got in results.values()):
            return 2
    elif a.cmd == "tracker":
        catalog = tracker.rows(db, a.brief)
        for r in catalog:
            speed = r["explosivity"] if r["explosivity"] is not None else r["theme_ratio"]
            print(f"{r['tier'] or 'unknown':8} {'-' if speed is None else format(speed, '.1f') + 'x':>6}  "
                  f"{r['platform']:9} {r['handle']}  {(r['title'] or r['url'])[:70]}")
        compared = tracker.compare(catalog, tracker.get_tuning(db, a.brief))
        print(f"Compared on {compared['basis']}.")
        for found in compared["layers"]:
            print(f"{found['factor']}: yours {found['yours']}, outliers {found['outliers']} ({found['gap']})")
    elif a.cmd == "ask":
        try:
            try:
                got = assistant.answer(db, a.brief, a.question, about=a.about, use_ai=a.ai)
            except (labeling.NoCredential, adapters.SourceError, anthropic.APIError) as exc:
                print(f"The AI model did not answer ({exc if not isinstance(exc, anthropic.APIError) else type(exc).__name__}). "
                      "From the data alone:")
                got = assistant.answer(db, a.brief, a.question, about=a.about)
        except ValueError as exc:
            print(f"Not answered: {exc}")
            return 2
        if got["answer"]:
            print(got["answer"] + "\n\nBuilt on:")
        for name, lines in got["sections"].items():
            print(f"[{name}]")
            for line in lines:
                print(f"  {line}")
    elif a.cmd == "learn":
        report = learner.fit(db, a.brief)
        if not report["trained"]:
            print(f"Not trained: {report['why']}.")
        else:
            print(f"Trained on {report['examples']} videos ({report['fast']} fast). On videos it was not shown it was "
                  f"right {report['accuracy']:.0%} of the time; always guessing the commoner answer scores "
                  f"{report['baseline']:.0%}. " + ("It adds something." if report["useful"] else
                                                  "No better than guessing: treat its scores as noise."))
    elif a.cmd == "export-training":
        try:
            lines = learner.training_examples(db, a.brief)
        except ValueError as exc:
            print(f"Not written: {exc}")
            return 2
        a.file.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
        print(f"{len(lines)} examples written to {a.file} (it contains the scraped captions)")
    elif a.cmd == "recommend":
        try:
            got = assistant.recommend(db, a.brief, a.kind, use_ai=a.ai)
        except (labeling.NoCredential, adapters.SourceError, anthropic.APIError) as exc:
            print(f"The AI model did not answer ({type(exc).__name__ if isinstance(exc, anthropic.APIError) else exc}). "
                  "From the data alone:")
            got = assistant.recommend(db, a.brief, a.kind)
        print(f"For {assistant.KINDS[a.kind]}." + (f" {got['note']}" if got["note"] else ""))
        if got["summary"]:
            print(f"\n{got['summary']}\n\nThe AI model recommends:")
            for n, option in enumerate(got["ai_options"], start=1):
                print(f"{n}. {option['do']}\n   {option['why']}")
            print("\nFrom the data alone, to check it against:")
        for n, option in enumerate(got["options"], start=1):
            print(f"{n}. {option['do']}\n   {option['why']} [{option['source']}]")
        if not got["options"]:
            print("Nothing to recommend yet: track more videos and review more creators.")
        for line in got["unchecked"]:
            print(f"Not compared: {line}")
    elif a.cmd == "trust-ai":
        try:
            host = labeling.unconfirmed(db)
        except adapters.SourceError as exc:
            print(f"Not confirmed: {exc}")
            return 2
        labeling.trust(db)
        print(f"Confirmed: AI requests may go to {host}." if host else
              f"Nothing to confirm: AI requests go to {labeling.describe()}.")
    elif a.cmd == "revoke":
        print(f"removed {store.revoke_source(db, a.source)} evidence items from {a.source}")
    elif a.cmd == "purge":
        print(f"removed {store.purge_expired(db)} expired evidence items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
