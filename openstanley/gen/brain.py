"""The Brain — OpenStanley's self-maintained memory, written BY OpenStanley itself.

data/brain/ holds plain, git-friendly markdown files:
  instructions.md  — OpenStanley's own operating manual (persona, workflow, priorities)
  rules.md         — numbered learned DO/DON'T rules (source + date, retireable)
  strategies.md    — what's working: theses + experiment log with outcomes
  files/           — self-written reference docs (niche-map, personas, pillars…)
  photos/          — media library: images + sidecar .md notes (caption/context)
  journal.md       — append-only reflection log: what changed and WHY

`brain_context()` renders a token-budgeted digest that is prepended to every
generation prompt — this is what makes the agent improve over time instead of
starting fresh each session. `reflect(trigger)` is the self-improvement step:
an LLM reviews recent chats/metrics/scan output and proposes structured edits
which are applied deterministically and journaled.
"""
from __future__ import annotations

import contextvars
import re
import secrets as _secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core import db
from .llm import chat as llm_chat, extract_json, LLMError

ROOT = Path(__file__).resolve().parent.parent.parent
BRAIN_DIR = ROOT / "data" / "brain"
FILES_DIR = BRAIN_DIR / "files"
PHOTOS_DIR = BRAIN_DIR / "photos"

BUDGET_CHARS = 1500  # hard cap for brain_context()

# ---------- security: never let secrets into the brain ----------

# credential-ish name followed by a real value (≥6 chars). The name pattern
# must also match INSIDE XOPENSTANLEY_LLM_API_KEY=… (no \b before "API" there).
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|password|passwd|bearer|"
    r"access[_-]?token|access[_-]?secret|auth[_-]?token|cookies|ct0|"
    r"credential)[a-z0-9_]*\s*[=:]\s*\S{6,}",
)
_ENV_ASSIGN_RE = re.compile(r"(?im)^\s*[A-Z][A-Z0-9_]*\s*=\s*\S{8,}\s*$")
_LONG_TOKEN_RE = re.compile(r"\b(?:sk|gx|zk|xox)[a-zA-Z0-9_\-]{20,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.")
_HEXISH_RE = re.compile(r"\b[a-fA-F0-9]{40,}\b")
_B64ISH_RE = re.compile(r"\b[A-Za-z0-9+/]{43,}={0,2}\b")


class BrainSecurityError(ValueError):
    """Raised when content looks like it contains a secret — never stored."""


def looks_secretly(content: str) -> Optional[str]:
    """Return a reason string if content matches secret-like patterns."""
    if not content:
        return None
    if _SECRET_KEY_RE.search(content):
        return "key/secret assignment pattern"
    if _ENV_ASSIGN_RE.search(content):
        return "XOPENSTANLEY_* env assignment"
    if _LONG_TOKEN_RE.search(content):
        return "provider key prefix"
    if _JWT_RE.search(content):
        return "JWT"
    if _HEXISH_RE.search(content):
        return "40+ char hex token"
    if _B64ISH_RE.search(content):
        return "base64-sized token"
    return None


def sanitize(content: str) -> str:
    """Validate content for writing into the brain. Raises BrainSecurityError."""
    reason = looks_secretly(content)
    if reason:
        raise BrainSecurityError(
            f"refusing to write secret-like content into the brain ({reason}) — "
            "credentials live in .env only"
        )
    return content


# ---------- structure ----------

MD_PARTS = ("instructions", "rules", "strategies", "journal")
SEED_FILES = ("niche-map", "audience-personas", "content-pillars",
              "voice-cards", "best-times", "competitor-notes")

SEED_INSTRUCTIONS = """# OpenStanley — Operating Manual (self-maintained)

I am OpenStanley, the account owner's AI Head of Content. This file is MY own
operating manual: I rewrite it as I learn how to serve this account better.

## Persona
- A sharp colleague, not a chatbot: proactive, opinionated, brief.
- I talk like the account owner talks — their voice, not mine.

## Workflow
1. Study the niche → fill the idea bank.
2. Draft in the owner's measured voice (style profile + rubric).
3. Everything lands in the approval queue — the owner approves, then it ships.
4. Learn weekly: what over/under-performed feeds back into rules + strategies.

## Priorities
1. Protect the account: no spam, no bait, no off-topic drift.
2. Grow real conversations (replies > likes on the X ranking model).
3. Bilingual respect: Arabic posts follow Arabic conventions (؟ ، ؛).

## Learned adjustments
(appended by reflect() — each entry says what changed and why)
"""

