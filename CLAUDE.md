# AI YoutubeTiktokInstagram Analyzer

<!-- knowledge-stack -->
## Knowledge stack (auto-seeded once per project; edit freely)
- **Code graph**: `graphify-out/graph.json` + `graphify-out/GRAPH_REPORT.md`, refreshed automatically each session.
  Start codebase questions with `graphify query "<q>"`, `graphify explain "<node>"`, `graphify path "A" "B"` before grepping.
- **Obsidian mirror**: `C:\Users\jorak\Documents\Obsidian Vault\graphs\AI YoutubeTiktokInstagram Analyzer` (one note per node + `graph.canvas`). Capture decisions and notes there with `ecc:knowledge-ops`.
- **ECC workflow**: `/ecc:plan` -> `/ecc:tdd-guide` -> `/ecc:code-review`; `ecc:codebase-onboarding` reads GRAPH_REPORT.md first.

## Project contract
- **Verify**: `python scripts/verify.py` (ruff, secret scan, pytest incl. the Streamlit render test). Done means it
  passed on the current tree; a skipped step is not a pass.
- **Definition of done**: acceptance criteria written first, a failing test before the fix, verify output read,
  fresh-context review (`ecc:code-reviewer`, plus `ecc:security-reviewer` for input, network, secrets), evidence report.
- **Lessons**: `docs/engineering-lessons/NNNN-title.md`, status `proposed` until the owner adopts.
- **Evals**: none yet. The week 3 reviewer test in `docs/PLAN.md` is the first candidate.
- **Never without the owner**: commit or push; live calls that spend Brave, YouTube or Claude quota; adding a scraper
  or data broker; widening a source's entry in `taxonomy.RIGHTS`; changing `ranking.WEIGHTS`.

## Project notes
- Plan and blueprint reconciliation: `docs/PLAN.md`. Read it before changing scope.
- Run: `.venv\Scripts\python -m creator_intel demo`, then `streamlit run app.py` (bound to 127.0.0.1).
- Invariants, each with a test: only accepted evidence-backed tags rank; material without the `ai` right never
  reaches the model; metrics without `derive` are never scored; a missing metric is NULL, not 0; snapshots are
  append-only; a model tag never replaces a reviewer's; replaying an import duplicates nothing.
- Tools: `python -m creator_intel doctor | evaluate | weekly <brief> | prepublish ... | export-labels <brief> f.csv |
  ask <brief> "q" [--ai] | learn <brief> | export-training <brief> f.jsonl | trust-ai | scrape <brief> --platform all`.
  `doctor --live` and `weekly` with keys set spend real quota: the owner runs those, not Claude.
- A check with nothing to inspect reports "not checked", never "ok" (`prepublish`); the launch gate is `None`
  until every suggestion has two reviewers' verdicts (`analysis.evaluation`).
- Tests never call a model or a network service: stub `labeling.label_pending` / `labeling.check_access` and the
  `adapters.*` calls.
- Exe: `python scripts/build_exe.py` builds `dist/CreatorResearch/` and smoke-tests it (not part of verify: it takes
  minutes). `launcher.py` must not import `creator_intel` or `streamlit` at module top: `prepare()` loads `.env`
  first, and `taxonomy` reads settings at import. `.env` may only set the names in `launcher.ENV_NAMES`.
- Settings typed in the tool go through `launcher.save_env` (never write `.env` another way); a new one is added to
  `SETTINGS` in `app.py` only if it is read at call time, not at import. `app.py` imports `launcher`, which the
  build bundles as a data file.
- New screens go in the `screens` dict at the bottom of `app.py`; plan steps live in `analysis.progress` and must be
  computed per brief from data, never from the shared `usage` counters.
- AI model: Claude, or the OpenAI-compatible server in `CI_AI_URL` (`CI_AI_MODEL`, `CI_AI_KEY`). A server that
  is not this computer gets nothing until `labeling.trust(db)` (Assistant screen, `trust-ai`); never bypass
  `labeling.unconfirmed`. `labeling.model_name()` is never "reviewer". Callers catch `NoCredential`,
  `SourceError` and `anthropic.APIError`.
- Tailoring (`tracker.get_tuning`) applies to every recommendation path: `compare`, `improvements`,
  `ai_improvements`, `promote_for_ai`. A new path must read it too. Widgets holding per-brief choices carry the
  brief id in their key.
- `assistant.facts` holds handles, labels and numbers only: never a caption, title or note. The `ai` right
  covers written material; derived numbers follow `derive`.
- `learner.evaluate` splits by creator, never by video (lesson 0003). `revoke broker` deletes trained models.
- Every AI call goes through `labeling.ask()`. Script builder (`scripts.py`): only labels and
  `reviewer_note` evidence with the `ai` right may reach the model; every string it returns passes `copies()` and
  `_clip()` before it is shown or stored.
- Login: with `CI_PASSWORD` set, the gate at the top of `app.py` stops the script before anything is drawn; code
  that shows data must stay below it, and every `on_click` / `on_change` action is wrapped in `unlocked_only`
  (actions run before the gate; a test checks this). Tries go through `launcher.unlock` (shared wait, never per session).
- Themes: day and night palettes live in `.streamlit/config.toml` and `launcher.LOOKS`; a test keeps them equal.
- Scripted multi-line edits through a shell heredoc have failed here repeatedly; use the editor tool.
- Broker (`broker.py`, Apify): the owner asked for it on 2026-11-19, overriding the earlier no-scraper decision. A
  real scrape costs credits and is the owner's to run. Scraped items are untrusted: everything goes through
  `broker.normalize` (links rebuilt by `parse_url`, unknown stays None). New settings must be added to
  `launcher.ENV_NAMES` (with the setting it belongs to, if any) or the exe will ignore them in `.env`.
- Screens are chosen in the sidebar (`SCREENS` in `app.py`); an input whose value must survive a screen change
  goes in `KEPT_INPUTS`. Screen tests select the screen first: `at.sidebar.radio(key="nav").set_value(...)`.
- Third-party text (titles, model output) shown in Markdown goes through `pipeline.md()`.
- Do not pass code containing backslashes through a shell heredoc; use the editor tool.
- `sqlite3.Row` cannot be used as a Streamlit widget option; convert to `dict`.
