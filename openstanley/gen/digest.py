"""Daily digest — the agent reports to its owner (v0.4.2).

OpenStanley does a lot autonomously (autopilot phases, scans, metrics, voice
lock, smart slots, mentions) but the human only sees it by opening the
dashboard. The relationship model is "your employee": it should proactively
REPORT what it did, what it learned, and what needs a decision.

build_digest(cfg, day) gathers everything from the DB and brain files —
no X reads, no LLM (journal summaries come from stored reflection
outputs). render_markdown / render_text shape it for humans; deliver()
stores it (data/digests/YYYY-MM-DD.md + the `digest_last` setting) and
POSTs the text to a webhook as generic JSON `{text: ...}` — works with a
Telegram bot relay, Discord, Slack-compatible endpoints.

Sections:
  did        loops run, posts published, replies sent, drafts the voice
             lock rejected, targets the engage gate rejected, mentions
  learned    brain journal entries (top 3), rules added/retired, strategy
             deltas for the day
  needs you  pending approvals (count + previews), autopilot state,
             cookie/smoke health
  numbers    followers delta (identity snapshots), avg engagement rate,
             best post of the day
  tomorrow   top smart slots with reasons, ideas remaining in the bank
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

from ..core import db
from ..core.config import Config

ROOT = Path(__file__).resolve().parent.parent.parent
DIGEST_DIR_ENV = "OPENSTANLEY_DIGEST_DIR"      # tests redirect the output dir
LOOP_NAMES = ("import", "study", "create", "engage", "mentions",
              "publish", "learn", "scan", "autopilot")
WEBHOOK_TIMEOUT_S = 10.0
JOURNAL_TOP = 3
PREVIEW_CHARS = 90
SLOTS_TOP = 3

# agent_log message shapes written by voice_lock / engage_gate — parsed,
# never re-derived (the log IS the record of what happened that day)
_VOICE_REJECT_RE = re.compile(r"voice lock rejected draft \(score (\d+), "
                              r"reasons: (.+)\)")
_ENGAGE_REJECT_RE = re.compile(r"gate: rejected (\d+)/(\d+) reply targets "
                               r"— (.+)")
_ENGAGE_REASON_RE = re.compile(r"(.*?) ×(\d+)(?:,|$)")


# ---------- the digest object ----------

@dataclass
class Digest:
    """One day, assembled from local state only."""
    day: str                                   # YYYY-MM-DD
    did: dict = field(default_factory=dict)
    learned: dict = field(default_factory=dict)
    needs_you: dict = field(default_factory=dict)
    numbers: dict = field(default_factory=dict)
    tomorrow: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- small helpers ----------

def _day_str(day: str | date | datetime | None) -> str:
    if day is None:
        return date.today().isoformat()
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    return str(day)[:10]


def _clip(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_iso(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def digest_dir(acct: int | None = None) -> Path:
    """Where digest markdown files live: data/accounts/<id>/digests/
    (v0.5.0). Env-overridable for tests (sandbox then serves all accounts)."""
    p = os.environ.get(DIGEST_DIR_ENV)
    if p:
        return Path(p)
    from . import brain as _brain
    root = _brain.account_dir(acct) / "digests"
    _migrate_legacy_digests(root)
    return root


def _migrate_legacy_digests(new_root: Path) -> None:
    """One-time v0.5.0 move: data/digests/ belongs to (bootstrap) account 1.
    Never fires when the env sandbox is active or the target already exists."""
    legacy = ROOT / "data" / "digests"
    if os.environ.get(DIGEST_DIR_ENV):
        return
    if not legacy.exists() or new_root.exists() or new_root == legacy:
        return
    import shutil
    if _brain_account_root_is_real():
        shutil.move(str(legacy), str(new_root))
        db.log("digest", "migrated data/digests/ → data/accounts/1/digests/ (v0.5.0)")


def _brain_account_root_is_real() -> bool:
    from . import brain as _brain
    return _brain.ACCOUNTS_ROOT == _brain.ROOT / "data" / "accounts"


# ---------- section builders (DB / brain files only) ----------

def _did_section(day: str) -> dict:
    with db.connect() as c:
        loop_rows = c.execute(
            f"SELECT loop, COUNT(*) AS n FROM agent_log "
            f"WHERE loop IN ({','.join('?' * len(LOOP_NAMES))}) "
            f"AND substr(ts, 1, 10) = ? GROUP BY loop",
            (*LOOP_NAMES, day)).fetchall()
        pub_rows = c.execute(
            "SELECT id, x_id, text, kind FROM drafts "
            "WHERE status = 'published' AND published_at IS NOT NULL "
            "AND substr(published_at, 1, 10) = ? ORDER BY published_at",
            (day,)).fetchall()
        reply_created = c.execute(
            "SELECT meta_json FROM drafts WHERE kind = 'reply' "
            "AND substr(created_at, 1, 10) = ?", (day,)).fetchall()
        voice_rows = c.execute(
            "SELECT message FROM agent_log WHERE loop = 'voice' "
            "AND substr(ts, 1, 10) = ? AND message LIKE "
            "'voice lock rejected draft%'", (day,)).fetchall()
        engage_rows = c.execute(
            "SELECT message FROM agent_log WHERE loop = 'engage' "
            "AND substr(ts, 1, 10) = ? AND message LIKE 'gate: rejected%'",
            (day,)).fetchall()
        mention_stats = c.execute(
            "SELECT COALESCE(SUM(handled), 0) AS handled, "
            "COALESCE(SUM(1 - handled), 0) AS pending FROM seen_mentions"
        ).fetchone()

    loops = {r["loop"]: int(r["n"]) for r in loop_rows if int(r["n"]) > 0}

    posts = [r for r in pub_rows if r["kind"] != "reply"]
    top = None
    if posts:
        xids = [r["x_id"] for r in posts if r["x_id"]]
        by_xid: dict[str, dict] = {}
        if xids:
            with db.connect() as c:
                for row in c.execute(
                    f"SELECT x_id, text, likes, reposts, replies, engagement "
                    f"FROM posts WHERE is_own = 1 AND x_id IN "
                    f"({','.join('?' * len(xids))})", xids):
                    by_xid[row["x_id"]] = dict(row)
        candidates = [by_xid[r["x_id"]] for r in posts
                      if r["x_id"] and r["x_id"] in by_xid]
        if candidates:
            best = max(candidates, key=lambda p: p["engagement"] or 0)
            top = {"text": _clip(best["text"]),
                   "likes": best["likes"], "reposts": best["reposts"],
                   "replies": best["replies"],
                   "engagement": best["engagement"]}

    mentions_handled = 0
    for r in reply_created:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if meta.get("source") == "mention":
            mentions_handled += 1

    voice_count = len(voice_rows)
    top_violation: Optional[str] = None
    if voice_rows:
        violations = Counter()
        for r in voice_rows:
            m = _VOICE_REJECT_RE.search(r["message"] or "")
            if m and m.group(2):
                violations[m.group(2).split(";")[0].strip()] += 1
        top_violation = violations.most_common(1)[0][0] if violations else None

    engage_count = 0
    top_reason: Optional[str] = None
    if engage_rows:
        reasons = Counter()
        for r in engage_rows:
            m = _ENGAGE_REJECT_RE.search(r["message"] or "")
            if not m:
                continue
            engage_count += int(m.group(1))
            for rm in _ENGAGE_REASON_RE.finditer(m.group(3)):
                reasons[rm.group(1).strip()] += int(rm.group(2))
        top_reason = reasons.most_common(1)[0][0] if reasons else None

    return {
        "loops": loops,
        "posts_published": {"count": len(posts), "top": top},
        "replies_sent": sum(1 for r in pub_rows if r["kind"] == "reply"),
        "voice_rejected": {"count": voice_count, "top_violation": top_violation},
        "engage_rejected": {"count": engage_count, "top_reason": top_reason},
        "mentions": {"handled": int(mention_stats["handled"]),
                     "pending": int(mention_stats["pending"])},
        "mentions_handled_today": mentions_handled,
    }


def _learned_section(day: str) -> dict:
    from . import brain as brain_mod  # lazy: sandboxed dir swaps cleanly in tests

    def _read(part: str) -> str:
        try:
            return brain_mod.read(part)
        except Exception:  # noqa: BLE001 — missing brain → honest empties
            return ""

    journal = []
    rules_added: list[str] = []
    rules_retired = 0
    strategy_updates: list[str] = []
    for e in brain_mod.parse_journal(_read("journal")):
        if e.get("date") != day:
            continue
        summary = _clip(e.get("body") or "(no notes)")
        journal.append({"time": e.get("time") or "", "trigger": e.get("trigger") or "",
                        "summary": summary})
        for ch in e.get("changes") or []:
            if re.match(r"added R\d+", ch):
                rules_added.append(_clip(ch))
            elif re.match(r"retired R\d+", ch):
                rules_retired += 1
            elif ch.startswith("strategy: "):
                strategy_updates.append(_clip(ch[len("strategy: "):]))
    # rules.md dates are the additive source of truth for "added"
    for r in brain_mod.parse_rules(_read("rules")):
        if r.get("date") == day and r.get("status") == "active" \
                and not any(f"R{r['id']}" in a for a in rules_added):
            rules_added.append(_clip(f"added R{r['id']}: {r['text']}"))

    return {"journal": journal[:JOURNAL_TOP],
            "rules_added": {"count": len(rules_added),
                            "latest": rules_added[0] if rules_added else None},
            "rules_retired": rules_retired,
            "strategy_updates": strategy_updates}


def _needs_you_section() -> dict:
    from . import autopilot as ap_mod  # lazy: same reason as brain

    with db.connect() as c:
        pending = c.execute(
            "SELECT id, text FROM drafts WHERE status = 'draft' "
            "ORDER BY created_at LIMIT 2").fetchall()
        (count,) = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE status = 'draft'").fetchone()

    ap = ap_mod.get_state()
    smoke = db.get_setting("smoke_last") or {}
    ran_at = smoke.get("ran_at")
    age_h: Optional[float] = None
    parsed = _parse_iso(ran_at)
    if parsed is not None:
        age_h = round(max(0.0, (datetime.now(parsed.tzinfo) - parsed)
                          .total_seconds() / 3600), 1)
    return {
        "pending_approvals": {
            "count": int(count),
            "previews": [_clip(r["text"]) for r in pending],
        },
        "autopilot": {"enabled": bool(ap.get("enabled")),
                      "phase": ap.get("phase"),
                      "next_tick": ap.get("next_tick")},
        "smoke": {"status": smoke.get("status") or "never",
                  "ran_at": ran_at, "age_h": age_h},
    }


def _numbers_section(day: str) -> dict:
    with db.connect() as c:
        snaps = c.execute(
            "SELECT followers FROM identity_snapshots "
            "WHERE substr(captured_at, 1, 10) = ? "
            "ORDER BY captured_at, id", (day,)).fetchall()
        carry = c.execute(
            "SELECT followers FROM identity_snapshots "
            "WHERE substr(captured_at, 1, 10) < ? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1", (day,)).fetchone()
        day_posts = c.execute(
            "SELECT x_id, text, likes, reposts, replies, impressions, "
            "engagement FROM posts WHERE is_own = 1 "
            "AND substr(created_at, 1, 10) = ?", (day,)).fetchall()

    followers_delta: Optional[int] = None
    if snaps:
        # gain vs the latest snapshot BEFORE the day (or the day's own first
        # when no earlier one exists) — one consistent "gained today" number
        last = int(snaps[-1]["followers"])
        first = int(carry["followers"]) if carry else int(snaps[0]["followers"])
        followers_delta = last - first

    total_imp = sum(int(p["impressions"] or 0) for p in day_posts)
    total_eng = sum(int(p["likes"] or 0) + 3 * int(p["reposts"] or 0)
                    + 8 * int(p["replies"] or 0) for p in day_posts)
    avg_rate = (round(total_eng / total_imp, 5) if total_imp > 0 else None)
    best = None
    if day_posts:
        bp = max(day_posts, key=lambda p: p["engagement"] or 0)
        best = {"text": _clip(bp["text"]), "likes": bp["likes"],
                "replies": bp["replies"], "engagement": bp["engagement"]}
    return {"followers_delta": followers_delta,
            "avg_engagement_rate": avg_rate,
            "best_post": best}


def _tomorrow_section(cfg: Config) -> dict:
    from . import slots as slots_mod  # lazy: metrics import stays out of module load

    slots = [{"time": s["at"].strftime("%H:%M"), "reason": s["reason"]}
             for s in slots_mod.best_slots(cfg, datetime.now(), k=SLOTS_TOP)]
    return {"slots": slots, "ideas_remaining": db.idea_count()}


def build_digest(cfg: Config, day: str | date | datetime | None = None) -> Digest:
    """Assemble one day's digest. DB + brain files only — no X, no LLM."""
    d = _day_str(day)
    return Digest(
        day=d,
        did=_did_section(d),
        learned=_learned_section(d),
        needs_you=_needs_you_section(),
        numbers=_numbers_section(d),
        tomorrow=_tomorrow_section(cfg),
    )


