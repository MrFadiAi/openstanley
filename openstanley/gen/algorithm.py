"""X algorithm scoring engine — "how will the ranking model treat this draft?"

Distilled from the open-sourced twitter/the-algorithm ranking stack (no torch,
pure rules). The heavy ranker there predicts, per candidate tweet:
reply-probability, retweet-probability, favorite-probability, negative-feedback
(report/block) probability, profile-visit probability, and dwell — then blends
them with heavily skewed weights (reply ≈ 27-75x a like, retweet ≈ 2x, negative
feedback a large negative). Candidate sourcing also runs simclusters interest
matching and author-author graph affinity.

We approximate those signal families with deterministic text features:

  the-algorithm signal          → local proxy
  ------------------------------ → -------------------------------------
  reply likelihood              → reply-invitation features (questions,
                                   challenges, fill-in-the-blanks)
  dwell time                    → specificity, length sweet spot, threads,
                                   media
  negative-feedback probability → bait/clickbait/hashtag/link/caps spam
  author-author graph affinity  → consistency with the topics the account
                                   already ranks for (its own corpus terms)
  simclusters interest match    → topic overlap with account's top terms
  quality filter                → language/script sanity (typos, mixed
                                   numerals, broken RTL)

score_draft() is pure: same inputs → same score. Pass `now_hour` to pin timing
in tests.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Optional

# --- lexicons (kept small + bilingual) -------------------------------------

BAIT_PATTERNS = [
    "rt if", "retweet if", "like and", "follow me", "follow for follow",
    "smash that", "comment below", "tag someone", "pls rt", "boost this",
    "شارك المنشور", "ريتويت اذا", "فولو مي", "لايك و",
]
CLICKBAIT = [
    "you won't believe", "this will shock", "number 7 will", "doctors hate",
    "one weird trick", "what happens next", "لن تصدق", "صدمة",
]
CHALLENGE_WORDS = [
    "unpopular", "hot take", "contrarian", "disagree", "prove me wrong",
    "change my mind", "أخالف", "رأي مخالف", "هل تتفق",
]
ASK_WORDS = [
    "what's your", "whats your", "what do you", "reply with", "tell me",
    "which one", "your take", "شو رأيك", "ما رأيك", "شاركني", "أخبرني",
]
FILLIN_PATTERNS = [r"\bis:?\s*$", r"is ___", r"fill in", r"\.\.\.:$",
                   r"أكمل الفراغ", r"الفراغ:"]
STORY_MARKERS = [
    "yesterday", "last week", "last month", "last year", "a year ago",
    "years ago", "today i", "we tried", "i built", "i shipped", "i learned",
    "امبارح", "الأسبوع الماضي", "اليوم", "بنيت", "تعلمت", "جربت",
]
BOLD_CLAIM = [
    "never", "always", "everyone", "nobody", "stop ", "secret", "truth is",
    "the trick", "most people", "أبداً", "دائماً", "الجميع", "الحقيقة",
]
WEAK_OPENERS = ["i think ", "maybe ", "in my opinion ", "arguably ", "اعتقد "]
DEFAULT_BEST_HOURS = {8, 9, 12, 13, 17, 18, 21}
DEAD_HOURS = {2, 3, 4, 5}

_AR = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
_AR_DIGIT = re.compile(r"[٠-٩]")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]"
)
_STOP = set("""a an the and or but if then of to in on for with at by from is are
was were be been i you he she it we they my your our this that these those as
it's i'm don't do does did not no so just very really""".split())


@dataclass
class Factor:
    name: str
    impact: int
    note: str


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w؀-ۿ']+", text.lower()) if w]


def _is_arabicish(text: str) -> bool:
    return len(_AR.findall(text)) > len(_LATIN.findall(text))


# --- individual signal scorers ----------------------------------------------

def _reply_invitation(text: str, words: list[str]) -> Factor:
    """the-algorithm's dominant signal: predicted reply probability."""
    impact, notes = 0, []
    stripped = text.rstrip()
    q = text.count("?") + text.count("؟")
    if q and (stripped.endswith("?") or stripped.endswith("؟")):
        impact += 10
        notes.append("ends with a question")
    elif q:
        impact += 6
        notes.append("question inside the post")
    low = text.lower()
    for w in ASK_WORDS:
        if w in low:
            impact += 6
            notes.append(f"explicit ask (\"{w}\")")
            break
    if any(re.search(p, low) for p in FILLIN_PATTERNS):
        impact += 6
        notes.append("fill-in-the-blank surface")
    if any(c in low for c in CHALLENGE_WORDS):
        impact += 5
        notes.append("contrarian challenge invites pushback")
    if "you" in words or "أنت" in text or "انت" in text:
        impact += 3
        notes.append("directly addresses the reader")
    if impact == 0:
        return Factor("Reply invitation", -4,
                      "closed statement — no reply surface")
    return Factor("Reply invitation", min(impact, 15), "; ".join(notes[:3]))