SEED_RULES = """# Learned Rules

Numbered DO/DON'T rules. Each rule carries its source (chat | learn | scan)
and the date it was learned. Retired rules stay listed but struck through.

<!-- no rules learned yet — reflect() will add them -->
"""

SEED_STRATEGIES = """# Growth Strategies

What's working, posting theses, and the experiment log with outcomes.

## Working theses
- (none yet — the learn loop will fill these from real metrics)

## Experiment log
- (none yet)
"""

SEED_JOURNAL = """# Reflection Journal

Append-only. Every reflection, user edit, and applied change — with WHY.
"""

FILE_STUB = """# {title}

(OpenStanley writes this file itself after deep scans and learn loops.)
"""


def ensure() -> None:
    """Create data/brain/ with seeded defaults on first run. Idempotent."""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    seeds = {
        "instructions.md": SEED_INSTRUCTIONS,
        "rules.md": SEED_RULES,
        "strategies.md": SEED_STRATEGIES,
        "journal.md": SEED_JOURNAL,
    }
    for name, content in seeds.items():
        p = BRAIN_DIR / name
        if not p.exists():
            _atomic_write(p, content)
    for stem in SEED_FILES:
        p = FILES_DIR / f"{stem}.md"
        if not p.exists():
            _atomic_write(p, FILE_STUB.format(title=stem.replace("-", " ").title()))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{_secrets.token_hex(3)}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- part addressing ----------

def _resolve(part: str) -> Path:
    """Map a part name to a safe path inside BRAIN_DIR. Raises on traversal."""
    part = (part or "").strip().strip("/")
    if not part or ".." in part or part.startswith("."):
        raise FileNotFoundError(f"unknown brain part {part!r}")
    if part in MD_PARTS:
        return BRAIN_DIR / f"{part}.md"
    m = re.fullmatch(r"files/([A-Za-z0-9_\-]+)", part)
    if m:
        return FILES_DIR / f"{m.group(1)}.md"
    if part == "photos":
        return PHOTOS_DIR
    raise FileNotFoundError(f"unknown brain part {part!r}")


def read(part: str) -> str:
    p = _resolve(part)
    if p.is_dir():
        raise IsADirectoryError("photos part is a directory — use list_photos()")
    if not p.exists():
        ensure()
    return p.read_text(encoding="utf-8")


def write(part: str, content: str) -> None:
    """Sanitized, atomic write of a brain part."""
    p = _resolve(part)
    if p.is_dir():
        raise IsADirectoryError("photos part is a directory")
    _atomic_write(p, sanitize(content))


# ---------- inventory (GET /api/brain) ----------

def _summary(part: str, text: str) -> str:
    if part == "rules":
        rules = parse_rules(text)
        active = sum(1 for r in rules if r["status"] == "active")
        return f"{active} active rules ({len(rules)} total)"
    if part == "journal":
        entries = parse_journal(text)
        return f"{len(entries)} entries"
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:90]
    return "(empty)"


def inventory() -> list[dict]:
    """[{name, type, size, modified, summary}] for every brain part."""
    ensure()
    parts: list[dict] = []
    order = ("instructions", "rules", "strategies") + \
        tuple(f"files/{s}" for s in sorted(SEED_FILES)) + ("journal",)
    for part in order:
        p = _resolve(part)
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        parts.append({
            "name": part, "type": "md", "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime)
                        .isoformat(timespec="seconds"),
            "summary": _summary(part, text),
        })
    photos = list_photos()
    parts.append({
        "name": "photos", "type": "photos",
        "size": sum(ph["size"] for ph in photos),
        "modified": max((ph["modified"] for ph in photos), default=None),
        "summary": f"{len(photos)} photos",
    })
    return parts


