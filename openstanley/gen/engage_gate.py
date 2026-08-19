"""Engage quality gate — score TARGET tweets before spending reply budget.

Reply quality on X decides everything: a generic reply on a dead tweet is
wasted cap budget (10 replies/day) and looks botty. This gate runs BEFORE
the LLM drafts anything, so rejected targets cost zero LLM calls.

Five fast heuristic layers, each 0-1:

  recency        tweet age — full points < 3h, linear decay to 0 at 24h,
                 hard reject > 48h (the X reply window is ~2-6h; after
                 that a reply is noise). Missing created_at → WARN, not
                 reject: recency goes neutral 0.5.
  traction       log-scaled likes+reposts of the TARGET tweet — a rising
                 tweet is a surface; 500 combined interactions ≈ full.
  author_surface avg engagement of the author's posts already in our
                 corpus (they engage back); neutral 0.5 when unseen.
  crowding       reply_count — sweet spot 1-15 (0 replies = nobody's
                 talking there; 40+ replies = we're bot #41).
  niche_fit      token overlap of the tweet against the brain's niche map
                 (style-profile topics + evergreen themes).

Composite is 0-100 with weights from config [agent.engage_gate]
(recency .35 / traction .25 / author .15 / crowding .10 / fit .15,
threshold 55). score_target() is deterministic for pinned `now` — pass
`now` in tests like score_draft() takes now_hour.

Reads (same searches, same caps as before the gate) are unchanged; the
gate only decides which targets are WORTH a draft. The approval gate
downstream is untouched: kept targets still produce approval-gated drafts.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from ..core import db
from ..core.config import Config

# --- tuning constants --------------------------------------------------------

FRESH_H = 3.0          # full recency points below this age
DECAY_H = 24.0         # recency hits 0 here
STALE_H = 48.0         # hard reject: the conversation is dead
TRACTION_FULL = 500.0  # likes+reposts that score full traction
AUTHOR_FULL = 200.0    # author avg likes+reposts+replies that score full
CROWD_SWEET = (1, 15)  # reply_count sweet spot (inclusive)
CROWD_FLOOR = 0.3      # worst crowding score at absurd reply counts
FIT_TERMS_FULL = 3     # niche-term hits that score full fit

DEFAULT_GATE: dict = {
    "weights": {"recency": 0.35, "traction": 0.25, "author": 0.15,
                "crowding": 0.10, "fit": 0.15},
    "threshold": 55,
    "max_targets": 12,
}


@dataclass
class TargetScore:
    """One scored reply target — composite + component breakdown."""
    score: int                      # composite 0-100 (0 = hard-rejected)
    recency: float
    traction: float
    author_surface: float
    crowding: float
    niche_fit: float
    age_h: Optional[float] = None   # parsed age; None = unknown → WARN path
    verdict: str = ""               # fresh | rising | warm | stale
    reasons: list[str] = field(default_factory=list)

    def components(self) -> dict[str, float]:
        return {"recency": round(self.recency, 3), "traction": round(self.traction, 3),
                "author": round(self.author_surface, 3),
                "crowding": round(self.crowding, 3), "fit": round(self.niche_fit, 3)}

    def meta(self) -> dict:
        """JSON-serializable block for draft meta.target_score (approval card)."""
        return {"score": self.score, "verdict": self.verdict,
                "age_h": round(self.age_h, 1) if self.age_h is not None else None,
                "components": self.components(),
                "reasons": self.reasons[:3]}


# --- config access -----------------------------------------------------------

def gate_cfg(cfg: Config) -> dict:
    """[agent.engage_gate] with defaults filled in — partial user config safe.

    A user-supplied `weights` dict REPLACES the default set (missing keys are
    0): the configured weights are the weights that matter, renormalized by
    their sum so any scale works.
    """
    raw = getattr(cfg.agent, "engage_gate", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    user_w = raw.get("weights")
    weights = ({k: float(v) for k, v in user_w.items()}
               if isinstance(user_w, dict) and user_w else dict(DEFAULT_GATE["weights"]))
    return {
        "weights": weights,
        "threshold": float(raw.get("threshold", DEFAULT_GATE["threshold"])),
        "max_targets": int(raw.get("max_targets", DEFAULT_GATE["max_targets"])),
    }


# --- individual layers (pure) ------------------------------------------------

def _age_hours(tweet: dict, now: datetime) -> tuple[Optional[float], bool]:
    """(age in hours, parseable?). Naive timestamps are read as local clock."""
    raw = tweet.get("created_at")
    if not raw:
        return None, False
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if ts.tzinfo is not None and now.tzinfo is None:
        ts = ts.replace(tzinfo=None)  # compare in naive-local like the rest of the app
    return max(0.0, (now - ts).total_seconds() / 3600.0), True


def _recency(age_h: Optional[float]) -> tuple[float, list[str]]:
    if age_h is None:
        return 0.5, ["created_at missing — recency neutral (WARN)"]
    if age_h <= FRESH_H:
        return 1.0, []
    if age_h >= STALE_H:
        return 0.0, [f"stale {age_h:.0f}h > {STALE_H:.0f}h — hard reject"]
    if age_h >= DECAY_H:
        return 0.0, [f"past the {DECAY_H:.0f}h reply window"]
    decayed = 1.0 - (age_h - FRESH_H) / (DECAY_H - FRESH_H)
    return decayed, [f"age {age_h:.1f}h — cooling"]


def _log_scale(value: float, full: float) -> float:
    """log10 ramp: 0 → 0, `full` → ~1.0, saturating after."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1.0 + value) / math.log10(1.0 + full))


