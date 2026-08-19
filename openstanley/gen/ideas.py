"""Idea engine + story bank — OpenStanley's "top performing ideas every day".

Two generators live here:
  - generate_ideas(): the original LLM batch generator (rich, needs a model).
  - replenish(): v0.4.3 deterministic mining chain that keeps the bank from
    ever running dry with NO LLM and (almost always) no X reads. When the
    bank drops below `min_bank` it distills a batch of new angles from, in
    priority order: unmined scan outliers → brain journal insights → fresh
    study reads (only when a client is wired and a+b came up short) →
    evergreen themes × strategy statements. Every idea carries its source
    badge (scan|brain|study|evergreen) for analytics.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..core import db
from . import brain as brain_mod
from .llm import chat, extract_json
from ..core.config import Config

IDEA_SYSTEM = """You are a growth strategist for a solo creator on X (Twitter).
Generate post IDEAS grounded in real niche evidence. Return STRICT JSON:
{"ideas": [
  {"title": "short title", "angle": "the specific take, 1-2 sentences",
   "format": "one-liner|hook+insight|thread|quote-post", "score": 0.0-10.0,
   "source": "why now / evidence"}
]}
Rules: angles must be opinionated or useful — never generic. No motivation-poster filler.
Scores reflect predicted engagement for THIS account's voice and past winners.
Reply-bait formats rank highest on X (replies are 27-75x a like). Media-friendly angles get a boost (2x reach).
When a big niche announcement appears in the evidence, "quote-post" is a strong
format: the idea angle then describes the take to add ON TOP of that tweet
(mention which announcement in "source")."""


def generate_ideas(cfg: Config, count: int = 10) -> list[dict]:
    niche = db.niche_posts(limit=60)
    own = db.own_posts(limit=120)
    used = [i["title"] for i in db.fresh_ideas(60)] + \
           [p["text"][:60] for p in own[:40]]

    niche_block = "\n".join(
        f"- [{p['author_handle']}] {p['text'][:160]} (♥{p['likes']} RT{p['reposts']})"
        for p in niche[:40]
    ) or "(no niche data yet)"
    own_block = "\n".join(f"- {p['text'][:120]} (♥{p['likes']})" for p in own[:25]) or "(none)"
    themes = "\n".join(f"- {t}" for t in cfg.agent.evergreen_themes)

    user = f"""Niche winners (other accounts, last days):
{niche_block}

This account's recent posts:
{own_block}

Evergreen themes the account owner cares about:
{themes}

Recently used ideas (AVOID duplicating):
{chr(10).join(used[:40]) or '(none)'}