# ---------- rendering ----------

# per-language labels: section heads + inline labels. EN is the source of
# truth; AR mirrors it (same "existing lang module pattern" as the UI dicts).
_EMOJI = {"did": "✅", "learned": "🧠", "needs_you": "⚠️",
          "numbers": "📊", "tomorrow": "📅"}

_LABELS = {
    "en": {
        "title": "OpenStanley daily digest",
        "did": "What I did",
        "learned": "What I learned",
        "needs_you": "Needs you",
        "numbers": "Numbers",
        "tomorrow": "Tomorrow",
        "loops": "loops run",
        "published": "published {n} post(s)",
        "top_post": "top",
        "nothing_published": "published 0 posts — nothing shipped today",
        "replies": "sent {n} repl(ies)",
        "voice_rejected": "voice lock rejected {n} draft(s)",
        "engage_rejected": "engage gate rejected {n} target(s)",
        "mentions": "mentions: {handled} handled · {pending} pending",
        "no_journal": "no reflections today",
        "rules": "rules: {added} added · {retired} retired",
        "no_rules": "no rule changes today",
        "strategy": "strategy",
        "approvals": "{n} draft(s) waiting for approval",
        "no_approvals": "no drafts waiting — queue is clear",
        "autopilot_on": "autopilot on — next tick {when}",
        "autopilot_off": "autopilot off",
        "smoke": "health check: {status}",
        "smoke_age": "({age}h ago)",
        "followers": "followers {delta}",
        "avg_rate": "avg engagement rate {rate}",
        "best_of_day": "best post",
        "slot_ideas": "idea bank: {n} idea(s) left",
        "no_slots": "no slots scored",
    },
    "ar": {
        "title": "تقرير ستانلي اليومي",
        "did": "ما أنجزته",
        "learned": "ما تعلمته",
        "needs_you": "يحتاج قرارك",
        "numbers": "الأرقام",
        "tomorrow": "غداً",
        "loops": "الحلقات",
        "published": "نُشر {n} منشوراً",
        "top_post": "الأفضل",
        "nothing_published": "لا منشورات اليوم — لم يُنشر شيء",
        "replies": "أُرسل {n} رداً",
        "voice_rejected": "رفض قفل الصوت {n} مسودة",
        "engage_rejected": "رفضت بوابة التفاعل {n} هدفاً",
        "mentions": "الإشارات: {handled} عولجت · {pending} معلّقة",
        "no_journal": "لا تأملات اليوم",
        "rules": "القواعد: {added} جديدة · {retired} متقاعدة",
        "no_rules": "لا تغييرات على القواعد اليوم",
        "strategy": "الاستراتيجية",
        "approvals": "{n} مسودة بانتظار اعتمادك",
        "no_approvals": "لا مسودات معلّقة — الطابور فارغ",
        "autopilot_on": "الطيّار الآلي يعمل — النبضة التالية {when}",
        "autopilot_off": "الطيّار الآلي متوقف",
        "smoke": "فحص الصحة: {status}",
        "smoke_age": "(قبل {age} ساعة)",
        "followers": "المتابعون {delta}",
        "avg_rate": "متوسط التفاعل {rate}",
        "best_of_day": "أفضل منشور",
        "slot_ideas": "بنك الأفكار: {n} فكرة متبقية",
        "no_slots": "لا خانات مرتبة",
    },
}


