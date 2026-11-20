"""The inspectable ranking rule. Deterministic: same labels in, same shortlist out."""
from collections import Counter
from dataclasses import dataclass

from .taxonomy import SETUP_LEVELS, WEIGHTS

MIN_POSTS = 3
MIN_COVERAGE = 0.80
MAX_PEERS = 5


@dataclass
class Score:
    fit: float | None  # 0-100 over the observed dimensions; None when nothing was observed
    coverage: float  # share of the weights that was actually observed
    match: dict  # dimension -> 1 exact, 0.5 related, 0 incompatible, None unknown
    stretch: bool  # format fits, but the peer's setup costs more than the owner's


@dataclass
class Cand:
    account_id: int
    posts: int
    score: Score


def aggregate(posts: list[dict]) -> dict:
    """Most common label per dimension across a creator's sampled posts.
    Ties, or fewer than two supporting posts, leave the dimension unknown (absent)."""
    out = {}
    for dim in {d for p in posts for d in p}:
        top = Counter(p[dim] for p in posts if p.get(dim)).most_common(2)
        if top and top[0][1] >= 2 and (len(top) == 1 or top[1][1] < top[0][1]):
            out[dim] = top[0][0]
    return out


def score(owner: dict, owner_setup: str, peer: dict, related: dict | None = None) -> Score:
    related = related or {}
    match, gap = {}, None
    for dim in WEIGHTS:
        if dim == "feasibility":
            setup = peer.get("production_setup")
            if setup in SETUP_LEVELS and owner_setup in SETUP_LEVELS:
                gap = SETUP_LEVELS.index(setup) - SETUP_LEVELS.index(owner_setup)
                # ponytail: one level up counts as "related"; swap for a budget model if levels prove too coarse
                match[dim] = 1 if gap <= 0 else 0.5 if gap == 1 else 0
            else:
                match[dim] = None
        elif owner.get(dim) and peer.get(dim):
            a, b = owner[dim], peer[dim]
            pairs = [set(p) for p in related.get(dim, [])]
            match[dim] = 1 if a == b else 0.5 if {a, b} in pairs else 0
        else:
            match[dim] = None
    coverage = round(sum(WEIGHTS[d] for d, m in match.items() if m is not None), 4)
    earned = sum(WEIGHTS[d] * m for d, m in match.items() if m is not None)
    return Score(100 * earned / coverage if coverage else None, coverage, match,
                 stretch=match["format_subtype"] == 1 and gap is not None and gap > 0)


def shortlist(cands: list[Cand]) -> tuple[list[Cand], list[Cand]]:
    """(up to five ranked peers, sparse candidates that need more review)."""
    def ok(c):
        return c.posts >= MIN_POSTS and c.score.fit is not None and c.score.coverage >= MIN_COVERAGE
    ranked = sorted((c for c in cands if ok(c)), key=lambda c: (-c.score.fit, -c.score.coverage, -c.posts))
    return ranked[:MAX_PEERS], [c for c in cands if not ok(c)]


def explain(s: Score, owner: dict, peer: dict) -> str:
    def names(value):
        return ["production budget" if d == "feasibility" else d.replace("_", " ")
                for d, m in s.match.items() if m == value]
    parts = []
    if names(1):
        parts.append("Matches your " + ", ".join(names(1)) + ".")
    for d, m in s.match.items():
        if m == 0.5 and d != "feasibility":
            parts.append(f"Related {d.replace('_', ' ')}: {peer[d]} vs your {owner[d]}.")
        if m == 0 and d != "feasibility":
            parts.append(f"Different {d.replace('_', ' ')}: {peer[d]} vs your {owner[d]}.")
    if s.stretch:
        parts.append(f"Stretch: their setup ({peer['production_setup']}) is above your budget.")
    elif s.match.get("feasibility") == 0:
        parts.append(f"Their setup ({peer['production_setup']}) is well above your budget.")
    if names(None):
        parts.append("Unverified: " + ", ".join(names(None)) + ".")
    return " ".join(parts)


def size_note(ratio: float | None) -> str | None:
    """How a creator's audience compares with the owner's. Context only: it is not part of the fit score.
    Creators between half and ten times your size tend to be the most transferable comparison."""
    if ratio is None:
        return None
    size = f"about {ratio:.1f}x your audience" if ratio >= 1 else f"about {ratio:.0%} of your audience"
    return size if 0.5 <= ratio <= 10 else size + " (outside the 0.5x to 10x range that compares best)"


def suggest_experiment(owner_opening: str | None, peer_openings: list) -> str | None:
    """One testable idea: an opening device that several shortlisted peers share and the owner does not use."""
    top = Counter(o for o in peer_openings if o).most_common(1)
    if not top or top[0][1] < 2 or top[0][0] == owner_opening:
        return None
    opening, n = top[0]
    return (f"{n} comparable creators repeatedly open with '{opening}'"
            f"{f' (you open with {owner_opening!r})' if owner_opening else ''}. "
            f"Film an original video of your own and test '{opening}' against your usual opening: "
            "four matched pairs, same topic family, duration range and posting slots, one primary metric, "
            "measured at seven days.")
