"""A small model of your own, trained on this brief's catalog, and a training file for a local AI model.

The model answers one question: given what is known before a video is posted (platform, length, how long
the caption is, the caption's words), does it look like the videos that went fast (outlier or strong)
or like the ones that did not? It is naive Bayes: it counts how often each trait appears in fast and in
other videos. It runs on this computer, needs no AI service, and is retrained whenever you ask.

What it is not: a predictor of views. It learns what the fast videos in your catalog had in common, and
a common trait is not a cause. So training always tests the model on videos it was not shown and puts
the result beside the score of always guessing the commoner answer; when it does no better, it says so.

Caption words are used only when the broker source is permitted for AI use (taxonomy.RIGHTS), and the
tiers come only from readings permitted for derived scores. `revoke broker` deletes the trained model.

ponytail: naive Bayes over present traits only, no weighting of rare words, no tuning. Enough to start
from; swap train() and chance() for something stronger once the catalog holds a few hundred videos.
"""
import math
import re
from collections import defaultdict

from . import db as store
from . import scripts, tracker
from .taxonomy import RIGHTS

MIN_EXAMPLES, MIN_EACH, FOLDS = 30, 5, 5
FAST = ("outlier", "strong")
TRAINING_SYSTEM = ("You judge how a short video is likely to do against its creator's usual, from its platform, "
                   "length and caption. Answer with one word: outlier (three times their usual or more), strong "
                   "(one and a half times) or typical.")


def _may_read() -> bool:
    return "ai" in RIGHTS["broker"].split(",")


def features(text, platform=None, duration_sec=None, with_words: bool = True) -> set[str]:
    """The traits of one video that are known before it is posted."""
    words = re.findall(r"[\w']{3,}", (text or "").lower())
    found = {f"word:{w}" for w in words} if with_words else set()
    if text:
        count = len(text.split())
        found.add("caption:" + ("under 6 words" if count < 6 else "6 to 15 words" if count <= 15 else "over 15 words"))
    if platform:
        found.add(f"platform:{platform}")
    if duration_sec is not None:
        found.add("length:" + scripts.nice(scripts.band_for(duration_sec)))
    return found


def examples(db, brief_id: int) -> list[tuple[set, bool, int]]:
    """(traits, went fast, creator) for every catalogued video that has a tier, in a fixed order.
    A tier means "against the creator's own usual" where they have a history and "against the other
    creators on the platform" where they do not (tracker.rows); both count as fast here."""
    with_words = _may_read()
    return [(features(r["title"], r["platform"], r["duration_sec"], with_words), r["tier"] in FAST, r["account_id"])
            for r in sorted(tracker.rows(db, brief_id), key=lambda r: r["post_id"]) if r["tier"]]


def train(found: list[tuple]) -> dict:
    n, counts, spread = [0, 0], defaultdict(lambda: [0, 0]), defaultdict(set)
    for traits, fast, *creator in found:
        n[fast] += 1
        for name in traits:
            counts[name][fast] += 1
            if fast:
                spread[name].update(creator)
    # a trait seen once says nothing about the next video
    kept = {name: c for name, c in counts.items() if sum(c) >= 2}
    return {"n": n, "counts": kept, "creators": {name: len(spread[name]) for name in kept}}  # creators with it in a fast video


def _weight(model: dict, name: str) -> float:
    """How far one trait moves the answer: above zero towards fast, below towards not."""
    (slow, fast), n = model["counts"][name], model["n"]
    return math.log((fast + 1) / (n[1] + 2)) - math.log((slow + 1) / (n[0] + 2))


def chance(model: dict, traits: set) -> float:
    n = model["n"]
    odds = math.log((n[1] + 1) / (n[0] + 1)) + sum(_weight(model, name) for name in traits if name in model["counts"])
    return 1 / (1 + math.exp(-max(-50.0, min(50.0, odds))))