# ---------- rules.md parsing ----------

RULE_HEADER_RE = re.compile(
    r"^##\s+\[R(\d+)\]\s*\((\w+)\s*[·-]\s*([\d\-]+)\s*[·-]\s*(active|retired)\)\s*$"
)


def parse_rules(text: str) -> list[dict]:
    """Parse numbered rules out of rules.md."""
    rules: list[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        m = RULE_HEADER_RE.match(line.strip())
        if m:
            if current:
                rules.append(current)
            current = {"id": int(m.group(1)), "source": m.group(2),
                       "date": m.group(3), "status": m.group(4), "text": ""}
        elif current is not None:
            if line.strip():
                current["text"] = (current["text"] + " " + line.strip()).strip()
            elif current["text"]:
                rules.append(current)
                current = None
    if current:
        rules.append(current)
    for r in rules:
        r["text"] = r["text"].strip()
    return rules


def render_rule(rule: dict) -> str:
    return (f"## [R{rule['id']}] ({rule['source']} · {rule['date']} · "
            f"{rule['status']})\n{rule['text']}\n")


def add_rule(text: str, source: str) -> int:
    """Append a new numbered rule. Returns its id. Sanitized."""
    text = sanitize(text.strip())
    if not text:
        raise ValueError("empty rule")
    raw = read("rules")
    rules = parse_rules(raw)
    next_id = max((r["id"] for r in rules), default=0) + 1
    date = _now()[:10]
    block = render_rule({"id": next_id, "source": source, "date": date,
                         "status": "active", "text": text})
    # drop a placeholder comment when writing the first real rule
    raw = re.sub(r"<!--[^>]*no rules learned yet[^>]*-->\s*", "", raw)
    _atomic_write(_resolve("rules"), raw.rstrip() + "\n\n" + block)
    return next_id


def retire_rule(rule_id: int) -> bool:
    """Flip a rule to retired (kept, struck through in the UI)."""
    raw = read("rules")
    rules = parse_rules(raw)
    hit = next((r for r in rules if r["id"] == rule_id), None)
    if not hit or hit["status"] == "retired":
        return False
    old = render_rule(hit)
    hit["status"] = "retired"
    new = render_rule(hit)
    _atomic_write(_resolve("rules"), raw.replace(old, new, 1))
    return True


# ---------- journal ----------

JOURNAL_HEADER_RE = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\s*[·-]\s*(.+?)\s*$"
)


def journal_append(trigger: str, body: str, changes: Optional[list[str]] = None) -> None:
    """Append one dated entry. `trigger` is reflect:chat / user-edit / …"""
    body = sanitize(body.strip() or "(no notes)")
    lines = [f"## {_now()[:10]} {_now()[11:16]} · {trigger}", body]
    for ch in changes or []:
        ch = str(ch).strip()
        if ch:
            lines.append(f"- {ch}")
    p = _resolve("journal")
    ensure()
    prev = p.read_text(encoding="utf-8")
    _atomic_write(p, prev.rstrip() + "\n\n" + "\n".join(lines) + "\n")


def parse_journal(text: str) -> list[dict]:
    """Journal → newest-first list of {date, time, trigger, body, changes[]}."""
    entries: list[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        m = JOURNAL_HEADER_RE.match(line.strip())
        if m:
            if current:
                entries.append(current)
            current = {"date": m.group(1), "time": m.group(2),
                       "trigger": m.group(3).strip(), "body": [], "changes": []}
        elif current is not None:
            s = line.strip()
            if s.startswith("- "):
                current["changes"].append(s[2:])
            elif s:
                current["body"].append(s)
    if current:
        entries.append(current)
    out = []
    for e in entries:
        e["body"] = "\n".join(e["body"]).strip()
        out.append(e)
    out.reverse()  # newest first
    return out


# ---------- brain_context() — the prompt injection ----------

BRAIN_HEADER = "=== OPENSTANLEY'S BRAIN (self-maintained memory — obey these) ==="

# A/B toggle for the harness: when set (in the current thread's context),
# brain_context() returns "" — proving what the brain actually adds.
_context_off: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "openstanley_brain_off", default=False)


