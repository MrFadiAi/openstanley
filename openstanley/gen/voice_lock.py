"""Voice lock — persona consistency enforcement (v0.4.0).

The brain learned the voice (rubric, style scan) and drafts average ~77 on
voice fidelity — but 77 means 1-in-4 drafts still drift, and the account is
an AI-agent persona with deliberate quirks (lowercase, "speek"-style
misspellings, short choppy posts). A single polished/corporate-sounding
reply breaks character visibly. Nothing ENFORCED consistency before; this
module closes the loop: every draft is scored, fixed, or rejected BEFORE
it reaches the approval queue.

Two layers:

  deterministic   regex/heuristic checks derived from the brain's style
                 profile (data/brain/voice.md keys, written by the deep
                 scan): casing, length band per kind, banned corporate
                 phrases, emoji budget, exclamation stacking, hashtag
                 walls, stylized-misspelling density band. No LLM, no
                 network — this layer decides almost everything.

  LLM fix         only when the deterministic score is BORDERLINE
                 (55-80): one cheap, focused "rewrite in voice" call.
                 The rewrite is re-scored deterministically and wins
                 only if it actually scores better.

check_draft() returns a VoiceCheck; apply_voice_lock() is the pipeline
helper (enabled-gate + rejection logging + fixed-text swap). The human
approval gate downstream is untouched — the lock runs BEFORE approval,
never instead of it. A rejected draft just means the loop moves on: no
draft beats an off-voice draft.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core import db
from .lang import detect
from .llm import chat, extract_json, LLMError

ROOT = Path(__file__).resolve().parent.parent.parent


def voice_md_path(acct: int | None = None) -> Path:
    """voice.md lives inside the ACCOUNT's brain dir (v0.5.0) — written by
    that account's deep scan, read by that account's voice lock. Called at
    request time (never at import) — the DB may not exist yet during boot."""
    from . import brain as _brain
    return _brain.brain_dir(acct) / "voice.md"

# --- tuning constants --------------------------------------------------------

BORDERLINE_LO = 55          # deterministic scores in [55, 80] get one LLM fix
BORDERLINE_HI = 80
DEFAULT_THRESHOLD = 75      # [agent] voice_lock_threshold default

# corporate / polished-marketer phrasing the persona never uses
BANNED_PHRASES = (
    "delve", "game-changer", "game changer", "excited to share",
    "thrilled to announce", "in today's fast-paced world", "fast-paced world",
    "unlock the power", "harness the power", "revolutionize", "gamechanging",
    "elevate your", "seamless experience", "at the forefront of",
)

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")
_HASHTAG = re.compile(r"#\w+")
_EXCL_STACK = re.compile(r"!{2,}")
_KEY_LINE = re.compile(r"(?m)^\s*-?\s*([a-z_]+)\s*:\s*(.+?)\s*$")

# deliberate/stylized spellings ("speek"-style quirks). The DENSITY of these
# is a persona signal: a quirky account uses some, a corporate one uses none.
MISSPELLINGS = frozenset("""
teh speek smol smort heckin fren frens thru nite tho altho prolly kinda wanna
gonna gotta dunno yea yeh pls plz thx ty cuz cos becuase becos wut wat wen dat
dis dose dem dere sum sumthin somethin nothin everyting everytime abt nvm tho
tommorow tomorow recieve definately seperate occured untill wich whitch wierd
""".split())

MIN_WORDS_FOR_MISSPELL_CHECK = 12   # shorter texts carry no density signal


def count_misspellings(text: str) -> int:
    """Occurrences of stylized-misspelling lexicon words in `text`."""
    return sum(1 for w in re.findall(r"[A-Za-z']+", text.lower())
               if w in MISSPELLINGS)


# --- the check result --------------------------------------------------------

@dataclass
class VoiceCheck:
    """One draft scored against the persona rules."""
    score_0_100: int
    violations: list[str] = field(default_factory=list)
    passed: bool = False
    fixed_text: Optional[str] = None   # set when the LLM fix won
    threshold: int = DEFAULT_THRESHOLD

    def meta(self) -> dict:
        """JSON-serializable block for draft meta.voice (UI chip)."""
        m: dict = {"score": self.score_0_100, "checked": True}
        if self.fixed_text is not None:
            m["fixed"] = True
        if self.violations:
            m["violations"] = self.violations[:5]
        return m


# --- persona rules: data/brain/voice.md --------------------------------------

def _cap_neutral() -> int:
    """Neutral-rule post ceiling follows the account's real capability."""
    from ..core.safety import max_post_chars
    return max_post_chars()