def evaluate(found: list[tuple[set, bool, int]]) -> dict:
    """Train on four fifths of the creators, test on the fifth, five times over. A creator is never on
    both sides: their signature words and their own usual would otherwise let the model recognise them
    instead of learning anything that carries over. "baseline" is what always giving the commoner answer
    scores; the model is called useful only when it beats that by more than chance would (about two
    standard errors: 1 / sqrt(videos), and never less than five points)."""
    fold_of = {creator: i % FOLDS for i, creator in enumerate(sorted({e[2] for e in found}))}
    right = 0
    for fold in range(FOLDS):
        model = train([e for e in found if fold_of[e[2]] != fold])
        right += sum((chance(model, traits) >= 0.5) == fast for traits, fast, creator in found if fold_of[creator] == fold)
    fast = sum(e[1] for e in found)
    accuracy, baseline = right / len(found), max(fast, len(found) - fast) / len(found)
    return {"accuracy": accuracy, "baseline": baseline,
            "useful": accuracy >= baseline + max(0.05, 1 / math.sqrt(len(found)))}


def fit(db, brief_id: int) -> dict:
    """Train on the catalog and keep the model. Refuses, saying what is missing, on too little data."""
    found = examples(db, brief_id)
    fast, creators = sum(e[1] for e in found), len({e[2] for e in found})
    report = {"trained": False, "examples": len(found), "fast": fast, "with_captions": _may_read(), "why": None}
    if len(found) < MIN_EXAMPLES or min(fast, len(found) - fast) < MIN_EACH or creators < FOLDS:
        report["why"] = (f"it needs at least {MIN_EXAMPLES} tracked videos with a tier, from at least {FOLDS} creators, "
                         f"{MIN_EACH} of them fast and {MIN_EACH} not. There are {len(found)} from {creators} creators, "
                         f"{fast} of them fast")
        return report
    report.update(trained=True, trained_at=store.now_iso(), **evaluate(found))
    store.set_setting(db, f"model:{brief_id}", {"model": train(found), "report": report})
    return report


def load(db, brief_id: int) -> dict | None:
    return store.get_setting(db, f"model:{brief_id}")


def score(db, brief_id: int, text: str, platform=None, duration_sec=None) -> dict | None:
    """What the trained model makes of a draft: the chance it looks like the fast videos, the share of
    fast videos it was trained on for comparison, and the traits that pushed the answer each way.
    None when no model has been trained."""
    kept = load(db, brief_id)
    if not kept:
        return None
    model, n = kept["model"], kept["model"]["n"]
    traits = features(text, platform, duration_sec, kept["report"]["with_captions"])
    pulls = sorted(((name.replace(":", " ", 1), round(_weight(model, name), 2)) for name in traits if name in model["counts"]),
                   key=lambda pull: -pull[1])
    return {"chance": chance(model, traits), "base": n[1] / sum(n), "useful": kept["report"]["useful"],
            "up": [p for p in pulls if p[1] > 0][:5], "down": [p for p in reversed(pulls) if p[1] < 0][:5]}


def top_traits(db, brief_id: int, limit: int = 5) -> list[str]:
    """The traits that most set the fast videos apart, each seen in fast videos of at least three
    creators (one creator's signature word is theirs, not a trait of fast videos). Empty unless
    a model is trained and beat guessing. Caption words are left out when the broker source is no longer
    permitted for AI use, whatever it was when the model was trained."""
    kept = load(db, brief_id)
    if not kept or not kept["report"]["useful"]:
        return []
    model = kept["model"]
    names = [name for name, (_, fast) in model["counts"].items()
             if model.get("creators", {}).get(name, 0) >= 3 and _weight(model, name) > 0 and (_may_read() or not name.startswith("word:"))]
    return [name.replace(":", " ", 1) for name in sorted(names, key=lambda name: -_weight(model, name))[:limit]]


def training_examples(db, brief_id: int) -> list[dict]:
    """The catalog as chat-format examples (one JSON object per line when written out), the format local
    fine-tuning tools take. Captions are in every example, so this needs the AI right on the broker source."""
    if not _may_read():
        raise ValueError("scraped captions are not permitted for AI use (CI_BROKER_RIGHTS), so there is nothing to train on")
    return [{"messages": [
        {"role": "system", "content": TRAINING_SYSTEM},
        {"role": "user", "content": f"Platform: {r['platform']}\nLength: "
                                    f"{'unknown' if r['duration_sec'] is None else str(r['duration_sec']) + ' s'}\n"
                                    f"Caption: {r['title']}"},
        {"role": "assistant", "content": r["tier"]}]}
        for r in sorted(tracker.rows(db, brief_id), key=lambda r: r["post_id"]) if r["tier"] and r["title"]]