def set_brain_enabled(enabled: bool) -> contextvars.Token:
    """Toggle brain_context() injection for THIS thread (harness A/B mode).

    Returns a token — call .reset(token) to restore. ContextVar keeps the
    toggle thread-local, so a no-brain eval never leaks into live traffic.
    """
    return _context_off.set(not enabled)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def brain_context(budget: int = BUDGET_CHARS) -> str:
    """Compact digest for prompts: instructions + active rules + top strategies
    + pillar summaries, hard-capped at `budget` chars."""
    if _context_off.get():
        return ""
    ensure()
    # fair-share budgets scaled to the total
    shares = (500, 420, 320, 260)  # instructions, rules, strategies, pillars
    blocks: list[str] = []

    instr = read("instructions")
    # skip the manual's title; keep persona/workflow/priorities essence
    instr_body = "\n".join(ln for ln in instr.splitlines()
                           if not ln.startswith("# "))
    blocks.append(_clip(instr_body, shares[0]))

    rules = [r for r in parse_rules(read("rules")) if r["status"] == "active"]
    if rules:
        rule_lines = [f"R{r['id']}: {r['text']}" for r in rules]
        blocks.append(_clip("RULES (learned — obey):\n" + "\n".join(rule_lines),
                            shares[1]))

    strat = read("strategies")
    # keep "Working theses" section essence + last experiment lines
    strat_lines = [ln for ln in strat.splitlines()
                   if ln.strip() and not ln.startswith("#")][:12]
    if any(l.strip() and not l.startswith("- (none") for l in strat_lines):
        blocks.append(_clip("STRATEGIES:\n" + "\n".join(strat_lines), shares[2]))

    pillars_path = FILES_DIR / "content-pillars.md"
    if pillars_path.exists():
        pillars = [ln for ln in pillars_path.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.startswith("#") and "(OpenStanley writes"
                   not in ln][:6]
        if pillars:
            blocks.append(_clip("CONTENT PILLARS:\n" + "\n".join(pillars), shares[3]))

    out = BRAIN_HEADER + "\n" + "\n\n".join(b for b in blocks if b.strip())
    return out[:budget]


# ---------- reflect() — the self-improvement step ----------

REFLECT_SYSTEM = """You are OpenStanley reflecting on your own operation as an AI Head
of Content. Review the material below and propose STRUCTURED edits to your
long-term memory. Be conservative: only propose a change when the evidence
clearly supports it. Return STRICT JSON:

{"instructions_delta": "1-3 sentences to append under 'Learned adjustments' in the operating manual, or empty string",
 "new_rules": [{"text": "one concrete DO/DON'T rule, <=140 chars", "why": "evidence"}],
 "retire_rule_ids": [7, 12],
 "strategy_updates": [{"title": "thesis or experiment name", "note": "what the data says now", "outcome": "working|failing|mixed|new"}],
 "file_updates": [{"file": "niche-map|audience-personas|content-pillars|voice-cards|best-times|competitor-notes", "content": "full markdown body for that reference doc, derived from the material"}],
 "journal_entry": "1-3 sentences: what you learned and why it matters"}

Rules for rules: never propose secrets, never propose posting without approval,
never propose anything that breaks the user's voice. retire_rule_ids must
reference rules shown in the context below. file_updates are for reference
docs only when the material clearly supports a rewrite."""


def _material_chat() -> str:
    """Recent chat messages since the last chat reflection."""
    last_id = int(db.get_setting("brain_last_chat_id", 0) or 0)
    hist = db.chat_history(limit=40)
    fresh = [h for h in hist if h["id"] > last_id] or hist[-20:]
    if not fresh:
        return "(no chat yet)"
    return "\n".join(f"{h['role'].upper()}: {h['content'][:300]}" for h in fresh[-25:])