def _fmt_delta(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _lines(d: Digest, lang: str = "en") -> dict[str, list[str]]:
    """Section → emoji-headed content lines (shared by both renderers)."""
    L = _LABELS.get(lang, _LABELS["en"])

    did = d.did or {}
    pub = did.get("posts_published") or {}
    top = pub.get("top")
    did_lines = []
    if did.get("loops"):
        did_lines.append(f"{L['loops']}: "
                         + ", ".join(f"{k} ×{v}" for k, v in
                                     sorted(did["loops"].items())))
    if pub.get("count"):
        line = L["published"].format(n=pub["count"])
        if top:
            line += f" — {L['top_post']}: “{top['text']}” ({top['engagement']} eng)"
        did_lines.append(line)
    else:
        did_lines.append(L["nothing_published"])
    if did.get("replies_sent"):
        did_lines.append(L["replies"].format(n=did["replies_sent"]))
    vr = did.get("voice_rejected") or {}
    if vr.get("count"):
        line = L["voice_rejected"].format(n=vr["count"])
        if vr.get("top_violation"):
            line += f" — {vr['top_violation']}"
        did_lines.append(line)
    er = did.get("engage_rejected") or {}
    if er.get("count"):
        line = L["engage_rejected"].format(n=er["count"])
        if er.get("top_reason"):
            line += f" — {er['top_reason']}"
        did_lines.append(line)
    men = did.get("mentions") or {}
    if men.get("handled") or men.get("pending"):
        did_lines.append(L["mentions"].format(handled=men.get("handled", 0),
                                              pending=men.get("pending", 0)))

    learned = d.learned or {}
    learned_lines = []
    for e in learned.get("journal") or []:
        learned_lines.append(f"{e.get('time') or ''} {e.get('trigger') or ''} — "
                             f"{e['summary']}".strip())
    if not learned_lines:
        learned_lines.append(L["no_journal"])
    ra = learned.get("rules_added") or {}
    if ra.get("count") or learned.get("rules_retired"):
        learned_lines.append(L["rules"].format(added=ra.get("count", 0),
                                               retired=learned.get("rules_retired", 0)))
        if ra.get("latest"):
            learned_lines.append(f"· {ra['latest']}")
    else:
        learned_lines.append(L["no_rules"])
    for su in learned.get("strategy_updates") or []:
        learned_lines.append(f"{L['strategy']}: {su}")

    needs = d.needs_you or {}
    needs_lines = []
    pa = needs.get("pending_approvals") or {}
    if pa.get("count"):
        needs_lines.append(L["approvals"].format(n=pa["count"]))
        needs_lines += [f"· “{p}”" for p in pa.get("previews") or []]
    else:
        needs_lines.append(L["no_approvals"])
    ap = needs.get("autopilot") or {}
    needs_lines.append(L["autopilot_on"].format(when=ap.get("next_tick") or "?")
                       if ap.get("enabled") else L["autopilot_off"])
    sm = needs.get("smoke") or {}
    line = L["smoke"].format(status=sm.get("status", "never"))
    if sm.get("age_h") is not None:
        line += " " + L["smoke_age"].format(age=sm["age_h"])
    needs_lines.append(line)

    numbers = d.numbers or {}
    numbers_lines = []
    if numbers.get("followers_delta") is not None:
        numbers_lines.append(L["followers"].format(
            delta=_fmt_delta(numbers["followers_delta"])))
    if numbers.get("avg_engagement_rate") is not None:
        numbers_lines.append(L["avg_rate"].format(
            rate=f"{numbers['avg_engagement_rate'] * 100:.2f}%"))
    bp = numbers.get("best_post")
    if bp:
        numbers_lines.append(f"{L['best_of_day']}: “{bp['text']}” "
                             f"({bp['engagement']} eng)")

    tomorrow = d.tomorrow or {}
    tomorrow_lines = []
    for s in tomorrow.get("slots") or []:
        tomorrow_lines.append(f"{s['time']} — {s['reason']}")
    if not tomorrow_lines:
        tomorrow_lines.append(L["no_slots"])
    tomorrow_lines.append(L["slot_ideas"].format(n=tomorrow.get("ideas_remaining", 0)))

    return {"did": did_lines, "learned": learned_lines,
            "needs_you": needs_lines, "numbers": numbers_lines,
            "tomorrow": tomorrow_lines}


def render_markdown(digest: Digest, lang: str = "en") -> str:
    """Dashboard/file rendering — section headers + bullet lists."""
    L = _LABELS.get(lang, _LABELS["en"])
    out = [f"# 📰 {L['title']} · {digest.day}"]
    for key in ("did", "learned", "needs_you", "numbers", "tomorrow"):
        head = f"## {_EMOJI[key]} {L[key]}"
        body = "\n".join(f"- {ln}" for ln in _lines(digest, lang)[key])
        out.append(f"{head}\n{body}")
    return "\n\n".join(out) + "\n"


def render_text(digest: Digest, lang: str = "en") -> str:
    """Compact webhook rendering — emoji-headed plain lines, no markdown."""
    L = _LABELS.get(lang, _LABELS["en"])
    out = [f"📰 {L['title']} · {digest.day}"]
    for key in ("did", "learned", "needs_you", "numbers", "tomorrow"):
        out.append(f"{_EMOJI[key]} {L[key]}")
        out.extend(_lines(digest, lang)[key])
        out.append("")  # blank line between sections
    return "\n".join(out).rstrip() + "\n"


# ---------- delivery ----------

def webhook_url() -> str:
    return str(db.get_setting("digest_webhook_url") or "")


def send_webhook(url: str, text: str) -> dict:
    """POST {text: ...} to the configured webhook. Never raises."""
    try:
        r = httpx.post(url, json={"text": text}, timeout=WEBHOOK_TIMEOUT_S)
        return {"ok": 200 <= r.status_code < 300, "status_code": r.status_code,
                "error": None if r.status_code < 300 else r.text[:200]}
    except Exception as e:  # noqa: BLE001 — delivery must never take the job down
        return {"ok": False, "status_code": None, "error": str(e)[:200]}


def store_digest(digest: Digest, markdown: str) -> Path:
    """Write data/digests/<day>.md and remember it in settings."""
    p = digest_dir() / f"{digest.day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(markdown, encoding="utf-8")
    return p


def deliver(cfg: Config, day: str | date | None = None, lang: str | None = None,
            force: bool = False) -> dict:
    """Build → store → send. The scheduler job and the API both land here.

    A webhook POST fires only when a URL is configured AND this day wasn't
    already delivered (force overrides — the "Send test digest" button).
    """
    lang = lang or str(db.get_setting("language") or "en")
    digest = build_digest(cfg, day)
    markdown = render_markdown(digest, lang)
    text = render_text(digest, lang)
    path = store_digest(digest, markdown)

    last = db.get_acct_setting("digest_last") or {}
    already_sent = bool(last.get("sent")) and last.get("day") == digest.day
    should_send = force or not already_sent
    sent, status_code, error = False, None, None
    url = webhook_url()
    if url and should_send:
        result = send_webhook(url, text)
        sent, status_code, error = result["ok"], result["status_code"], result["error"]
    # v0.4.4 — the digest also reaches the owner's Telegram chat (parallel
    # to the webhook, same once-per-day gate). Failure never breaks delivery.
    tg_sent = False
    try:
        from ..integrations import telegram as tg_mod
        if tg_mod.is_enabled() and should_send:
            tg_sent = bool(tg_mod.notify(text)["sent"])
    except Exception as e:  # noqa: BLE001 — TG bridge is strictly optional
        db.log("digest", f"telegram bridge failed: {e}", level="warn")
    record = {"day": digest.day, "sent": sent or already_sent or tg_sent,
              "at": datetime.now().isoformat(timespec="seconds"),
              "status_code": status_code, "error": error,
              "tg_sent": tg_sent, "file": str(path)}
    db.set_acct_setting("digest_last", record)
    db.log("digest", f"daily digest for {digest.day} — stored"
                     f"{' and delivered' if (sent or tg_sent) else ''}"
                     f"{f' (send failed: {error})' if error else ''}")
    return {"ok": True, "day": digest.day, "sent": sent,
            "tg_sent": tg_sent,
            "already_sent": already_sent and not sent and not tg_sent,
            "status_code": status_code, "error": error, "file": str(path)}


def history(limit: int = 7) -> list[str]:
    """Last N digest days on disk, newest first (the history picker)."""
    try:
        days = sorted(p.stem for p in digest_dir().glob("????-??-??.md")
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    except OSError:
        return []
    return days[-limit:][::-1]


def read_stored(day: str) -> Optional[str]:
    """Markdown of a stored digest, or None when the day has no file."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    p = digest_dir() / f"{day}.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


# ---------- scheduler integration ----------

def digest_hour(cfg: Config) -> int:
    """When the daily digest job fires — db setting wins, else config."""
    v = db.get_setting("agent_digest_hour")
    if v is not None:
        try:
            return max(0, min(23, int(v)))
        except (TypeError, ValueError):
            pass
    return max(0, min(23, int(getattr(cfg.agent, "digest_hour", 20) or 20)))


def mask_webhook_url(url: str) -> str:
    """Telegram bot URLs embed the token — mask everything but the host."""
    if not url:
        return ""
    m = re.match(r"^(https?://[^/]+)", url)
    host = m.group(1) if m else ""
    tail = url[-4:] if len(url) > len(host) + 12 else ""
    return f"{host}/•••••{tail}"