def _hook_strength(text: str) -> Factor:
    head = text[:60].lower()
    impact, notes = 0, []
    if _DIGIT.search(head):
        impact += 4
        notes.append("number in the first line")
    if any(w in head for w in BOLD_CLAIM):
        impact += 4
        notes.append("bold claim up front")
    if any(w in head for w in WEAK_OPENERS):
        impact -= 3
        notes.append("hedged opener blurs the hook")
    if not notes:
        impact += 1
        notes.append("neutral opener")
    return Factor("Hook strength", impact, "; ".join(notes))


def _specificity(text: str, words: list[str]) -> Factor:
    impact, notes = 0, []
    n = len(text)
    if _DIGIT.search(text):
        impact += 4
        notes.append("concrete numbers")
    if 100 <= n <= 240:
        impact += 4
        notes.append("length in the dwell sweet spot")
    elif n < 60:
        impact -= 3
        notes.append("too thin for dwell")
    elif n > 280:
        impact -= 2
        notes.append("runs long past truncation")
    if words and len(set(words)) / len(words) > 0.75:
        impact += 2
        notes.append("high word variety")
    if any(m in text.lower() for m in STORY_MARKERS):
        impact += 3
        notes.append("micro-story marker")
    return Factor("Specificity & dwell", impact, "; ".join(notes))


def _scannability(text: str) -> Factor:
    impact, notes = 0, []
    breaks = text.count("\n")
    if 1 <= breaks <= 3:
        impact += 4
        notes.append("clean line-break rhythm")
    elif breaks > 5:
        impact -= 2
        notes.append("fragmented into too many lines")
    sentences = [s for s in re.split(r"[.!?\n؟]+", text) if s.strip()]
    if sentences:
        avg_w = sum(len(_words(s)) for s in sentences) / len(sentences)
        if avg_w <= 25:
            impact += 2
            notes.append("short sentences")
    if breaks == 0 and len(text) > 200:
        impact -= 3
        notes.append("wall of text")
    if not notes:
        impact += 1
        notes.append("fine as-is")
    return Factor("Scannability", impact, "; ".join(notes))


def _spam_risk(text: str, words: list[str]) -> Factor:
    """Predicted negative-feedback probability → the ranker's big negative."""
    impact, notes = 0, []
    low = text.lower()
    for p in BAIT_PATTERNS:
        if p in low:
            impact -= 12
            notes.append(f"engagement bait (\"{p}\")")
            break
    for c in CLICKBAIT:
        if c in low:
            impact -= 10
            notes.append("clickbait phrasing")
            break
    hashtags = re.findall(r"#\w+", text)
    if len(hashtags) > 2:
        impact -= 6
        notes.append(f"{len(hashtags)} hashtags (spam-class signal)")
    elif hashtags:
        impact -= 2
        notes.append("1 hashtag (mild reach tax)")
    if "http://" in low or "https://" in low or "t.co" in low:
        impact -= 4
        notes.append("outbound link suppresses reach")
    caps_words = [w for w in words if len(w) > 3 and w.isupper()]
    if len(caps_words) > 2:
        impact -= 5
        notes.append("shouty caps")
    if len(_EMOJI.findall(text)) > 4:
        impact -= 3
        notes.append("emoji overload")
    if "fyp" in low or "viral" in low:
        impact -= 3
        notes.append("reach-begging language")
    if impact == 0:
        return Factor("Spam/negative-feedback risk", 0, "clean of bait signals")
    return Factor("Spam/negative-feedback risk", impact, "; ".join(notes))


def _topic_affinity(words: list[str], account_topics: list[str]) -> Factor:
    if not account_topics:
        return Factor("Topic affinity", 0, "no account corpus yet — neutral")
    topic_terms = {t.lower() for t in account_topics}
    hits = sum(1 for w in words if w in topic_terms)
    topic_phrases = [t for t in account_topics
                     if len(t.split()) > 1 and t.lower() in " ".join(words)]
    hits += 2 * len(topic_phrases)
    if hits >= 3:
        return Factor("Topic affinity", 8,
                      "squarely in the account's interest cluster")
    if hits >= 1:
        return Factor("Topic affinity", 4, "some overlap with known topics")
    return Factor("Topic affinity", -2,
                  "off the topics this account ranks for")


def _timing_fit(now_hour: Optional[int], best_hours: set[int]) -> Factor:
    if now_hour is None:
        return Factor("Timing fit", 0, "no scheduled time yet")
    if now_hour in best_hours:
        return Factor("Timing fit", 6, "inside a historically strong hour")
    for h in best_hours:  # adjacent hour still rides the wave
        if abs(h - now_hour) == 1:
            return Factor("Timing fit", 3, "adjacent to a strong hour")
    if now_hour in DEAD_HOURS:
        return Factor("Timing fit", -4, "dead-zone hour (2-5am)")
    return Factor("Timing fit", 0, "neutral hour")


def _language_quality(text: str) -> Factor:
    """Quality-filter proxy: script sanity, numerals, RTL punctuation."""
    from .lang import arabic_issues  # local import: lang has no deps on us
    impact, notes = 0, []
    issues = arabic_issues(text)
    if not issues:
        impact += 2
        notes.append("clean script + punctuation")
    else:
        impact -= min(6, 2 * len(issues))
        notes.extend(issues[:3])
    tokens = text.split()
    if any(len(t) > 28 for t in tokens):
        impact -= 2
        notes.append("unbroken 28+ char token (typo-ish)")
    return Factor("Language quality", impact, "; ".join(notes))