def _material_learn() -> str:
    """Post performance: what over/under-performed vs the account baseline."""
    posts = db.own_posts(limit=60)
    if not posts:
        return "(no posts with metrics yet)"
    rates = [p["engagement"] or 0 for p in posts]
    baseline = sum(rates) / max(1, len(rates))
    top = sorted(posts, key=lambda p: p["engagement"] or 0, reverse=True)[:5]
    flop = sorted(posts, key=lambda p: p["engagement"] or 0)[:5]
    fmt = lambda p: f"- [{p['likes']}♥ {p['replies']}💬 eng {p['engagement']:.3f}] {(p['text'] or '')[:110]}"  # noqa: E731
    return (f"BASELINE engagement: {baseline:.3f}\n\nOVER-PERFORMED:\n"
            + "\n".join(fmt(p) for p in top)
            + "\n\nUNDER-PERFORMED:\n" + "\n".join(fmt(p) for p in flop))


def _material_metrics() -> str:
    """Real performance numbers for reflect('metrics') when no material is
    passed in (refresh_metrics normally supplies its fresher summary)."""
    from .metrics import engagement_rate, latest_followers
    posts = db.own_posts(limit=60)
    if not posts:
        return "(no posts with metrics yet)"
    followers = latest_followers()
    rates = [engagement_rate(p.get("likes", 0), p.get("reposts", 0),
                             p.get("replies", 0), followers) for p in posts]
    baseline = sum(rates) / max(1, len(rates))
    top = sorted(posts, key=lambda p: engagement_rate(
        p.get("likes", 0), p.get("reposts", 0), p.get("replies", 0),
        followers), reverse=True)[:5]
    fmt = lambda p: f"- [{p['likes']}♥ {p['replies']}💬 rate {engagement_rate(p.get('likes', 0), p.get('reposts', 0), p.get('replies', 0), followers):.4f}] {(p['text'] or '')[:110]}"  # noqa: E731
    return (f"REAL METRICS — followers {followers}, baseline engagement rate "
            f"{baseline:.4f} (follower-normalized), {len(posts)} recent posts.\n\n"
            "TOP PERFORMERS:\n" + "\n".join(fmt(p) for p in top))


def _material_scan() -> str:
    profile = db.get_setting("style_profile") or {}
    if not profile:
        return "(no style profile yet — this was a first scan)"
    s = profile.get("stats") or {}
    out = (f"STYLE PROFILE (updated {profile.get('updated_at')}):\n"
           f"posts scanned {s.get('posts_scanned')}, avg len "
           f"{s.get('avg_length_chars')}, emoji/post "
           f"{(s.get('emoji') or {}).get('per_post')}, languages "
           f"{s.get('language_mix')}, best hours "
           f"{(s.get('posting_times') or {}).get('best_hours')}\n"
           f"top topics/terms: {(s.get('vocabulary') or {}).get('top_terms')}\n"
           f"summary: {(profile.get('human_summary') or '')[:400]}")
    # top + niche performers give the LLM real niche evidence to map
    top = sorted(db.own_posts(30), key=lambda p: p["engagement"] or 0,
                 reverse=True)[:8]
    if top:
        out += "\n\nTOP OWN POSTS (engagement):\n" + "\n".join(
            f"- [{p['likes']}♥ {p['replies']}💬] {(p['text'] or '')[:110]}" for p in top)
    niche = sorted(db.niche_posts(30),
                   key=lambda p: p.get("engagement") or 0, reverse=True)[:8]
    if niche:
        out += "\n\nTOP NICHE POSTS (what the niche rewards):\n" + "\n".join(
            f"- [{p.get('likes', 0)}♥ {p.get('replies', 0)}💬] {(p.get('text') or '')[:110]}"
            for p in niche)
    return out


MATERIALS = {"chat": _material_chat, "learn": _material_learn,
             "scan": _material_scan, "metrics": _material_metrics}