Generate {count} NEW post ideas mixing: 40% niche-react (respond to what's landing),
35% evergreen (owner's themes), 25% remix (fresh angle on this account's own winners)."""

    raw = chat(cfg.llm, brain_mod.brain_context() + "\n\n" + IDEA_SYSTEM, user,
               temperature=0.9, json_mode=True)
    data = extract_json(raw)
    ideas = data.get("ideas", data if isinstance(data, list) else [])
    added = []
    for idea in ideas[:count]:
        if not idea.get("title"):
            continue
        iid = db.add_idea(
            title=str(idea.get("title", ""))[:200],
            angle=str(idea.get("angle", ""))[:600],
            fmt=str(idea.get("format", "one-liner")),
            source=str(idea.get("source", ""))[:300],
            score=float(idea.get("score", 5) or 5),
        )
        added.append(iid)
    db.log("study", f"generated {len(added)} ideas (bank now {db.idea_count()})")
    return added


# ---------- v0.4.3 self-replenishing bank (no LLM, X reads only in path c) ----------

SOURCE_SCAN = "scan"
SOURCE_BRAIN = "brain"
SOURCE_STUDY = "study"
SOURCE_EVERGREEN = "evergreen"
SOURCES = (SOURCE_SCAN, SOURCE_BRAIN, SOURCE_STUDY, SOURCE_EVERGREEN)

DEFAULT_MIN_BANK = 15   # below this the create loop would silently starve
DEFAULT_BATCH = 8
NOVELTY_OVERLAP = 0.7   # token containment above this = angle already banked.
#                        0.7 keeps theme-sharing siblings distinct (two evergreen
#                        angles on one strategy share ~0.6) while still catching
#                        paraphrases (near-copies land at ~1.0).
OUTLIER_TOP_PCT = 0.10  # mine only the top decile by engagement rate…
OUTLIER_MIN = 3         # …but at least this many on a thin corpus
STUDY_READ_QUERIES = 2  # path c: at most this many throttled searches
STUDY_READ_LIMIT = 30
JOURNAL_RECENT = 12     # newest journal entries considered
EVERGREEN_STRATEGIES = 6

_STOPWORDS = frozenset("""
the and for are but not you all can her was one our out day get has him his
how its new now old see two way who did yes this that with from they have
will your what when why been were more some them then than here into only
also just over very much after before under again about every
على هذا هذه ذلك التي الذي لكن عند قد هو هي فيها منه بها مثل
""".split())


def _tokens(text: str) -> set[str]:
    """Content tokens for novelty checks (Latin + Arabic, ≥3 chars)."""
    words = re.findall(r"[a-z؀-ۿ][a-z؀-ۿ'\-]*", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    """Token containment: how much of the smaller set the larger covers."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _novel(tokens: set[str], known: list[set[str]]) -> bool:
    return all(_overlap(tokens, k) < NOVELTY_OVERLAP for k in known)


def _bank_tokens() -> list[set[str]]:
    """Token sets of every banked idea — ANY status: a used or discarded idea
    still represents an angle we've already covered (ACTIVE account)."""
    with db.connect() as c:
        rows = c.execute("SELECT title, angle FROM ideas WHERE account_id=?",
                         (db.active_account(),)).fetchall()
    return [_tokens(f"{r['title']} {r['angle'] or ''}") for r in rows]


def _first_sentence(text: str, clip: int = 160) -> str:
    """Best-effort sentence split (Latin + Arabic terminals)."""
    text = (text or "").strip()
    if not text:
        return ""
    first = re.split(r"(?<=[.!?؟])\s+", text)[0].strip()
    if not first:
        first = text[:clip]
    return first[:clip]


def _engagement_shape(p: dict) -> tuple[str, str]:
    """(take-style hint, suggested format) from how the post earned engagement."""
    likes = max(float(p.get("likes") or 0), 1.0)
    if (p.get("replies") or 0) >= likes * 0.5:
        return "a reply-bait question hook", "hook+insight"
    if (p.get("reposts") or 0) >= likes * 0.5:
        return "a quotable one-liner claim", "one-liner"
    if (p.get("bookmarks") or 0) >= likes * 0.3:
        return "a save-worthy step-by-step breakdown", "thread"
    return "a strong standalone take", "one-liner"


def _post_idea(p: dict, source: str, rank: int = 0) -> dict | None:
    """Distill one stored/fresh post into a bankable idea (extractive — no LLM)."""
    hook = _first_sentence(p.get("text") or "")
    tokens = _tokens(hook)
    if len(tokens) < 4:
        return None  # too thin to define an angle
    style, fmt = _engagement_shape(p)
    title = " ".join(hook.split()[:6])[:80]
    rate = float(p.get("engagement") or 0) * 100
    handle = p.get("author_handle") or "niche"
    if source == SOURCE_STUDY:
        angle = (f'Fresh study read from @{handle} ({rate:.1f}% engagement): "{hook}" '
                 f"— our take: {style} on this topic.")
        score = 5.5
    else:
        angle = (f'Outlier from @{handle} ({rate:.1f}% engagement): "{hook}" '
                 f"— our take: {style} on this topic for our audience.")
        score = round(max(5.6, 9.0 - rank * 0.8), 1)
    return {"title": title, "angle": angle[:600], "fmt": fmt,
            "source": source, "score": score, "tokens": tokens}


def _outlier_ideas() -> list[dict]:
    """Top-decile-by-rate niche posts (scan data) not yet represented in the bank."""
    posts = db.niche_posts(limit=200)
    cut = max(OUTLIER_MIN, int(len(posts) * OUTLIER_TOP_PCT))
    out = []
    for rank, p in enumerate(posts[:cut]):
        idea = _post_idea(p, SOURCE_SCAN, rank=rank)
        if idea:
            out.append(idea)
    return out


def _journal_ideas() -> list[dict]:
    """Recent reflection insights that never became ideas."""
    try:
        entries = brain_mod.parse_journal(brain_mod.read("journal"))
    except Exception:  # noqa: BLE001 — an unreadable journal must not break replenish
        return []
    out = []
    for e in entries[:JOURNAL_RECENT]:
        insight = _first_sentence(e.get("body") or "")
        if len(_tokens(insight)) < 3 or insight.lower() in ("(no notes)", "none"):
            continue
        out.append({
            "title": f"Journal: {' '.join(insight.split()[:6])}"[:80],
            "angle": (f"Reflection insight ({e.get('trigger') or 'reflect'}): "
                      f"\"{insight}\" — develop it into our own post angle.")[:600],
            "fmt": "hook+insight", "source": SOURCE_BRAIN, "score": 6.0,
            "tokens": _tokens(insight),
        })
    return out


async def _study_reads(cfg: Config, x) -> list[dict]:
    """Path c — fresh X reads THROUGH the throttled client methods only.
    Results are upserted so they enrich future scan mining too."""
    posts: list[dict] = []
    for q in cfg.agent.evergreen_themes[:STUDY_READ_QUERIES]:
        res = await x.search(q, limit=STUDY_READ_LIMIT)
        for p in res:
            db.upsert_post(p)
            posts.append(p)
    return posts


def _evergreen_ideas(cfg: Config) -> list[dict]:
    """Config themes × brain strategy statements → durable angles. Strategy
    statements are the bullet lines reflect() appends to the experiment log —
    prose intros and "(none" placeholders are not statements."""
    try:
        strat_lines = [
            s[2:].strip()
            for s in (ln.strip() for ln in brain_mod.read("strategies").splitlines())
            if s.startswith("- ") and not s[2:].strip().startswith("(none")
        ][:EVERGREEN_STRATEGIES]
    except Exception:  # noqa: BLE001 — no strategies file → no synthesis
        strat_lines = []
    out = []
    for theme in cfg.agent.evergreen_themes:
        for strat in strat_lines:
            out.append({
                "title": f"{theme} × {' '.join(strat.split()[:4])}"[:80],
                "angle": (f"Working thesis to apply: \"{strat}\" — take it to the "
                          f"theme '{theme}' with a concrete personal example.")[:600],
                "fmt": "one-liner", "source": SOURCE_EVERGREEN, "score": 5.0,
                "tokens": _tokens(f"{theme} {strat}"),
            })
    return out


async def replenish(cfg: Config, min_bank: int = DEFAULT_MIN_BANK,
                    batch: int = DEFAULT_BATCH, x=None) -> dict:
    """Fill the bank back toward `min_bank` when it dips below. Runs the
    source chain in priority order (a→d), deduping against the bank and
    within the batch by token containment. Returns
    {ran, added, sources, bank, bank_before}. Never raises on source failure."""
    bank_before = db.idea_count()
    if bank_before >= min_bank:
        return {"ran": False, "added": 0, "sources": [],
                "bank": bank_before, "bank_before": bank_before}

    known = _bank_tokens()
    added: list[dict] = []
    sources: list[str] = []

    def _accept(idea: dict) -> None:
        if not _novel(idea["tokens"], known):
            return
        known.append(idea["tokens"])
        added.append(idea)
        if idea["source"] not in sources:
            sources.append(idea["source"])

    def _drain(ideas: list[dict]) -> None:
        for idea in ideas:
            if len(added) >= batch:
                return
            _accept(idea)

    _drain(_outlier_ideas())                       # a) unmined scan outliers
    _drain(_journal_ideas())                       # b) journal insights
    if len(added) < batch // 2 and x is not None:  # c) fresh study reads
        try:
            fresh = await _study_reads(cfg, x)
        except Exception as e:  # noqa: BLE001 — reads failing must not kill replenish
            db.log("ideas", f"study read failed: {e}", level="warn")
            fresh = []
        _drain([i for i in (_post_idea(p, SOURCE_STUDY) for p in fresh) if i])
    _drain(_evergreen_ideas(cfg))                  # d) evergreen synthesis

    for idea in added:
        db.add_idea(idea["title"], idea["angle"], idea["fmt"], idea["source"],
                    idea["score"])
    if added:
        db.set_acct_setting("ideas_last_replenish", {
            "at": datetime.now().isoformat(timespec="seconds"),
            "added": len(added), "sources": sources})
        db.log("ideas", f"replenished +{len(added)} from {', '.join(sources)} "
                        f"(bank {bank_before} → {db.idea_count()})")
    return {"ran": True, "added": len(added), "sources": sources,
            "bank": db.idea_count(), "bank_before": bank_before}


def bank_health() -> dict:
    """Count + last replenish record — feeds the Ideas page health chip."""
    return {"count": db.idea_count(),
            "last": db.get_acct_setting("ideas_last_replenish") or {}}
