"""The Brain — OpenStanley's self-maintained memory, written BY OpenStanley itself.

Each ACCOUNT owns its whole brain under data/accounts/<id>/brain/ (v0.5.0):
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
which are applied deterministically and journaled. All functions take an
optional `acct` (default: the ACTIVE account) — one account's memory is never
visible to another's prompts.
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
# v0.5.0: brains are PER ACCOUNT — data/accounts/<id>/brain/. The anchor is
# ACCOUNTS_ROOT (tests swap it for a sandbox); account 1 is the migrated
# legacy install (its files moved from data/brain/ on first run).
ACCOUNTS_ROOT = ROOT / "data" / "accounts"
LEGACY_BRAIN_DIR = ROOT / "data" / "brain"

BUDGET_CHARS = 6000  # hard cap — the LLM config allows 20k tokens; starving
                     # the learned layers to 1.5k chars made the agent ignore
                     # its own memory (user report 2026-08-20)

# ---------- security: never let secrets into the brain ----------

# credential-ish name followed by a real value (≥6 chars). The name pattern
# must also match INSIDE OPENSTANLEY_LLM_API_KEY=… (no \b before "API" there).
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
        return "OPENSTANLEY_* env assignment"
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


# ---------- per-account layout (v0.5.0) ----------

def account_dir(acct: int | None = None) -> Path:
    """data/accounts/<id> — one account's whole world (brain, digests, …)."""
    a = db.active_account() if acct is None else int(acct)
    return ACCOUNTS_ROOT / str(a)


def brain_dir(acct: int | None = None) -> Path:
    return account_dir(acct) / "brain"


def _files_dir(acct: int | None = None) -> Path:
    return brain_dir(acct) / "files"


def _photos_dir(acct: int | None = None) -> Path:
    return brain_dir(acct) / "photos"


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