def _scan_fallback_files(stats: dict, profile: dict) -> list[tuple[str, str]]:
    """Deterministic niche-map / audience-personas bodies derived from scan
    stats — used when reflect("scan")'s LLM pass proposes no file_updates, so
    the brain reference docs always absorb the scan."""
    date = _now()[:10]
    topics = (stats.get("vocabulary") or {}).get("top_terms") or []
    mix = stats.get("language_mix") or {}
    hours = (stats.get("posting_times") or {}).get("best_hours") or []
    lang_txt = ", ".join(f"{k} {int(v * 100)}%" for k, v in mix.items() if v) or "n/a"
    niche = [f"# Niche Map (scan {date})", "",
             f"- posts scanned: {stats.get('posts_scanned')}",
             f"- recurring topics: {', '.join(topics[:10]) or '(none detected)'}",
             f"- language mix: {lang_txt}",
             f"- best posting hours: {', '.join(str(h) for h in hours) or 'n/a'}",
             f"- avg post length: {stats.get('avg_length_chars')} chars", "",
             "Refined by reflect(scan) / strategy one-pager as evidence accumulates."]
    personas = [f"# Audience Personas (scan {date})", "",
                "- signals below are scan-derived; the strategy one-pager refines them", "",
                f"- engages with: {', '.join(topics[:8]) or 'topics being learned'}",
                f"- most active around hours: {', '.join(str(h) + ':00' for h in hours) or 'n/a'}",
                f"- languages spoken (by content mix): {lang_txt}"]
    return [("niche-map", "\n".join(niche) + "\n"),
            ("audience-personas", "\n".join(personas) + "\n")]