def _neutral_rules() -> dict:
    """Built-in fallback when voice.md is missing (warn once)."""
    return {
        "source": "neutral",
        "lowercase_first": False,          # unknown → casing unchecked
        "bands": {"post": (15, _cap_neutral()), "reply": (5, 200)},
        "emoji_max": 2,
        "hashtags_max": 2,
        "misspelling_band": (0.0, 6.0),    # unknown → only extremes flagged
    }


_rules_cache: dict = {}          # {path_str: (mtime_ns, rules)}
_warned_missing = False


def _parse_voice_md(text: str) -> dict:
    """Parse `- key: value` / `key: value` lines into normalized rules.

    Unparseable values fall back to the neutral rule for that key, so a
    hand-mangled file degrades instead of crashing the pipeline.
    """
    rules = _neutral_rules()
    rules["source"] = "brain"
    raw: dict[str, str] = {}
    for m in _KEY_LINE.finditer(text):
        raw[m.group(1)] = m.group(2)

    def _bool(v: str, default: bool) -> bool:
        return v.strip().lower() in ("true", "yes", "1") if v.strip() else default

    def _band(v: str, default: tuple[int, int]) -> tuple[int, int]:
        try:
            lo, hi = (int(x) for x in v.split("-"))
            return (lo, hi) if 0 <= lo < hi else default
        except ValueError:
            return default

    rules["lowercase_first"] = _bool(raw.get("lowercase_first", ""),
                                     rules["lowercase_first"])
    rules["bands"]["post"] = _band(raw.get("length_band_post", ""),
                                   rules["bands"]["post"])
    rules["bands"]["reply"] = _band(raw.get("length_band_reply", ""),
                                    rules["bands"]["reply"])
    try:
        rules["emoji_max"] = max(0, int(raw.get("emoji_max", "").strip()
                                        or rules["emoji_max"]))
    except ValueError:
        pass
    try:
        rules["hashtags_max"] = max(0, int(raw.get("hashtags_max", "").strip()
                                           or rules["hashtags_max"]))
    except ValueError:
        pass
    mband = raw.get("misspelling_band", "")
    if mband:
        try:
            lo, hi = (float(x) for x in mband.split("-"))
            if 0 <= lo < hi:
                rules["misspelling_band"] = (lo, hi)
        except ValueError:
            pass
    return rules


def load_persona_rules(acct: int | None = None) -> dict:
    """Persona rules from the account's brain voice.md (mtime-cached);
    neutral fallback."""
    global _warned_missing
    path = voice_md_path(acct)
    key = str(path)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        if not _warned_missing:
            db.log("voice", "voice.md missing — voice lock running on neutral "
                            "rules (run a deep scan to derive persona keys)",
                   level="warn")
            _warned_missing = True
        return _neutral_rules()
    hit = _rules_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        rules = _parse_voice_md(path.read_text(encoding="utf-8"))
    except OSError:
        return _neutral_rules()
    _rules_cache[key] = (mtime, rules)
    return rules


def write_voice_md(stats: dict, acct: int | None = None) -> Optional[Path]:
    """Derive voice.md keys from scan stats (compute_stats output).

    Called by the deep scan so the lock's rules are always scan-derived.
    Hand edits are overwritten on the next scan — the brain owns this file.
    """
    if not stats:
        return None
    avg = max(20.0, float(stats.get("avg_length_chars") or 120))
    emoji = float((stats.get("emoji") or {}).get("per_post") or 0)
    hash_pct = float((stats.get("hashtags") or {}).get("pct_with") or 0)
    hash_pp = float((stats.get("hashtags") or {}).get("per_post") or 0)
    lower_pct = float((stats.get("casing") or {}).get("pct_lowercase_start") or 0)
    m = float(stats.get("misspellings_per_100_words") or 0)

    # floor at 60% of avg: the account's own short tail, not half its voice.
    # Premium accounts may post long-form — the ceiling is the account's
    # real capability, not the free-account 280
    from ..core.safety import max_post_chars
    _cap = max_post_chars()
    p_lo, p_hi = max(15, round(avg * 0.6)), min(_cap, round(avg * 1.8))
    if p_hi <= p_lo:
        p_hi = min(_cap, p_lo + 40)
    r_lo, r_hi = max(5, round(avg * 0.2)), min(200, round(avg * 1.0))
    if r_hi <= r_lo:
        r_hi = min(200, r_lo + 30)
    emoji_max = 0 if emoji < 0.3 else math.ceil(emoji + 1)
    hash_max = 0 if hash_pct < 0.15 else math.ceil(hash_pp + 1)
    if m >= 0.5:
        m_lo, m_hi = max(0.0, round(m * 0.5, 1)), round(m * 2, 1) + 0.5
    else:
        m_lo, m_hi = 0.0, 1.5

    content = "\n".join([
        "# Voice — scan-derived keys",
        "",
        "<!-- auto-written by the deep scan; read by the voice lock.",
        "     hand-edits are overwritten on the next scan. -->",
        "",
        f"lowercase_first: {'true' if lower_pct > 0.6 else 'false'}",
        f"length_band_post: {p_lo}-{p_hi}",
        f"length_band_reply: {r_lo}-{r_hi}",
        f"emoji_max: {emoji_max}",
        f"hashtags_max: {hash_max}",
        f"misspelling_band: {m_lo}-{m_hi}",
        "",
    ])
    path = voice_md_path(acct)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _rules_cache.pop(str(path), None)
    return path