SEED_STRATEGIES = """# Creator Content Strategy — One-Pager

(The full generated strategy syncs here on every strategy regeneration —
goal, audience tiers, positioning, weighted pillars, rhythm, lean-into/
avoid. The learning log below keeps accumulating between regenerations.)

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


def _migrate_legacy_brain() -> bool:
    """One-time v0.5.0 move: the pre-multi-account data/brain/ belongs to
    (bootstrap) account 1. Returns True when a move happened. Never fires in
    test sandboxes (they anchor ACCOUNTS_ROOT away from the real data dir) —
    a swapped anchor must never swallow the REAL brain."""
    if ACCOUNTS_ROOT != ROOT / "data" / "accounts":
        return False  # anchor swapped (test sandbox) — hands off real data
    if not LEGACY_BRAIN_DIR.exists() or brain_dir(1).exists():
        return False
    import shutil
    brain_dir(1).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(LEGACY_BRAIN_DIR), str(brain_dir(1)))
    return True


def ensure(acct: int | None = None) -> None:
    """Create the account's brain dir with seeded defaults on first run.
    Idempotent. A fresh account starts EMPTY (seed stubs only) — nothing
    from other accounts ever leaks in (the user's hard requirement)."""
    if _migrate_legacy_brain():
        db.log("brain", "migrated data/brain/ → data/accounts/1/brain/ (v0.5.0)")
    root = brain_dir(acct)
    _files_dir(acct).mkdir(parents=True, exist_ok=True)
    _photos_dir(acct).mkdir(parents=True, exist_ok=True)
    seeds = {
        "instructions.md": SEED_INSTRUCTIONS,
        "rules.md": SEED_RULES,
        "strategies.md": SEED_STRATEGIES,
        "journal.md": SEED_JOURNAL,
    }
    for name, content in seeds.items():
        p = root / name
        if not p.exists():
            _atomic_write(p, content)
    for stem in SEED_FILES:
        p = _files_dir(acct) / f"{stem}.md"
        if not p.exists():
            _atomic_write(p, FILE_STUB.format(title=stem.replace("-", " ").title()))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{_secrets.token_hex(3)}")
    tmp.write_text(content, encoding="utf-8")
    # Windows: the destination can be transiently LOCKED by AV/indexers
    # during os.replace (WinError 5, live 2026-08-28 suite run) — retry
    # briefly instead of losing the write
    import time as _time
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            _time.sleep(0.05 * (attempt + 1))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- part addressing ----------

def _resolve(part: str, acct: int | None = None) -> Path:
    """Map a part name to a safe path inside the account's brain dir.
    Raises on traversal."""
    part = (part or "").strip().strip("/")
    if not part or ".." in part or part.startswith("."):
        raise FileNotFoundError(f"unknown brain part {part!r}")
    if part in MD_PARTS:
        return brain_dir(acct) / f"{part}.md"
    m = re.fullmatch(r"files/([A-Za-z0-9_\-]+)", part)
    if m:
        return _files_dir(acct) / f"{m.group(1)}.md"
    if part == "photos":
        return _photos_dir(acct)
    raise FileNotFoundError(f"unknown brain part {part!r}")


def read(part: str, acct: int | None = None) -> str:
    p = _resolve(part, acct)
    if p.is_dir():
        raise IsADirectoryError("photos part is a directory — use list_photos()")
    if not p.exists():
        ensure(acct)
    return p.read_text(encoding="utf-8")


def write(part: str, content: str, acct: int | None = None) -> None:
    """Sanitized, atomic write of a brain part."""
    p = _resolve(part, acct)
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


def inventory(acct: int | None = None) -> list[dict]:
    """[{name, type, size, modified, summary}] for every brain part."""
    ensure(acct)
    parts: list[dict] = []
    order = ("instructions", "rules", "strategies") + \
        tuple(f"files/{s}" for s in sorted(SEED_FILES)) + ("journal",)
    for part in order:
        p = _resolve(part, acct)
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        parts.append({
            "name": part, "type": "md", "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime)
                        .isoformat(timespec="seconds"),
            "summary": _summary(part, text),
        })
    photos = list_photos(acct)
    parts.append({
        "name": "photos", "type": "photos",
        "size": sum(ph["size"] for ph in photos),
        "modified": max((ph["modified"] for ph in photos), default=None),
        "summary": f"{len(photos)} photos",
    })
    return parts


# ---------- rules.md parsing ----------

RULE_HEADER_RE = re.compile(
    r"^##\s+\[R(\d+)\]\s*\((\w+)\s*[·-]\s*([\d\-]+)\s*[·-]\s*(active|retired)"
    r"(?:\s*[·-]\s*seen\s*([\d\-]+))?\)\s*$"
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
                       "date": m.group(3), "status": m.group(4),
                       "last_seen": m.group(5) or "", "text": ""}
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
    seen = f" · seen {rule['last_seen']}" if rule.get("last_seen") else ""
    return (f"## [R{rule['id']}] ({rule['source']} · {rule['date']} · "
            f"{rule['status']}{seen})\n{rule['text']}\n")


def add_rule(text: str, source: str, acct: int | None = None) -> int:
    """Append a new numbered rule. Returns its id. Sanitized."""
    text = sanitize(text.strip())
    if not text:
        raise ValueError("empty rule")
    raw = read("rules", acct)
    rules = parse_rules(raw)
    next_id = max((r["id"] for r in rules), default=0) + 1
    date = _now()[:10]
    block = render_rule({"id": next_id, "source": source, "date": date,
                         "status": "active", "text": text})
    # drop a placeholder comment when writing the first real rule
    raw = re.sub(r"<!--[^>]*no rules learned yet[^>]*-->\s*", "", raw)
    _atomic_write(_resolve("rules", acct), raw.rstrip() + "\n\n" + block)
    return next_id


def retire_rule(rule_id: int, acct: int | None = None) -> bool:
    """Flip a rule to retired (kept, struck through in the UI)."""
    raw = read("rules", acct)
    rules = parse_rules(raw)
    hit = next((r for r in rules if r["id"] == rule_id), None)
    if not hit or hit["status"] == "retired":
        return False
    old = render_rule(hit)
    hit["status"] = "retired"
    new = render_rule(hit)
    _atomic_write(_resolve("rules", acct), raw.replace(old, new, 1))
    return True


# ---------- journal ----------

JOURNAL_HEADER_RE = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\s*[·-]\s*(.+?)\s*$"
)


