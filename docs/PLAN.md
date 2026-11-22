# Creator research suite: integrated plan

Written 2026-11-19 from two blueprints: *Creator Comparison* (7 pages, evidence-first) and
*Cross-Platform Creator Intelligence & Growth Engine* (8 pages, automation-first). This file is the
plan to work from: what the suite does, what is already built, how the two blueprints were
reconciled, and what to do next in order.

## What the suite answers

Every week, for one channel: **who makes content for the same viewer in a format I can produce, and
what should I try next?** The output is up to five comparable creators per platform, each with a fit
score, the evidence behind it, the mismatch, and one experiment to run.

## How the two blueprints fit together

The Comparison blueprint is the spine: every label has evidence, every source declares what it
permits, and ranking is a rule you can read. The Growth Engine blueprint supplies the automation
around it: breakout math, append-only metric history, adapters, a weekly digest.

Where they disagree, this is what was chosen and why.

| Topic | Growth Engine says | Comparison says | Chosen |
|---|---|---|---|
| TikTok / Instagram peer data | Scrape through brokers (Apify, EnsembleData) | Use only permitted sources; ship the manual path | Manual review plus owner exports. No scraper is built. A broker is a decision for you (see Decisions). |
| How creators are matched | Embeddings of transcript, thumbnail, audio (40/30/15/15) | Explicit labels and weights (30/25/20/15/10) | The explicit rule. Embeddings may later help *find* candidates, never rank them. |
| Scores from YouTube API data | Outlier scoring is phase 1 | Confirm the derived-metrics policy first | Built, but switched off for YouTube API data until you confirm. Reviewer notes and your own exports are scored now. |
| Same creator across platforms | Merge automatically from link-in-bio pages | Keep separate unless the creator confirms | Separate. |
| Database | PostgreSQL + pgvector | PostgreSQL | SQLite, one file, no server. Move when two people write at once or embeddings arrive. |
| Services | FastAPI, Celery/Prefect, Redis | Streamlit plus a scheduled worker | Streamlit app plus a command you can schedule. |
| AI model | Gemini / OpenAI | Any model with structured output | Claude (`claude-opus-5-5`), structured output, no tools. |
| Audience-size filter (0.5x to 10x) | 15% of the score | Not in the score | Not in the score. Follower counts are stored and shown; add a filter if mega-channels crowd the shortlist. |

## What is built (verified 2026-11-19)

`python scripts/verify.py` passes: lint, secret scan, 215 tests including a render of every app screen
and a click-through of judging and the draft check. The app was also started for real and answered its
health check, and the pre-publish check was run on a real image and audio file.

| Part | File | What it does |
|---|---|---|
| Link intake | `creator_intel/urls.py` | Accepts YouTube, TikTok, Instagram creator and post links; one canonical form; rejects everything else. |
| Storage | `creator_intel/db.py` | Briefs, creators, posts, evidence with rights and expiry, tags, append-only metric snapshots, matches, experiments, usage counters. Revoke and purge remove a source's data and every match built on it. |
| Ranking | `creator_intel/ranking.py` | The scoring rule, coverage, the three-post and 80% gates, top five, "stretch", the plain-language explanation, the experiment idea. Reproduces the blueprint's 88.2 and 20.0 examples. |
| Metrics | `creator_intel/metrics.py` | Breakout rules per platform, saves per 1,000 reached, the four-pair trial decision, "hot on TikTok, absent elsewhere". Unknown inputs give "unknown", never zero. |
| Outside world | `creator_intel/adapters.py` | Brave search, YouTube feed, video stats and subscriber counts, owner metrics CSV. Retries with backoff; keys never appear in errors. |
| AI labeling | `creator_intel/labeling.py` | Claude proposes labels with a quote; code accepts only allowed values whose quote is really in the evidence; the rest goes to the review queue. Only material with the "ai" right is sent. |
| Run | `creator_intel/pipeline.py` | discover, refresh, label, rank. Skips a stage that has no key, pauses on denied access or a failing source, resumes where it stopped. Weekly Markdown digest. |
| Analyzer | `creator_intel/analysis.py` | The launch gate (reviewer verdicts, rule versus topic-only, agreement), what happened to AI labels, where evidence is thin, cost per accepted creator, connector check, label export for calibration. |
| Pre-publish | `creator_intel/prepublish.py` | Checks your own draft: title length, thumbnail contrast at 120px, cover aspect, the areas the app draws over, loudness (through ffmpeg). |
| Script builder | `creator_intel/scripts.py` | For your own drafts: what the creators reviewers accepted have in common (rejected ones as contrast), a rule-based comparison of your draft (length, opening, format, delivery, rejected patterns, copied wording), an outline to write into, and optional Claude feedback or a first draft. Only labels and reviewers' own notes are sent to the model. |
| Screens | `app.py` | Dashboard (plan progress and the next step), Shortlist, Script builder, Review, Candidates, Creator viewer, Breakouts, Experiments, Analysis, Pre-publish, Brief, Data and access. Day and night looks: the menu at the top right. |
| Program | `launcher.py`, `scripts/build_exe.py` | `CreatorResearch.exe`: opens the dashboard, or runs a command when given one. The build script smoke-tests the exe it produces and keeps your data and keys if you rebuild over a folder you have been using. |
| Commands | `python -m creator_intel ...` | `demo`, `run`, `weekly`, `digest`, `evaluate`, `doctor`, `prepublish`, `export-labels`, `import-links`, `import-metrics`, `revoke`, `purge`, `briefs`. |

