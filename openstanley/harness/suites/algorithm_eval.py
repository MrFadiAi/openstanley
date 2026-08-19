"""Algorithm fitness eval — would the X ranking model like these drafts?

M generated drafts scored by gen/algorithm.py with pinned timing: metrics are
mean score, % strong (≥65), % weak (<35), and the factor distribution (which
signals helped/hurt across the sample).
"""
from __future__ import annotations

from ..base import EvalContext, sample_posts
from ...core import db
from ...gen.algorithm import score_draft


def run(ctx: EvalContext) -> dict:
    profile = db.get_acct_setting("style_profile") or {}
    topics = (profile.get("stats") or {}).get("topics") or []
    posts = sample_posts(ctx, temps=("safe", "bold", "experimental"))
    scored = []
    for p in posts:
        alg = score_draft(p["text"], now_hour=12, account_topics=topics)
        scored.append({"idea": p["idea"], "temp": p["temp"],
                       "text": p["text"][:140], "score": alg["score"],
                       "grade": alg["grade"],
                       "factors": alg["factors"]})
    scores = [s["score"] for s in scored]
    mean = round(sum(scores) / max(1, len(scores)), 1)

    # factor distribution: average impact per factor name across the sample
    agg: dict[str, dict] = {}
    for s in scored:
        for f in s["factors"]:
            a = agg.setdefault(f["name"], {"sum": 0.0, "neg": 0, "n": 0})
            a["sum"] += f["impact"]
            a["neg"] += 1 if f["impact"] < 0 else 0
            a["n"] += 1
    factors = [{"name": k, "avg_impact": round(v["sum"] / v["n"], 1),
                "pct_negative": round(100 * v["neg"] / v["n"], 0)}
               for k, v in sorted(agg.items(), key=lambda kv: kv[1]["sum"])]

    return {
        "score": mean,
        "details": {
            "samples": scored,
            "mean": mean,
            "pct_strong": round(100 * sum(1 for x in scores if x >= 65)
                                / max(1, len(scores)), 0),
            "pct_weak": round(100 * sum(1 for x in scores if x < 35)
                              / max(1, len(scores)), 0),
            "factor_distribution": factors,
        },
    }