def reflect(cfg, trigger: str, payload: Optional[dict] = None) -> dict:
    """Run one reflection: LLM proposes edits → applied deterministically.

    Returns {"ok", "applied": {added_rules, retired_rules, strategy_updates,
    instructions_updated}, "journal_entry"}. Raises LLMError only when the
    LLM itself fails (callers treat it as best-effort).
    """
    ensure()
    if trigger not in MATERIALS:
        raise ValueError(f"unknown reflect trigger {trigger!r}")
    payload = payload or {}
    material = payload.get("material") or MATERIALS[trigger]()
    current_rules = [r for r in parse_rules(read("rules")) if r["status"] == "active"]
    rules_ctx = "\n".join(f"R{r['id']}: {r['text']}" for r in current_rules) \
        or "(none yet)"
    user = (f"TRIGGER: {trigger}\n\nCURRENT ACTIVE RULES:\n{rules_ctx}\n\n"
            f"=== MATERIAL TO REVIEW ===\n{material}\n\n"
            "Propose your memory edits now (STRICT JSON).")

    raw = llm_chat(cfg.llm, system=REFLECT_SYSTEM, user=user,
                   temperature=0.4, json_mode=True)
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise LLMError(f"reflect returned non-object: {str(data)[:120]}")

    applied: dict = {"added_rules": [], "retired_rules": [],
                     "strategy_updates": [], "file_updates": [],
                     "instructions_updated": False, "dropped_tainted": 0}
    changes: list[str] = []

    # 1. instructions delta — appended under "Learned adjustments"
    delta = str(data.get("instructions_delta") or "").strip()
    if delta and delta.lower() not in ("none", "n/a", "-"):
        try:
            delta = sanitize(delta)
            p = _resolve("instructions")
            txt = p.read_text(encoding="utf-8")
            marker = "## Learned adjustments"
            entry = f"- {_now()[:10]} ({trigger}) — {delta}"
            if marker in txt:
                txt = txt.replace(marker, marker + "\n" + entry, 1)
            else:
                txt = txt.rstrip() + f"\n\n{marker}\n{entry}\n"
            _atomic_write(p, txt)
            applied["instructions_updated"] = True
            changes.append(f"instructions: {delta[:90]}")
        except BrainSecurityError:
            applied["dropped_tainted"] += 1

    # 2. new rules
    for nr in data.get("new_rules") or []:
        if not isinstance(nr, dict):
            continue
        text = str(nr.get("text") or "").strip()
        if not text:
            continue
        try:
            rid = add_rule(text, source=trigger)
            applied["added_rules"].append(rid)
            changes.append(f"added R{rid}: {text[:90]}")
        except BrainSecurityError:
            applied["dropped_tainted"] += 1
        except ValueError:
            pass

    # 3. retire rules (only existing, currently-active ones)
    for rid in data.get("retire_rule_ids") or []:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if retire_rule(rid):
            applied["retired_rules"].append(rid)
            changes.append(f"retired R{rid}")

    # 4. strategy updates — appended to the experiment log
    for su in data.get("strategy_updates") or []:
        if not isinstance(su, dict):
            continue
        title = str(su.get("title") or "").strip()
        if not title:
            continue
        note = str(su.get("note") or "").strip()
        outcome = str(su.get("outcome") or "new").strip()
        try:
            line = f"- {_now()[:10]} · {title} — {note} [{outcome}]"
            p = _resolve("strategies")
            txt = p.read_text(encoding="utf-8")
            marker = "## Experiment log"
            if marker in txt:
                txt = txt.replace(
                    marker, marker + "\n" + sanitize(line), 1)
            else:
                txt = txt.rstrip() + f"\n\n{marker}\n{line}\n"
            txt = txt.replace("- (none yet)\n", "", 1)
            _atomic_write(p, txt)
            applied["strategy_updates"].append(title[:80])
            changes.append(f"strategy: {title[:80]} [{outcome}]")
        except BrainSecurityError:
            applied["dropped_tainted"] += 1

    # 4.5 file updates — self-maintained reference docs (seed files only)
    for fu in data.get("file_updates") or []:
        if not isinstance(fu, dict):
            continue
        stem = str(fu.get("file") or "").strip()
        content = str(fu.get("content") or "").strip()
        if stem not in SEED_FILES or not content:
            continue
        try:
            _atomic_write(_resolve(f"files/{stem}"), sanitize(content))
            applied["file_updates"].append(stem)
            changes.append(f"file {stem}: {content.splitlines()[0][:80]}")
        except BrainSecurityError:
            applied["dropped_tainted"] += 1

    # 4.6 scan trigger guarantees the niche/persona docs reflect the scan
    # even when the LLM proposes no file_updates (deterministic, stats-derived)
    if trigger == "scan":
        profile = db.get_setting("style_profile") or {}
        stats = profile.get("stats") or {}
        if stats:
            done = set(applied["file_updates"])
            for stem, content in _scan_fallback_files(stats, profile):
                if stem not in done:
                    _atomic_write(_resolve(f"files/{stem}"), sanitize(content))
                    applied["file_updates"].append(stem)
                    changes.append(f"file {stem}: refreshed from scan stats")

    # 5. journal entry (always written — even an empty reflection is a fact)
    entry = str(data.get("journal_entry") or "").strip() or \
        f"reflected on {trigger}; no changes warranted"
    if payload.get("note"):
        entry = f"{payload['note']} — {entry}"
    journal_append(f"reflect:{trigger}", entry, changes)
    if applied["dropped_tainted"]:
        journal_append(f"reflect:{trigger}",
                       "dropped a proposed edit that looked secret-like "
                       "(never stored).")

    # 6. advance the chat watermark
    if trigger == "chat":
        hist = db.chat_history(limit=1)
        if hist:
            db.set_setting("brain_last_chat_id", hist[-1]["id"])
    db.log("brain", f"reflect({trigger}): "
                   f"+{len(applied['added_rules'])} rules, "
                   f"-{len(applied['retired_rules'])} retired, "
                   f"{len(applied['strategy_updates'])} strategies, "
                   f"{len(applied['file_updates'])} files")
    return {"ok": True, "trigger": trigger, "applied": applied,
            "journal_entry": entry}


# ---------- chat hook (every 10th message) ----------

CHAT_REFLECT_EVERY = 10


def maybe_reflect_chat_async(cfg) -> bool:
    """Count one chat message; on every 10th, reflect in a daemon thread.

    Called right after an assistant message is persisted. Never blocks the
    chat path; LLM failures are logged, not raised.
    """
    n = int(db.get_setting("brain_chat_counter", 0) or 0) + 1
    db.set_setting("brain_chat_counter", n)
    if n % CHAT_REFLECT_EVERY != 0:
        return False

    def _worker():
        try:
            reflect(cfg, "chat")
        except Exception as e:  # noqa: BLE001 — hook must never break chat
            db.log("brain", f"reflect(chat) failed: {e}", level="warn")

    threading.Thread(target=_worker, daemon=True,
                     name="brain-reflect-chat").start()
    return True


