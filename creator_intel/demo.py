"""Fictional demo data: a brief, ten creator links, reviewed posts. Nothing here describes a real channel."""
from . import db as store
from . import pipeline
from .taxonomy import Brief
from .urls import parse_url

MATCH = {"audience_promise": "learn_one_repair", "format_subtype": "demonstration", "topic": "sewing_repair",
         "production_setup": "single_camera", "opening": "result_first", "duration_band": "30_60s"}
PEERS = {  # handle -> (posts reviewed, labels)
    "fictional-mend-01": (4, {**MATCH, "topic": "clothing_care"}),
    "fictional-mend-02": (3, {**MATCH, "delivery": "calm"}),
    "fictional-mend-03": (3, {**MATCH, "production_setup": "multi_camera"}),
    "fictional-mend-04": (3, {**MATCH, "audience_promise": "entertain", "format_subtype": "vlog",
                             "delivery": "energetic", "opening": "story"}),
    "fictional-mend-05": (1, MATCH),
}


NAME = "Beginner sewing (fictional demo)"


def seed(db) -> int:
    existing = [b.id for b in store.list_briefs(db) if b.name == NAME]
    if existing:
        return existing[0]
    brief_id = store.save_brief(db, Brief(
        name=NAME, platform="youtube", language="en", format_family="short",
        objective="Grow subscribers with one-repair tutorials", constraints="One camera, two hours per video",
        owner={"audience_promise": "learn_one_repair", "format_subtype": "demonstration", "topic": "sewing_repair",
               "delivery": "calm", "opening": "problem_first"},
        owner_setup="single_camera", topics=["sewing_repair", "clothing_care", "cosplay"],
        audience_promises=["learn_one_repair", "entertain"], related={"topic": [["sewing_repair", "clothing_care"]]}))
    pipeline.import_links(db, brief_id, [f"https://www.youtube.com/@fictional-mend-{n:02d}" for n in range(1, 11)])
    for i, (handle, (count, labels)) in enumerate(PEERS.items()):
        account = store.upsert_account(db, parse_url(f"https://www.youtube.com/@{handle}"))
        for n in range(count):
            post = store.upsert_post(db, account, parse_url(f"https://www.youtube.com/shorts/demo{i:02d}post{n:01d}"),
                                     title=f"Fictional repair {n + 1}", published_at=f"2026-11-{10 + n:02d}T12:00:00+00:00")
            ev = store.add_evidence(db, post, "reviewer_note",
                                    "Shows the finished hem first, then demonstrates one stitch.", timestamp="00:02")
            for dim, value in labels.items():
                store.set_tag(db, ev, dim, value)
    pipeline.rank(db, brief_id)
    return brief_id