def _crowding(reply_count: int) -> tuple[float, list[str]]:
    lo, hi = CROWD_SWEET
    r = max(0, int(reply_count or 0))
    if r == 0:
        return 0.7, ["0 replies — nobody's talking there"]
    if r <= hi:
        return 1.0, []
    # mild linear penalty past the sweet spot, floored
    penalty = max(0.0, 1.0 - (r - hi) / 150.0)
    return CROWD_FLOOR + (1.0 - CROWD_FLOOR) * penalty, [f"{r} replies — crowded"]


def _niche_terms(cfg: Config) -> set[str]:
    """Brain's niche map: style-profile topics + evergreen theme words."""
    terms: set[str] = set()
    profile = db.get_setting("style_profile") or {}
    for t in (profile.get("stats") or {}).get("topics") or []:
        for w in re.findall(r"[a-z؀-ۿ]{3,}", str(t).lower()):
            terms.add(w)
    for theme in cfg.agent.evergreen_themes or []:
        for w in re.findall(r"[a-z؀-ۿ]{3,}", str(theme).lower()):
            terms.add(w)
    return terms


def _niche_fit(text: str, terms: set[str]) -> tuple[float, list[str]]:
    if not terms:
        return 0.5, ["no niche map yet — fit neutral"]
    low = text.lower()
    hits = sum(1 for w in set(re.findall(r"[a-z؀-ۿ]{3,}", low)) if w in terms)
    if hits == 0:
        return 0.0, ["off the niche map"]
    return min(1.0, hits / FIT_TERMS_FULL), []


def _author_surface(handle: str) -> float:
    """Avg interactions of the author's posts in our corpus; 0.5 if unseen."""
    if not handle:
        return 0.5
    with db.connect() as c:
        rows = c.execute(
            "SELECT likes, reposts, replies FROM posts "
            "WHERE author_handle=? AND is_own=0 LIMIT 20", (handle,)).fetchall()
    if not rows:
        return 0.5
    per_post = [(r["likes"] or 0) + (r["reposts"] or 0) + (r["replies"] or 0)
                for r in rows]
    return _log_scale(sum(per_post) / len(per_post), AUTHOR_FULL)


def _verdict(age_h: Optional[float], traction: float) -> str:
    fresh = age_h is not None and age_h <= 6.0
    if age_h is not None and age_h > DECAY_H:
        return "stale"
    if fresh and traction >= 0.6:
        return "rising"
    if fresh:
        return "fresh"
    return "warm"


# --- main entry --------------------------------------------------------------

def score_target(cfg: Config, tweet: dict, now: datetime) -> TargetScore:
    """Score one reply target 0-100. Deterministic for pinned `now`."""
    g = gate_cfg(cfg)
    w = g["weights"]
    age_h, parseable = _age_hours(tweet, now)
    recency, notes = _recency(age_h)
    reasons = list(notes)
    if not parseable and tweet.get("created_at"):
        reasons.append(f"created_at unparseable: {str(tweet['created_at'])[:24]}")

    likes = int(tweet.get("likes") or 0)
    reposts = int(tweet.get("reposts") or 0)
    traction = _log_scale(likes + reposts, TRACTION_FULL)
    if likes + reposts == 0:
        reasons.append("no traction yet")

    author = _author_surface(str(tweet.get("author_handle") or ""))
    crowding, notes = _crowding(int(tweet.get("replies") or 0))
    reasons.extend(notes)
    fit, notes = _niche_fit(tweet.get("text") or "", _niche_terms(cfg))
    reasons.extend(notes)

    hard_reject = parseable and age_h is not None and age_h > STALE_H
    if hard_reject:
        score = 0
    else:
        total_w = sum(w.values()) or 1.0
        score = round(100.0 * sum(w.get(k, 0.0) * v for k, v in (
            ("recency", recency), ("traction", traction), ("author", author),
            ("crowding", crowding), ("fit", fit))) / total_w)

    return TargetScore(
        score=score, recency=recency, traction=traction, author_surface=author,
        crowding=crowding, niche_fit=fit, age_h=age_h if parseable else None,
        verdict=_verdict(age_h if parseable else None, traction), reasons=reasons)


def filter_targets(cfg: Config, tweets: list[dict],
                   now: datetime) -> tuple[list[tuple[dict, TargetScore]],
                                           list[tuple[dict, TargetScore]]]:
    """Gate a candidate batch → (kept, rejected), kept sorted by score desc,
    capped at [agent.engage_gate] max_targets. Rejects are logged (count +
    top reasons) for the Insights/debug view."""
    g = gate_cfg(cfg)
    scored = [(t, score_target(cfg, t, now)) for t in tweets]
    kept = sorted(((t, s) for t, s in scored if s.score >= g["threshold"]),
                  key=lambda pair: -pair[1].score)[:g["max_targets"]]
    rejected = [(t, s) for t, s in scored if s.score < g["threshold"]]

    if rejected or scored:
        top = Counter(s.reasons[0] if s.reasons else f"score {s.score} < {g['threshold']:.0f}"
                      for _, s in rejected)
        if rejected:
            summary = ", ".join(f"{r} ×{n}" for r, n in top.most_common(3))
            db.log("engage", f"gate: rejected {len(rejected)}/{len(scored)} "
                             f"reply targets — {summary}")
        warned = sum(1 for _, s in scored if s.age_h is None and
                     any("created_at missing" in r for r in s.reasons))
        if warned:
            db.log("engage", f"gate: {warned} target(s) missing created_at — "
                             "recency neutral, not rejected", level="warn")
    return kept, rejected


def asdict_score(ts: TargetScore) -> dict:
    """Full dataclass dump (tests/debug). Use ts.meta() for draft meta."""
    return asdict(ts)