# --- main entry -------------------------------------------------------------

def score_draft(text: str, *, kind: str = "post", image: bool = False,
                is_thread: bool = False, now_hour: Optional[int] = None,
                scheduled_hour: Optional[int] = None,
                account_topics: Optional[list[str]] = None,
                best_hours: Optional[set[int]] = None) -> dict:
    """Score 0-100 with factor breakdown. Pure + deterministic for pinned args."""
    words = _words(text)
    factors: list[Factor] = [
        _reply_invitation(text, words),
        _hook_strength(text),
        _specificity(text, words),
        _scannability(text),
        _spam_risk(text, words),
        _topic_affinity(words, account_topics or []),
    ]

    if best_hours is None:
        from ..core import db
        profile = db.get_setting("style_profile") or {}
        bh = (profile.get("stats") or {}).get("posting_times", {}).get("best_hours")
        if bh:
            best_hours = set(bh)
        else:
            # real own-post performance beats the generic heuristic when we
            # have enough posts; otherwise fall back to DEFAULT_BEST_HOURS
            from .metrics import best_hours_for_scoring
            best_hours = best_hours_for_scoring() or set(DEFAULT_BEST_HOURS)
    factors.append(_timing_fit(
        scheduled_hour if scheduled_hour is not None else now_hour,
        best_hours))

    if image:
        factors.append(Factor("Media boost", 8, "attached image ~2x reach"))
    elif kind == "quote":
        factors.append(Factor("Media boost", 2, "quote post adds a reply surface"))
    else:
        factors.append(Factor("Media boost", 0, "text-only"))

    if is_thread or (kind == "post" and "\n\n" in text and len(text) > 400):
        factors.append(Factor("Thread potential", 4,
                              "multiple reply surfaces per tweet"))
    else:
        factors.append(Factor("Thread potential", 0, "single tweet"))

    factors.append(_language_quality(text))

    total = 50 + sum(f.impact for f in factors)
    score = max(0, min(100, total))
    grade = ("excellent" if score >= 80 else
             "good" if score >= 65 else
             "fair" if score >= 50 else "weak")
    factors.sort(key=lambda f: abs(f.impact), reverse=True)
    return {"score": score, "grade": grade,
            "factors": [asdict(f) for f in factors]}


def score_draft_row(d: dict) -> dict:
    """Score a draft dict (as returned by db.drafts_by_status)."""
    meta = d.get("meta") or {}
    sched_hour = None
    sched = d.get("scheduled_at")
    if sched and "T" in str(sched):
        try:
            sched_hour = int(str(sched)[11:13])
        except ValueError:
            pass
    topics = _account_topics()
    return score_draft(
        d.get("text") or "",
        kind=d.get("kind") or "post",
        image=bool(d.get("image") or meta.get("image")),
        is_thread=bool(d.get("thread")),
        scheduled_hour=sched_hour,
        account_topics=topics,
    )


def _account_topics() -> list[str]:
    from ..core import db
    profile = db.get_setting("style_profile") or {}
    topics = (profile.get("stats") or {}).get("topics") or []
    if topics:
        return topics
    # fall back to frequent own-post terms (cheap simclusters stand-in)
    from ..core import db as _db
    words: dict[str, int] = {}
    for p in _db.own_posts(80):
        for w in _words(p.get("text") or ""):
            if len(w) > 3 and w not in _STOP:
                words[w] = words.get(w, 0) + 1
    return sorted(words, key=words.get, reverse=True)[:30]


def grade_color(score: int) -> str:
    if score >= 80:
        return "green"
    if score >= 65:
        return "purple"
    if score >= 50:
        return "amber"
    return "red"


PROMPT_BLOCK = """ALGORITHM TARGETING (X ranking model — optimize for these):
- Replies dominate ranking weight (~27-75x a like): end with a real question,
  a challenge, or a fill-in-the-blank WHEN it fits the voice. Forced CTAs read
  as bait and trigger negative-feedback downranking — never write "RT if".
- Dwell time: concrete numbers, micro-stories, specifics beat aphorisms.
  Sweet spot 100-240 chars. 1-3 line breaks make it scannable.
- Negative feedback kills reach: no engagement bait, max 1 hashtag (0 better),
  no outbound links in the post, no caps-lock shouting, ≤3 emoji.
- Stay inside the account's topic cluster — the algorithm already routes your
  audience's interests; off-topic posts cold-start.
- Media (~2x reach) and threads (multiple reply surfaces) when the idea suits.
- Typos get classified as unknown language and shadow-filtered: spellcheck."""


def improvement_hints(alg: dict) -> list[str]:
    """Actionable notes for the hurting factors — fed into regeneration."""
    hurting = [f for f in alg.get("factors", []) if f["impact"] < 0]
    return [f"{f['name']}: {f['note']}" for f in hurting]