def journal_append(trigger: str, body: str, changes: Optional[list[str]] = None,
                   acct: int | None = None) -> None:
    """Append one dated entry. `trigger` is reflect:chat / user-edit / …"""
    body = sanitize(body.strip() or "(no notes)")
    lines = [f"## {_now()[:10]} {_now()[11:16]} · {trigger}", body]
    for ch in changes or []:
        ch = str(ch).strip()
        if ch:
            lines.append(f"- {ch}")
    p = _resolve("journal", acct)
    ensure(acct)
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


def brain_context(budget: int = BUDGET_CHARS, acct: int | None = None) -> str:
    """Compact digest for prompts: instructions + active rules + top strategies
    + pillar summaries, hard-capped at `budget` chars — from the ACTIVE
    account's brain only (never another account's memory)."""
    if _context_off.get():
        return ""
    ensure(acct)
    # fair-share budgets scaled to the total
    shares = (1200, 1200, 1600, 1200, 1000, 800)  # instructions, directives,
    #                rules, strategies, pillars, recent journal lessons
    blocks: list[str] = []

    instr = read("instructions", acct)
    # skip the manual's title; keep persona/workflow/priorities essence.
    # Learned adjustments: NEWEST few only (oldest-first injected week-old
    # baselines while today's learning got clipped), and self-directed
    # bookkeeping lines stay out of content prompts entirely (live audit
    # 2026-09-01: 'recompute x-times-baseline claims vs 0.0022' is the
    # agent's internal arithmetic, not wisdom for writing a post).
    def _not_bookkeeping(line: str) -> bool:
        low = line.lower()
        return not ("baseline" in low and re.search(r"0\.0\d", line))
    instr_core = "\n".join(ln for ln in instr.splitlines()
                           if not ln.startswith("# "))
    if "## Learned adjustments" in instr_core:
        head, adj = instr_core.split("## Learned adjustments", 1)
        adj_lines = [ln for ln in adj.splitlines()
                     if ln.strip().startswith("-") and _not_bookkeeping(ln)]
        instr_core = (head.rstrip()
                      + "\n\n## Recent adjustments (newest first)\n"
                      + "\n".join(adj_lines[:5]))
    blocks.append(_clip(instr_core, shares[0]))

    all_rules = [r for r in parse_rules(read("rules", acct)) if r["status"] == "active"]
    # OWNER DIRECTIVES lead the prompt: rules the owner personally dictated
    # (source="directive") are law, not learned heuristics — they get their
    # own block and budget so a flood of learned rules can never crowd them
    # out of the context window
    directives = [r for r in all_rules if r["source"] == "directive"]
    if directives:
        dlines = [f"R{r['id']}: {r['text']}" for r in directives]
        blocks.append(_clip("OWNER DIRECTIVES (absolute — the owner said these "
                            "in their own words):\n" + "\n".join(dlines),
                            shares[1]))
    # NEWEST-FIRST: rules.md appends, so file order = oldest-first — the
    # digest used to inject the Aug-21 pre-pivot era into every prompt
    # while this week's 33 rules got clipped (live audit 2026-09-01).
    # Bookkeeping rules (baseline arithmetic) stay in the file for audit
    # but never reach content prompts.
    rules = [r for r in reversed(all_rules)
             if r["source"] != "directive" and _not_bookkeeping(r["text"])]
    if rules:
        rule_lines = [f"R{r['id']}: {r['text']}" for r in rules]
        blocks.append(_clip("RULES (learned, newest first — obey):\n"
                            + "\n".join(rule_lines), shares[2]))

    strat = read("strategies", acct)
    # the one-pager lives above; the actionable digest is the learning
    # log — theses + experiments — not the (long) strategy prose
    _split = strat.split("## Working theses", 1)
    strat = ("## Working theses" + _split[1]) if len(_split) > 1 else strat
    strat_lines = [ln for ln in strat.splitlines()
                   if ln.strip() and not ln.startswith("#")][:12]
    if any(l.strip() and not l.startswith("- (none") for l in strat_lines):
        blocks.append(_clip("STRATEGIES:\n" + "\n".join(strat_lines), shares[3]))

    pillars_path = _files_dir(acct) / "content-pillars.md"
    if pillars_path.exists():
        pillars = [ln for ln in pillars_path.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.startswith("#") and "(OpenStanley writes"
                   not in ln][:6]
        if pillars:
            blocks.append(_clip("CONTENT PILLARS:\n" + "\n".join(pillars), shares[4]))

    # recent journal entries — the FRESHEST lessons were previously never
    # shown to the agent at all; two entries is the working set that matters
    journal_tail = parse_journal(read("journal", acct))[-2:]
    if journal_tail:
        jlines = [f"- {j.get('text', '')[:220]}" for j in journal_tail]
        blocks.append(_clip("RECENT LESSONS (newest learnings — obey):" + chr(10)
                            + chr(10).join(jlines), shares[5]))

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
docs only when the material clearly supports a rewrite.