Added in the second round (2026-11-19): the analyzer, the creator viewer, the pre-publish checker,
Discord or Slack alerts, breakouts compared at day 7, audience-size context on each card, and the
`weekly` job.

Not built, on purpose: official TikTok and Instagram connectors, embeddings, transcription,
style analysis of peers' thumbnails and audio, PostgreSQL. Each is listed under "Later" with the
condition that should trigger it. The weekly job exists but is not scheduled; scheduling changes your
computer, so that step is yours (week 2).

(A data-broker scraper was left out at first and added later at the owner's request: see the third round below.)

Not yet exercised against the live services: the Brave, YouTube, Claude and Apify calls, and any other AI server. Their parsing,
retry and validation logic is tested with recorded shapes; the first real run with keys is week 2.

## Start here

The program is packaged as a Windows exe. Build it once (a few minutes, about 260 MB):

```
.venv\Scripts\python scripts\build_exe.py
```

Then copy the folder `dist\CreatorResearch` to wherever you want to keep the program, and double-click
`CreatorResearch.exe`. A console window opens and the dashboard appears in your browser; closing the
console stops it. Your work is saved in a `data` folder beside the exe, and keys go in a `.env` file
there (copy `.env.example`). Commands work through the exe too: `CreatorResearch.exe demo`,
`CreatorResearch.exe weekly 1 -o digest.md`.

The first screen is the **Dashboard**: it works out from your data which of the steps below are done
and tells you the next one and which screen it is on.

Without building, from the repo:

```
.venv\Scripts\activate
python -m creator_intel demo      # fictional brief so every screen has something in it
python launcher.py                # same as the exe; or: streamlit run app.py
```

Keys are optional and read from the environment (see `.env.example`). Without them the manual flow
works in full: brief, paste links, review posts, rank, experiment.

| Key | Enables | Notes |
|---|---|---|
| `BRAVE_API_KEY` | Candidate discovery by search | Only links are kept. |
| `YOUTUBE_API_KEY` | Sampling recent uploads, view counts, subscribers | About 3 quota units per channel per refresh. Restrict the key to YouTube Data API v3. |
| `ANTHROPIC_API_KEY` or `ant auth login` | AI labeling | Capped at 300 evidence items per run. If a safety classifier declines a request, it is retried on `claude-opus-4-8`. |
| `CI_YOUTUBE_RIGHTS` | What YouTube API data may be used for | Default `store`. Set `store,ai,derive` only after the decision below. |
| `CI_WEBHOOK_URL` | A short alert after each weekly job | Discord or Slack incoming webhook, https only. Treat it as a secret. |
| `CI_BRAVE_USD_PER_REQUEST` | Brave spend in the cost report | Your plan's price per request; default 0. Counts only with `BRAVE_API_KEY` in the same file. |
| `APIFY_TOKEN` | Scraping videos through the data broker | Each scrape costs Apify credits. |
| `CI_BROKER_RIGHTS` | What scraped data may be used for | Default `store,ai,derive`. `store` keeps it away from AI and scoring. Always counts. |
| `CI_AI_URL`, `CI_AI_MODEL`, `CI_AI_KEY` | Another AI model in place of Claude | Any OpenAI-compatible server. A server that is not this computer must be confirmed once (`trust-ai`). |

## Decisions that are yours

1. **YouTube derived metrics.** Until you confirm that the YouTube API policy lets you compute your
   own scores and labels from API data, titles are not sent to the model and API view counts are
   not scored. Confirming it is one environment variable.
2. **Brave plan.** Storage rights depend on the plan. The suite stores result links only.
3. **A paid social data broker for TikTok / Instagram.** Decided 2026-11-19: yes, through Apify (third round). The
   Comparison blueprint's warning still stands: whether your use is allowed depends on the broker's and each
   platform's terms. `revoke broker` removes everything it supplied; `CI_BROKER_RIGHTS=store` narrows its use.
4. **Weights.** 30/25/20/15/10 are editorial priorities. Change them only after the week 3 test.

## Next steps, in order

**Week 1: one real brief.**
- Write the brief for one channel and one format on YouTube. Agree the topic and audience labels.
- Paste ten creator links. Review three posts for each viable one. Rank.
- Access test: for each platform, process one of your own records and one peer link, and note which
  fields actually came back. Try the same with a missing or wrong key and confirm the stage pauses.
- Done when: a shortlist exists where every claim opens a post you can check.

**Week 2: discovery.**
- Add the Brave and YouTube keys. Run the pipeline. Replay it and confirm nothing duplicates.
- `python -m creator_intel doctor --live` makes one small request per service and tells you which
  keys work. This is the access test with real keys.
- Check the Usage table against each plan's quota. Schedule the weekly job in Windows Task Scheduler:
  `python -m creator_intel weekly <brief> -o digest.md`. Set `CI_WEBHOOK_URL` to a Discord or Slack
  webhook if you want a short alert each week.
- Done when: a weekly run adds candidates and a digest without you touching it.

**Week 3: prove the ranking and the AI labels.**
- Ten held-out briefs, five suggestions each, two reviewers judging independently.
- Launch gate: at least 80% of suggestions accepted, every displayed claim traceable to evidence, and
  better acceptance than ranking by topic alone.
- Each reviewer opens the Analysis screen, types their name, and accepts or rejects each suggestion.
  The list mixes the rule's picks with topic-only picks so the reviewer cannot tell which is which.
- `python -m creator_intel evaluate` (or the Analysis screen) reports the gate across all briefs, how
  often AI labels were confirmed, corrected or rejected, and the cost per accepted creator.
- `export-labels <brief> labels.csv` gives a spreadsheet of every label for calibrating reviewers.
- Done when: the gate passes, or the weights and label guide have been changed and it passes.

**Week 4: growth pilot.**
- Add your own channel and posts (Data and access), import your metrics export.
- Run one trial per channel: four matched pairs, random order, one metric chosen up front, seven days.
- TikTok and Instagram: manual review and owner exports. Apply for approved connectors in parallel.
- Log cost per accepted peer and minutes saved against manual research.
- Done when: one experiment has a decision and you know what a week of this costs.

## Later, and what should trigger it

| Add | When |
|---|---|
| Embedding search for candidates | A synonym test shows keyword search misses good peers. |
| PostgreSQL | A second reviewer needs to write at the same time. |
| TikTok Display API / Instagram Graph connectors | Your app review is approved. The manual path stays as the fallback. |
| Transcripts, thumbnail and audio signals | Reviewers keep leaving delivery and setup unknown and coverage falls under 80%. |
| Own-channel connectors (YouTube Analytics, Instagram Graph, TikTok Display) | You have registered an app with each platform and it is approved. Until then, metric exports. |
| Audience size as a filter, not just a note | Channels far outside 0.5x to 10x of your size keep reaching the shortlist. |
| Minutes saved versus manual research | You have timed one manual research session to compare against. |

## Known limits

- The quote check proves a label's quote is in the text, not that the label is right. Text written to
  steer the model could still get an allowed label accepted once third-party titles are sent to it.
  Reviewer correction on the shortlist is the backstop.
- Revoking a source clears its evidence, tags, snapshots, matches and the post title, length and date
  it filled. A creator's channel id and subscriber count remain.
- The app has no login unless you set a password (eighth round). It is bound to this computer; do not expose the port.
- The exe is a folder, not one file, and is not code-signed: Windows SmartScreen may warn the first time
  it runs on another computer. Once used, the folder holds your keys and data; share only a fresh build.
- The dashboard's "own channel and metrics" step looks at your channel on the brief's platform, so it is
  shared by all briefs on that platform. Its port is chosen just before the server starts; if another
  program takes it in that instant, start the program again.
- Script builder: you label your own draft (opening, format, delivery); nothing is guessed from the text.
  The copy check catches eight consecutive shared words, not a close paraphrase. Spoken length assumes
  150 words a minute. Labels the AI auto-accepted (not yet confirmed by a reviewer) count toward the
  patterns. The AI feedback and AI draft have only been tested with a stand-in model, never the live one.
- The launch gate trusts reviewer names: one person typing two names would satisfy "two reviewers".
- The cost report prices Claude tokens at `claude-opus-5-5` rates even if `CI_MODEL` is changed or the
  fallback model answers. Minutes saved versus manual research is not measured.
- Pre-publish thresholds (contrast, the covered screen areas, the loudness target) are starting points,
  not platform rules. The creator viewer's "in common" labels use all reviewed posts, where ranking uses
  the five most recent in the brief's format, so the two can differ.
- Breakouts at day 7 only cover posts that have a reading taken within a week of publishing.
- One reviewer, one workspace, one label vocabulary per brief.
- Owner metrics CSV: one normalised layout (`post_url` plus metric columns). A cell that is not a plain
  whole number is stored as unknown.

## Added 2026-11-19, third round: data broker, video tracker, sidebar

- **Data broker** (`creator_intel/broker.py`): scrapes TikTok, Instagram and YouTube through Apify for a
  theme search, another creator's channel, or your own channel. Needs `APIFY_TOKEN` in `.env`. Each
  scrape costs Apify credits and is capped at 100 items requested. Run it from the Video tracker screen
  or `python -m creator_intel scrape <brief> --platform tiktok --search "visible mending"`.
- **Video tracker** (`creator_intel/tracker.py`, the Video tracker screen, `tracker <brief>`): your videos
  and scraped ones in one catalog, with views per day, explosivity (speed against that creator's own
  usual; 3x is an outlier, 1.5x is strong), engagement, shares per view, and a "what to improve" table
  once there are three of your videos and three outliers. Only fast videos are queued for AI labeling.
- **Sidebar**: every screen is in the sidebar menu with a one-line description, plus the next step and
  the night-mode hint. Text typed on one screen is kept while you visit another.

This changes an earlier decision in this plan: the first blueprint advised against scraping TikTok and
Instagram, and the first build left it out. It is in now because you asked for it. Scraping through a
broker is outside the platforms' official APIs; whether your use is permitted is governed by Apify's
and each platform's terms. To undo it: `python -m creator_intel revoke broker` removes every reading,
catalog entry and raw file it supplied, and `CI_BROKER_RIGHTS=store` stops scraped captions reaching
the AI and scraped numbers being scored.

Limits of this round: no scrape has been run against Apify (no token here), so the field mapping is
checked against Apify's documentation and sample items only. YouTube items often carry a relative
date ("10 months ago"), which is stored as unknown, so those videos get no speed figure. A YouTube
creator scraped by channel id and added by hand as @handle is held as two creators. Engagement is not
comparable across platforms (they report different counts). The catalog fallback compares against
other creators in your catalog, so it is only as good as what you have scraped.

## Added 2026-11-19, fourth round: all platforms, tailoring, any AI model, questions, your own model

- **One theme on every platform** (`broker.scrape_many`, the Video tracker screen, `scrape <brief> --platform all
  --search "..."`): one scrape, and one charge, per platform. A platform that fails is reported and the others
  are kept. A creator with no history is compared with creators on the same platform only.
- **Tailoring, per brief** (`tracker.get_tuning` / `set_tuning`, the "Tailor this" panel): switch a platform off,
  put one first, switch a layer off. The platform put first is used alone only when it has three of your videos
  and three outliers; otherwise every platform left on is used and the screen says why.
- **Layers with a scope check** (`tracker.LAYERS`, `tracker.compare`): caption length, length, views per day,
  views per follower, engagement, share rate, save rate. Each says "checked", "switched off", or "not checked"
  with what is missing; a gap is stated only on three values a side.
- **Any AI model** (`labeling.ask`): Claude by default; `CI_AI_URL` + `CI_AI_MODEL` (+ `CI_AI_KEY`) send every AI
  request to an OpenAI-compatible server instead, such as Ollama on this computer. Plain http only for this
  computer. A server elsewhere receives nothing until it is confirmed once per workspace (Assistant screen or
  `trust-ai`). The answer must fit the same shape Claude's must, or it is an error.
- **Assistant** (`creator_intel/assistant.py`, the Assistant screen, `ask <brief> "question" [--ai]`): the brief
  laid out as facts per tool (plan, shortlist, launch gate, video tracker, script patterns, own model). A
  question is answered from the matching facts with no AI, or by the AI model from the same facts, naming the
  sections it used. The facts hold handles, labels and numbers; no caption, title or note.
- **Your own model** (`creator_intel/learner.py`, `learn <brief>`): naive Bayes over caption words, caption
  length, video length and platform, trained on this computer from the catalog. It is tested on creators it was
  not shown and says when it does no better than guessing. `export-training <brief> file.jsonl` writes the
  catalog as chat-format examples for fine-tuning a local model.

Known limits of this round:
- Nothing was run against a real Apify actor, a real local model server, or Claude.
- The own model is a starting point: it finds traits fast videos share, which is not what causes speed, and its
  percentage is a ranking, not odds. Tiers mix "against their own usual" and "against the platform".
- (Closed in the sixth round.) A YouTube title first stored by the YouTube API was treated as a broker caption if the
  same video is later scraped with an empty title. Narrow, not yet closed.
- Tokens used by a server other than Claude are counted (`other_ai_*`) but not priced in the cost report.
- (Closed in the fifth round: `.env` names are now an explicit list.)

## Added 2026-11-19, fifth round: explicit .env names, recommendations per kind of content

- **`.env` names** (`launcher.ENV_NAMES`): every name the file may set is listed; anything else is ignored and
  reported at startup (the name only, and only when it looks like a setting's name). A setting that belongs to
  another counts only when that one is in the same file: `CI_YOUTUBE_RIGHTS` with `YOUTUBE_API_KEY`,
  `CI_BRAVE_USD_PER_REQUEST` with `BRAVE_API_KEY`, `CI_APIFY_*` with `APIFY_TOKEN`, `CI_AI_URL` and `CI_AI_MODEL`
  with each other, `CI_AI_KEY` with `CI_AI_URL`. `CI_BROKER_RIGHTS` always counts, because it can only narrow.
- **What the "ai" right covers** (owner's decision): written material, meaning captions, titles and notes.
  Handles and numbers worked out under "derive" go to the AI model regardless.
- **Recommendations** (`assistant.recommend`, "What to do next" on the Assistant screen, `recommend <brief>
  --for script|short|video|stream [--ai]`): from the data alone, options come from checked layers where the
  outliers differ from you (shorts and longer videos compared separately), judged creators' patterns, your own
  model's traits and the experiment idea. With AI, the model also reads permitted notes and captions,
  summarises them and adds its own recommendations; text repeating a caption's or note's wording is dropped.
  Streams: nothing is measured on streams, and the screen says so.

Known limits of this round:
- A `.env` that brings both a key and the setting that depends on it is taken at its word; `CI_DB`,
  `CI_WEBHOOK_URL`, `CI_MODEL` and the service keys stand alone. The file is treated as the owner's own.
- The own model's traits are filtered by the tailoring but come from a model trained on the whole catalog.

## Added 2026-11-19, sixth round: limits closed, tools on the Dashboard

- **Captions keep their source.** The tracker, the AI reading, your own model, the training file and the
  recommendations use only the caption the broker itself sent (`catalog.caption`), never `post.title`, which the
  YouTube API may have filled. An existing workspace is brought forward when opened: TikTok and Instagram
  captions carry over; YouTube ones return at the next scrape.
- **Own-model traits** must appear in fast videos of at least three creators before they are recommended.
- **Dashboard**: a "Growth tools" table beside the plan (your videos tracked, outliers found, layers compared,
  model trained), worked out per brief from its data. Not part of the four-week plan: scraping costs credits.

What is left in the plan is the owner's: week 1 with a real brief, week 2 with keys and the scheduled job,
week 3 with two reviewers, week 4 with your own channel. The "Later" table's triggers have not occurred.
- Found by this round's reviews and fixed: revoking the broker now clears the whole catalog, including videos
  that came with a caption but no numbers; narrowing `CI_BROKER_RIGHTS` stops captions already queued for
  labeling, and nothing new is queued while it is narrowed or the caption layer is off.
- Still open: caption length for YouTube videos is unknown until they are scraped again (the stored API title
  may not be used for scores by default); revoking one source can remove a title another source supplied.

## Added 2026-11-19, seventh round: keys in the tool, rehearsal, weekly job

- **Keys and AI model** (Data and access screen; `launcher.save_env`): the four service keys, the other-AI-server
  settings and the alert webhook can be typed into the tool. They are saved to the `.env` file beside the
  program, work at once, and are never shown again. The same rules as the file apply. The rights settings and
  the scraper choices stay in the file and are read at start.
- **Rehearsal** (2026-11-19): the demo brief was run through the packaged program in `dist\CreatorResearch`
  (`demo`, `run 1`, `weekly 1`, `doctor`, `evaluate`, `recommend`, `ask`). With no keys the search, YouTube and
  labeling stages are skipped and the run still ranks and writes `digest.md`. Reviewer verdicts were not
  entered: invented verdicts would count towards the real launch gate, which spans all briefs.
- **Weekly job**: registered in Windows Task Scheduler as "CreatorResearch weekly" (Mondays 09:00, brief 1,
  writing `digest.md` beside the exe). It points at `dist\CreatorResearch`: if you move the folder, or want
  another brief, change or re-register the task. Remove it with
  `Unregister-ScheduledTask -TaskName "CreatorResearch weekly"`.
- (Closed in the eighth round.) The dashboard has no login, so anyone using this computer while it runs can change the settings.

## Added 2026-11-20, eighth round: a password on the dashboard

- **Password** (`CI_PASSWORD`; Data and access screen, or the `.env` file; `launcher.unlock`): when set, the
  dashboard shows nothing but a password box until it is typed. Each wrong try doubles the wait before the next
  one is looked at (2 seconds up to 5 minutes), for every browser at once. Changing the password asks every open
  browser again at its next click (yours straight after saving); a page already on screen stays visible until then. Forgotten: delete the `CI_PASSWORD` line from `.env`
  and start the program again.
- What it does not do: the password sits in `.env` as plain text, like the keys, so it keeps out other people
  using this computer, not someone who can read that file. The commands (`CreatorResearch.exe weekly ...`) do
  not ask for it. Restarting the program clears the wait. There is still one password, not accounts per person.
  Someone else on this computer typing wrong passwords can keep you waiting (restart the program to clear it),
  and while no password is set, anyone who can open the dashboard can set one. Any length is accepted, and
  changing it does not ask for the old one.
- Left as it is: revoking one source can remove a title another source supplied. Remembering which source
  filled each field would close it; over-clearing is the safe side, and the next import fills the title again.