# --- config access -----------------------------------------------------------

def lock_threshold(cfg) -> int:
    """db setting wins (UI slider), else [agent] voice_lock_threshold."""
    v = db.get_setting("voice_lock_threshold")
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    return int(getattr(cfg.agent, "voice_lock_threshold", DEFAULT_THRESHOLD)
               or DEFAULT_THRESHOLD)


def lock_enabled(cfg) -> bool:
    v = db.get_setting("voice_lock_enabled")
    if v is not None:
        return bool(v)
    return bool(getattr(cfg.agent, "voice_lock_enabled", True))


# --- deterministic layer (pure) ----------------------------------------------

def _band_for(rules: dict, kind: str) -> tuple[int, int]:
    return rules["bands"]["reply"] if kind == "reply" else rules["bands"]["post"]


from ..core.text import scrub_ai_punctuation  # noqa: E402


def score_deterministic(text: str, kind: str = "post",
                        rules: Optional[dict] = None) -> tuple[int, list[str]]:
    """Score 0-100 against the persona rules. No LLM, no I/O, no clock."""
    rules = rules or load_persona_rules()
    t = (text or "").strip()
    if not t:
        return 0, ["empty draft"]

    score = 100.0
    violations: list[str] = []

    def hit(penalty: float, msg: str) -> None:
        nonlocal score
        score -= penalty
        violations.append(msg)

    # RTL guard (user rule 2026-08-25, deterministic): X renders
    # Arabic-dominant posts that OPEN with a Latin word broken — the first
    # word must be Arabic. English tech terms stay inside the sentence.
    arabic_ratio = sum(1 for ch in t if "؀" <= ch <= "ۿ") / max(len(t), 1)
    first_word = t.split()[0] if t.split() else ""
    if arabic_ratio > 0.2 and first_word and first_word.isascii() and first_word[:1].isalpha():
        hit(30, f"opens with English word '{first_word[:14]}' on an Arabic-dominant "
                "account — X breaks RTL rendering; the first word must be Arabic")

    low = t.lower()
    for phrase in [p for p in BANNED_PHRASES if p in low][:2]:  # cap at 2 hits
        hit(30, f"corporate phrase: '{phrase}'")

    if rules["lowercase_first"] and t[:1].isupper():
        hit(12, "starts uppercase — persona writes lowercase")

    over = len(_EMOJI.findall(t)) - rules["emoji_max"]
    if over > 0:
        hit(min(30, 15 * over),
            f"{len(_EMOJI.findall(t))} emoji — persona allows {rules['emoji_max']}")

    if _EXCL_STACK.search(t):
        hit(12, "exclamation stacking ('!!')")

    tags = _HASHTAG.findall(t)
    if len(tags) >= 3:
        hit(15, f"hashtag wall ({len(tags)} tags)")
    elif rules["hashtags_max"] == 0 and tags:
        hit(15, "hashtags — persona never uses them")
    elif len(tags) > rules["hashtags_max"]:
        hit(15, f"{len(tags)} hashtags — persona allows {rules['hashtags_max']}")

    lo, hi = _band_for(rules, kind)
    if len(t) < lo or len(t) > hi:
        hit(15, f"length {len(t)} outside persona band {lo}-{hi}")

    # stylized-misspelling density: Latin signal only, needs a real sample
    if detect(t) == "en":
        words = re.findall(r"[A-Za-z']+", t)
        if len(words) >= MIN_WORDS_FOR_MISSPELL_CHECK:
            m_lo, m_hi = rules["misspelling_band"]
            d = count_misspellings(t) / len(words) * 100
            if d < m_lo:
                hit(min(25, 12 + (m_lo - d) * 4),
                    f"too polished — {d:.1f} stylized spellings/100w, "
                    f"persona band {m_lo}-{m_hi}")
            elif d > m_hi:
                hit(min(25, 12 + (d - m_hi) * 6),
                    f"misspelling overload — {d:.1f}/100w, "
                    f"persona band {m_lo}-{m_hi}")

    caps = [w for w in re.findall(r"[A-Za-z]{4,}", t) if w.isupper()]
    if len(caps) > 2:
        hit(8, "shouting in caps")

    return int(max(0, round(score))), violations