# ---------- photos ----------

PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PHOTO_SIDECAR_NOTE = """# {name}

- uploaded: {date}
- caption: {caption}
- note: captions come from the user or usage context only (OpenStanley has no vision)
{usage}"""


def save_photo(data: bytes, filename: str, caption: str = "",
               usage: str = "") -> dict:
    """Save an uploaded image + sidecar .md note. Returns the photo record."""
    sanitize(f"{filename} {caption} {usage}")  # captions too — no secrets
    ext = Path(filename).suffix.lower()
    if ext not in PHOTO_EXTS:
        raise ValueError(f"unsupported photo type {ext} (png/jpg/webp/gif)")
    ensure()
    stem = Path(filename).stem
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", stem)[:40] or "photo"
    name = f"{stem}_{_secrets.token_hex(4)}{ext}"
    (PHOTOS_DIR / name).write_bytes(data)
    _atomic_write(PHOTOS_DIR / f"{name}.md", PHOTO_SIDECAR_NOTE.format(
        name=name, date=_now(), caption=caption.strip() or "(none)",
        usage=f"- usage context: {usage.strip()}" if usage.strip() else ""))
    db.log("brain", f"photo saved: {name} ({len(data)} bytes)")
    return photo_record(name)


def photo_record(name: str) -> dict:
    p = PHOTOS_DIR / name
    sidecar = PHOTOS_DIR / f"{name}.md"
    caption = ""
    if sidecar.exists():
        m = re.search(r"(?m)^- caption:\s*(.*)$", sidecar.read_text(encoding="utf-8"))
        if m:
            caption = m.group(1).strip()
    return {"name": name, "size": p.stat().st_size if p.exists() else 0,
            "modified": datetime.fromtimestamp(p.stat().st_mtime)
                        .isoformat(timespec="seconds") if p.exists() else None,
            "caption": caption, "url": f"/api/brain/photos/{name}"}


def list_photos() -> list[dict]:
    ensure()
    return [photo_record(p.name) for p in sorted(PHOTOS_DIR.iterdir())
            if p.suffix.lower() in PHOTO_EXTS]


def photo_path(name: str) -> Path:
    """Safe path for serving a photo. Raises on traversal/unknown names."""
    if "/" in name or "\\" in name or ".." in name:
        raise FileNotFoundError("bad photo name")
    p = PHOTOS_DIR / name
    if p.suffix.lower() not in PHOTO_EXTS or not p.exists():
        raise FileNotFoundError(f"no such photo {name!r}")
    return p


# ---------- brain snapshot for evals (used by the harness A/B mode) ----------

def has_meaningful_brain() -> bool:
    """True once the brain carries learned content beyond the seed stubs."""
    ensure()
    rules = [r for r in parse_rules(read("rules")) if r["status"] == "active"]
    strat = read("strategies")
    real_strat = any(l.strip() and not l.startswith("#")
                     and "(none" not in l for l in strat.splitlines())
    return bool(rules) or real_strat


def to_dict() -> dict:
    """Whole brain as a dict (for tests + harness inspection)."""
    ensure()
    out = {p: read(p) for p in MD_PARTS}
    out["files"] = {f"{s}.md": read(f"files/{s}") for s in SEED_FILES}
    out["photos"] = list_photos()
    return out


def from_dict(snapshot: dict) -> None:
    """Restore brain files from a snapshot (tests only — sanitized)."""
    ensure()
    for k, v in snapshot.items():
        if k == "files":
            for fname, content in v.items():
                _atomic_write(FILES_DIR / Path(fname).name, sanitize(content))
        elif k == "photos":
            continue
        elif k in MD_PARTS:
            _atomic_write(_resolve(k), sanitize(v))