Trigger glossary: "rejection" = the owner REFUSED these drafts — mine the
pattern (topic, tone, format, length, bait) they share, contrast with what
the owner approved, and propose DON'T rules for that pattern. One shared
pattern beats one rule per draft."""


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
    profile = db.get_acct_setting("style_profile") or {}
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


def _material_rejections() -> str:
    """Owner-rejected drafts vs approved contrast — what the owner refuses.
    Empty string when nothing is pending (reflect callers treat that as a
    no-op pass)."""
    from .rejection_learn import build_material
    return build_material()


MATERIALS = {"chat": _material_chat, "learn": _material_learn,
             "scan": _material_scan, "metrics": _material_metrics,
             "rejection": _material_rejections}


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

    topics_l = (stats.get("topics") or {}) if isinstance(stats.get("topics"), dict) else {}
    pillars = [f"# Content Pillars (scan {date})", ""]
    if topics_l:
        for t, w in list(topics_l.items())[:6]:
            pillars.append(f"- **{t}**: recurring theme (scan weight {w})")
    else:
        for t in topics[:5]:
            pillars.append(f"- **{t}**: high-frequency term the audience engages with")
    pillars += ["", "Pillars refine as reflect(scan)/strategy accumulate evidence."]

    cas = stats.get("casing") or {}
    emo = (stats.get("emoji") or {}).get("per_post")
    punct = stats.get("punctuation") or {}
    vc = [f"# Voice Cards (scan {date})", "",
          "One card per speaking mode detected in the corpus:", "",
          f"- **Standard post**: avg {stats.get('avg_length_chars')} chars, "
          f"casing {cas or 'n/a'}, emoji/post {emo}"]
    if punct.get("question"):
        vc.append(f"- **Question mode**: asks directly ({punct['question']}/post) "
                  "— the engagement-bait register")
    if punct.get("colon"):
        vc.append(f"- **Colon-led mode**: {punct['colon']}/post — 'hot take:' style openers")
    if punct.get("ellipsis"):
        vc.append(f"- **Trailing-thought mode**: {punct['ellipsis']}/post — '...' endings")
    vc += [f"- languages: {lang_txt}", ""]

    comp = [f"# Competitor Notes (scan {date})", ""]
    try:
        with db.connect() as _c:
            rows = _c.execute(
                "SELECT author_handle, COUNT(*) n, SUM(engagement) e "
                "FROM posts WHERE is_own=0 AND engagement > 0 "
                "AND account_id=? GROUP BY author_handle "
                "ORDER BY 3 DESC LIMIT 6",
                (db.active_account(),)).fetchall()
        for r in rows:
            comp.append(f"- **@{r['author_handle']}**: {r['n']} posts studied, "
                        f"{int(r['e'] or 0)} total engagement — study their hooks")
        if not rows:
            comp.append("- (no niche competitor posts stored yet — run study)")
    except Exception:  # noqa: BLE001
        comp.append("- (competitor data pending next study)")
    comp.append("")

    return [("niche-map", "\n".join(niche) + "\n"),
            ("audience-personas", "\n".join(personas) + "\n"),
            ("content-pillars", "\n".join(pillars) + "\n"),
            ("voice-cards", "\n".join(vc) + "\n"),
            ("competitor-notes", "\n".join(comp) + "\n")]


DECAY_DAYS = 14            # unreaffirmed rules older than this retire
DECAY_MAX_PER_SWEEP = 15   # a bug can never nuke the brain in one pass
SUBSUME_OVERLAP = 0.6      # token overlap with a NEWER rule = duplicate


def _forgetting_sweep(acct: int | None, changes: list[str]) -> None:
    """The forgetting governor (owner 2026-09-01: 'it will grow and grow —
    slop?'). Three verdicts per ACTIVE non-directive rule:

    - SUBSUMED: a newer active rule covers ≥60% of its tokens → retire
      the old duplicate (redundancy, not staleness)
    - REAFFIRMED: its R-id is cited in the working theses (the pattern is
      actively performing) → refresh its `seen` date
    - STALE: neither, and unseen past DECAY_DAYS → retire to archive

    Retire = archive (never delete, reversible). Directives are immune."""
    from datetime import date as _date
    rules = parse_rules(read("rules", acct))
    if not rules:
        return
    today = _date.today()
    today_iso = today.isoformat()
    strat_txt = read("strategies", acct)

    def _toks(t: str) -> set[str]:
        return {w for w in re.findall(r"[A-Za-z؀-ۿ]{4,}", t.lower())}

    actives = [r for r in rules if r["status"] == "active"]
    retired_out: list[tuple[dict, str]] = []
    out_lines: list[str] = ["# Learned Rules\n"]
    n_retired = 0
    refreshed = False
    for i, r in enumerate(rules):
        if r["status"] != "active":
            out_lines.append(render_rule(r))
            continue
        if r["source"] == "directive":
            out_lines.append(render_rule(r))
            continue
        seen = _date.fromisoformat(r["last_seen"] or r["date"])
        age = (today - seen).days
        verdict = None
        # subsumed by a NEWER active rule?
        rt = _toks(r["text"])
        for newer in actives:
            if newer["id"] <= r["id"] or newer["source"] == "directive":
                continue
            nt = _toks(newer["text"])
            if rt and nt and len(rt & nt) / len(rt | nt) >= SUBSUME_OVERLAP:
                verdict = f"subsumed by R{newer['id']}"
                break
        # reaffirmed by the working theses?
        if verdict is None and f"R{r['id']}" in strat_txt:
            if age > DECAY_DAYS:
                r = dict(r, last_seen=today_iso)  # data says it's alive
                refreshed = True
                changes.append(f"R{r['id']}: reaffirmed by theses (seen "
                               f"refreshed)")
            out_lines.append(render_rule(r))
            continue
        if verdict is None and age <= DECAY_DAYS:
            out_lines.append(render_rule(r))
            continue
        if verdict is None:
            verdict = f"stale — unreaffirmed for {age}d"
        if n_retired >= DECAY_MAX_PER_SWEEP:
            out_lines.append(render_rule(r))  # cap reached: keep the rest
            continue
        n_retired += 1
        rr = dict(r, status="retired")
        retired_out.append((rr, verdict))
        out_lines.append(render_rule(rr))

    if retired_out or refreshed:
        _atomic_write(_resolve("rules", acct), "\n".join(out_lines) + "\n")
        arch_path = _files_dir(acct) / "rules-archive.md"
        prev = arch_path.read_text(encoding="utf-8") \
            if arch_path.exists() else "# Retired Rules (audit archive)\n"
        stamp = _now()[:10]
        arch_txt = (prev.rstrip() + "\n\n"
                    + "\n\n".join(f"{render_rule(rr)}<!-- {stamp}: {why} -->"
                                  for rr, why in retired_out) + "\n")
        _atomic_write(arch_path, arch_txt)
        db.log("brain", f"forgetting governor: retired {n_retired} rule(s) "
                        f"to archive ({', '.join(why for _, why in retired_out[:3])}"
                        f"{'…' if len(retired_out) > 3 else ''})")
        changes.extend(f"R{rr['id']}: retired ({why})"
                       for rr, why in retired_out)



def reflect(cfg, trigger: str, payload: Optional[dict] = None,
            acct: int | None = None) -> dict:
    """Run one reflection: LLM proposes edits → applied deterministically.

    Returns {"ok", "applied": {added_rules, retired_rules, strategy_updates,
    instructions_updated}, "journal_entry"}. Raises LLMError only when the
    LLM itself fails (callers treat it as best-effort).
    """
    ensure(acct)
    if trigger not in MATERIALS:
        raise ValueError(f"unknown reflect trigger {trigger!r}")
    payload = payload or {}
    material = payload.get("material") or MATERIALS[trigger]()
    current_rules = [r for r in parse_rules(read("rules", acct)) if r["status"] == "active"]
    rules_ctx = "\n".join(f"R{r['id']}: {r['text']}" for r in current_rules) \
        or "(none yet)"
    user = (f"TRIGGER: {trigger}\n\nCURRENT ACTIVE RULES:\n{rules_ctx}\n\n"
            f"=== MATERIAL TO REVIEW ===\n{material}\n\n"
            "Propose your memory edits now (STRICT JSON).")

    raw = llm_chat(cfg.llm, system=REFLECT_SYSTEM, user=user,
                   temperature=0.4, json_mode=True)
    try:
        data = extract_json(raw)
    except LLMError:
        # one malformed-JSON retry (live 2026-08-31 15:45: the metrics
        # reflect died on a single bad emit with a 20000-token budget —
        # not starvation). Log the FULL raw text, not a 200-char clip, so
        # the next occurrence is actually diagnosable.
        db.log("brain", f"reflect({trigger}) unparseable JSON — retrying. "
                        f"Raw: {raw[:600]}", level="warn")
        raw = llm_chat(cfg.llm, system=REFLECT_SYSTEM, user=user,
                       temperature=0.2, json_mode=True)
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
            p = _resolve("instructions", acct)
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
            rid = add_rule(text, source=trigger, acct=acct)
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
        if retire_rule(rid, acct=acct):
            applied["retired_rules"].append(rid)
            changes.append(f"retired R{rid}")

    # 4. strategy updates — outcome=working goes to WORKING THESES
    # (upsert by title), everything else to the experiment log. The old
    # writer only knew the log: 'Working theses' sat on its placeholder
    # forever while confirmed theses piled into the log unnoticed
    # (live 2026-09-01: owner asked why strategy never fills).
    for su in data.get("strategy_updates") or []:
        if not isinstance(su, dict):
            continue
        title = str(su.get("title") or "").strip()
        if not title:
            continue
        note = str(su.get("note") or "").strip()
        outcome = str(su.get("outcome") or "new").strip()
        try:
            p = _resolve("strategies", acct)
            txt = p.read_text(encoding="utf-8")
            if outcome == "working":
                line = f"- {sanitize(title)} — {sanitize(note)} (confirmed {_now()[:10]})"
                txt = re.sub(r"^- \(none yet[^\n]*\n", "", txt, flags=re.M)
                if re.search(rf"^- {re.escape(title)}[ —·]", txt, flags=re.M):
                    txt = re.sub(rf"^- {re.escape(title)}[^\n]*", line, txt, count=1, flags=re.M)
                else:
                    m = re.search(r"^## Working theses\s*$", txt, flags=re.M)
                    if m:
                        txt = txt[:m.end()] + "\n" + line + txt[m.end():]
                    else:
                        txt = txt.rstrip() + f"\n\n## Working theses\n{line}\n"
            else:
                line = f"- {_now()[:10]} · {title} — {note} [{outcome}]"
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
            _atomic_write(_resolve(f"files/{stem}", acct), sanitize(content))
            applied["file_updates"].append(stem)
            changes.append(f"file {stem}: {content.splitlines()[0][:80]}")
        except BrainSecurityError:
            applied["dropped_tainted"] += 1

    # 4.6 scan trigger guarantees the niche/persona docs reflect the scan
    # even when the LLM proposes no file_updates (deterministic, stats-derived)
    if trigger == "scan":
        profile = db.get_acct_setting("style_profile") or {}
        stats = profile.get("stats") or {}
        if stats:
            done = set(applied["file_updates"])
            for stem, content in _scan_fallback_files(stats, profile):
                if stem not in done:
                    _atomic_write(_resolve(f"files/{stem}", acct), sanitize(content))
                    applied["file_updates"].append(stem)
                    changes.append(f"file {stem}: refreshed from scan stats")

    # 4.7 archive sweep: retired rules are audit history, not working
    # memory — when they pile up, move them out so rules.md stays the
    # ACTIVE set (live audit 2026-09-01: 112 retired blocks = 62% of the
    # file, dead weight for humans AND for every prompt read)
    try:
        all_blocks = parse_rules(read("rules", acct))
        retired = [r for r in all_blocks if r["status"] == "retired"]
        if len(retired) > 40:
            active_txt = ("# Learned Rules\n\n"
                          + "\n\n".join(render_rule(r) for r in all_blocks
                                        if r["status"] == "active") + "\n")
            arch_path = _files_dir(acct) / "rules-archive.md"
            prev = arch_path.read_text(encoding="utf-8") \
                if arch_path.exists() else "# Retired Rules (audit archive)\n"
            arch_txt = (prev.rstrip() + "\n\n"
                        + "\n\n".join(render_rule(r) for r in retired) + "\n")
            _atomic_write(_resolve("rules", acct), active_txt)
            _atomic_write(arch_path, arch_txt)
            db.log("brain", f"archived {len(retired)} retired rules to "
                            f"files/rules-archive.md")
            changes.append(f"archived {len(retired)} retired rules")
    except Exception as e:  # noqa: BLE001 — housekeeping never breaks reflect
        db.log("brain", f"rule archive sweep failed: {e}", level="warn")

    # 4.8 FORGETTING GOVERNOR — memory that earns its stay. Unreaffirmed
    # rules decay to the archive; subsumed duplicates retire; thesis-
    # cited rules refresh. Directives NEVER decay (owner law). Archive-
    # only and capped: a bug can never nuke the brain.
    try:
        _forgetting_sweep(acct, changes)
    except Exception as e:  # noqa: BLE001 — housekeeping never breaks reflect
        db.log("brain", f"forgetting sweep failed: {e}", level="warn")

# 5. journal entry (always written — even an empty reflection is a fact)
    entry = str(data.get("journal_entry") or "").strip() or \
        f"reflected on {trigger}; no changes warranted"
    if payload.get("note"):
        entry = f"{payload['note']} — {entry}"
    journal_append(f"reflect:{trigger}", entry, changes, acct=acct)
    if applied["dropped_tainted"]:
        journal_append(f"reflect:{trigger}",
                       "dropped a proposed edit that looked secret-like "
                       "(never stored).", acct=acct)

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
               usage: str = "", acct: int | None = None) -> dict:
    """Save an uploaded image + sidecar .md note. Returns the photo record."""
    sanitize(f"{filename} {caption} {usage}")  # captions too — no secrets
    ext = Path(filename).suffix.lower()
    if ext not in PHOTO_EXTS:
        raise ValueError(f"unsupported photo type {ext} (png/jpg/webp/gif)")
    ensure(acct)
    stem = Path(filename).stem
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", stem)[:40] or "photo"
    name = f"{stem}_{_secrets.token_hex(4)}{ext}"
    (_photos_dir(acct) / name).write_bytes(data)
    _atomic_write(_photos_dir(acct) / f"{name}.md", PHOTO_SIDECAR_NOTE.format(
        name=name, date=_now(), caption=caption.strip() or "(none)",
        usage=f"- usage context: {usage.strip()}" if usage.strip() else ""))
    db.log("brain", f"photo saved: {name} ({len(data)} bytes)")
    return photo_record(name, acct)


def photo_record(name: str, acct: int | None = None) -> dict:
    p = _photos_dir(acct) / name
    sidecar = _photos_dir(acct) / f"{name}.md"
    caption = ""
    if sidecar.exists():
        m = re.search(r"(?m)^- caption:\s*(.*)$", sidecar.read_text(encoding="utf-8"))
        if m:
            caption = m.group(1).strip()
    return {"name": name, "size": p.stat().st_size if p.exists() else 0,
            "modified": datetime.fromtimestamp(p.stat().st_mtime)
                        .isoformat(timespec="seconds") if p.exists() else None,
            "caption": caption, "url": f"/api/brain/photos/{name}"}


def list_photos(acct: int | None = None) -> list[dict]:
    ensure(acct)
    return [photo_record(p.name, acct) for p in sorted(_photos_dir(acct).iterdir())
            if p.suffix.lower() in PHOTO_EXTS]


def photo_path(name: str, acct: int | None = None) -> Path:
    """Safe path for serving a photo. Raises on traversal/unknown names."""
    if "/" in name or "\\" in name or ".." in name:
        raise FileNotFoundError("bad photo name")
    p = _photos_dir(acct) / name
    if p.suffix.lower() not in PHOTO_EXTS or not p.exists():
        raise FileNotFoundError(f"no such photo {name!r}")
    return p


# ---------- brain snapshot for evals (used by the harness A/B mode) ----------

def has_meaningful_brain(acct: int | None = None) -> bool:
    """True once the brain carries learned content beyond the seed stubs."""
    ensure(acct)
    rules = [r for r in parse_rules(read("rules", acct)) if r["status"] == "active"]
    strat = read("strategies", acct)
    real_strat = any(l.strip() and not l.startswith("#")
                     and "(none" not in l for l in strat.splitlines())
    return bool(rules) or real_strat


def to_dict(acct: int | None = None) -> dict:
    """Whole brain as a dict (for tests + harness inspection)."""
    ensure(acct)
    out = {p: read(p, acct) for p in MD_PARTS}
    out["files"] = {f"{s}.md": read(f"files/{s}", acct) for s in SEED_FILES}
    out["photos"] = list_photos(acct)
    return out


def from_dict(snapshot: dict, acct: int | None = None) -> None:
    """Restore brain files from a snapshot (tests only — sanitized)."""
    ensure(acct)
    for k, v in snapshot.items():
        if k == "files":
            for fname, content in v.items():
                _atomic_write(_files_dir(acct) / Path(fname).name, sanitize(content))
        elif k == "photos":
            continue
        elif k in MD_PARTS:
            _atomic_write(_resolve(k, acct), sanitize(v))