# --- LLM fix layer (borderline only) -----------------------------------------

FIX_SYSTEM = """You are a strict line editor enforcing ONE rule: match the
persona voice exactly. Rewrite the DRAFT so it follows every VOICE RULE.
Keep the same meaning, the same language, and roughly the same length
(stay inside the length band). Fix ONLY the style problems listed.
Return STRICT JSON: {"text": "..."}"""


def _rules_block(rules: dict, kind: str) -> str:
    lo, hi = _band_for(rules, kind)
    m_lo, m_hi = rules["misspelling_band"]
    return "\n".join([
        f"- start lowercase: {'yes' if rules['lowercase_first'] else 'no preference'}",
        f"- length band (chars): {lo}-{hi}",
        f"- emoji: at most {rules['emoji_max']}",
        f"- hashtags: at most {rules['hashtags_max']}",
        f"- stylized misspellings per 100 words: {m_lo}-{m_hi}",
        "- never use corporate phrasing like: " + ", ".join(BANNED_PHRASES[:6]),
    ])


def _llm_rewrite(cfg, text: str, kind: str, rules: dict,
                 violations: list[str]) -> Optional[str]:
    user = (f"VOICE RULES:\n{_rules_block(rules, kind)}\n\n"
            f"STYLE PROBLEMS IN THE DRAFT:\n"
            + "\n".join(f"- {v}" for v in violations)
            + f"\n\nDRAFT:\n{text}\n\nRewrite it now.")
    raw = chat(cfg.llm, FIX_SYSTEM, user, temperature=0.4, json_mode=True)
    out = str(extract_json(raw).get("text", "")).strip()
    return out or None


# --- main entry points -------------------------------------------------------

def check_draft(cfg, text: str, kind: str = "post",
                allow_fix: bool = True) -> VoiceCheck:
    """Score one draft 0-100; borderline drafts get one LLM rewrite attempt.

    Deterministic first; if the score lands in [55, 80] and allow_fix, one
    focused "rewrite in voice" call produces a candidate which is re-scored
    deterministically — it wins only if it scores strictly better.
    `passed = score >= threshold` (config [agent] voice_lock_threshold).
    """
    text = scrub_ai_punctuation(text)
    rules = load_persona_rules()
    threshold = lock_threshold(cfg)
    score, violations = score_deterministic(text, kind, rules)

    fixed_text: Optional[str] = None
    if allow_fix and BORDERLINE_LO <= score <= BORDERLINE_HI:
        try:
            cand = _llm_rewrite(cfg, text, kind, rules, violations)
        except LLMError:
            cand = None  # LLM down → the deterministic verdict stands
        if cand:
            fscore, fviol = score_deterministic(cand, kind, rules)
            if fscore > score:
                score, violations, fixed_text = fscore, fviol, cand

    return VoiceCheck(score_0_100=score, violations=violations,
                      passed=score >= threshold, fixed_text=fixed_text,
                      threshold=threshold)


def apply_voice_lock(cfg, text: str, kind: str = "post") \
        -> tuple[Optional[str], Optional[dict]]:
    """Pipeline gate: returns (text_to_store, voice_meta) — (None, None) when
    rejected. Disabled lock → (text, None); rejection is logged and the loop
    continues (no draft beats an off-voice draft).
    """
    if not lock_enabled(cfg):
        return text, None
    vc = check_draft(cfg, text, kind)
    if not vc.passed:
        db.log("voice", f"voice lock rejected draft (score {vc.score_0_100}, "
                        f"reasons: {'; '.join(vc.violations[:3])})", level="warn")
        return None, None
    return (vc.fixed_text or text), vc.meta()
