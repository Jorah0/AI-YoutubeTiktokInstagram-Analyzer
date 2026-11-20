"""Label vocabulary, scoring weights, source rights and the Brief record."""
import os
from typing import Literal

from pydantic import BaseModel, Field

RULES_VERSION = "1"

# Editorial priorities from the blueprint; tune against reviewer judgments (plan, week 3).
WEIGHTS = {"audience_promise": 0.30, "format_subtype": 0.25, "topic": 0.20, "delivery": 0.15, "feasibility": 0.10}

# Cheapest to most expensive. Feasibility compares a peer's observed setup with the owner's.
SETUP_LEVELS = ["phone_handheld", "single_camera", "multi_camera", "studio_crew"]

OBSERVED = ["topic", "audience_promise", "format_subtype", "delivery", "opening", "duration_band", "production_setup"]

# Text cannot establish these; only a reviewer who watched the post can.
TEXT_BLIND = {"delivery", "production_setup"}

DEFAULT_TAXONOMY = {
    "format_subtype": ["demonstration", "talking_head", "voiceover_broll", "listicle", "vlog", "skit", "reaction",
                       "interview", "screen_recording"],
    "delivery": ["calm", "energetic", "humorous", "instructional", "asmr"],
    "opening": ["problem_first", "result_first", "question", "bold_claim", "story", "direct_intro"],
    "duration_band": ["under_30s", "30_60s", "1_3min", "3_10min", "over_10min"],
    "production_setup": SETUP_LEVELS,
}

# What each source lets us do with its material: store it, send it to the model ("ai"),
# compute scores from it ("derive"). "discovery" means: keep the link, nothing else.
# YouTube API data stays store-only until you have confirmed the derived-metrics policy
# applies to you; then set CI_YOUTUBE_RIGHTS=store,ai,derive.
RIGHTS = {
    "reviewer_note": "store,ai,derive",
    "owner_export": "store,ai,derive",
    "youtube_api": "".join(os.environ.get("CI_YOUTUBE_RIGHTS", "store").split()),  # tolerate "store, ai"
    "brave": "discovery",
    # Scraped through a data broker at the owner's request, for tracking, AI intake and scoring. Whether
    # that use is allowed is governed by the broker's and each platform's terms: narrow it here if not.
    "broker": "".join(os.environ.get("CI_BROKER_RIGHTS", "store,ai,derive").split()),
}

Platform = Literal["youtube", "tiktok", "instagram"]


class Brief(BaseModel):
    id: int | None = None
    name: str
    platform: Platform
    language: str = "en"
    format_family: Literal["short", "long"] = "short"
    objective: str = ""
    constraints: str = ""
    owner: dict[str, str] = Field(default_factory=dict)  # the owner's own label per dimension
    owner_setup: str = "single_camera"
    topics: list[str] = Field(default_factory=list)
    audience_promises: list[str] = Field(default_factory=list)
    related: dict[str, list[list[str]]] = Field(default_factory=dict)  # reviewer-approved "close enough" pairs

    def taxonomy(self) -> dict[str, list[str]]:
        return {**DEFAULT_TAXONOMY, "topic": self.topics, "audience_promise": self.audience_promises}
