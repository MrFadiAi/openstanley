"""Rejection learning — the owner's NO is the strongest teaching signal.

A reject tap says "this does not represent me" far more precisely than any
engagement metric, and before this module it taught nothing: the draft just
flipped to status=rejected and the brain never looked at it.

Every rejection path (web endpoint, TG command, TG inline tap, nightly
expiry sweep) stamps the draft's meta with why it died. Owner rejections
(reason != "expired") accumulate; at >=3 pending, or on the nightly pass,
the brain's own reflect() machinery reviews them — contrasted against what
the owner DID approve — and extracts patterns into rules with
source="rejection". Expiry rejections are queue hygiene, not taste: stamped,
never learned.

Nothing here ever blocks a reject tap: recording is a meta update, and the
reflection runs in a daemon thread.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from ..core import db

# owner rejections waiting to be learned before the async trigger fires —
# one reflection per rejection would burn LLM calls on single data points
REFLECT_THRESHOLD = 3

# how many unlearned rejections one reflection pass reviews (newest first)
BATCH = 12


def record_rejection(draft_id: int, reason: str = "owner", via: str = "web",
                     acct: Optional[int] = None) -> None:
    """Stamp WHY a draft died onto its meta. Idempotent — a double tap or a
    sweep hitting an already-stamped draft never overwrites the first reason
    (the owner's word outranks the sweeper's clock)."""
    d = db.get_draft(draft_id, acct=acct)
    if not d or d["status"] != "rejected":
        return
    meta = dict(d.get("meta") or {})
    if meta.get("rejected_reason"):
        return
    meta["rejected_reason"] = reason
    meta["rejected_via"] = via
    meta["rejected_at"] = datetime.now().isoformat(timespec="seconds")
    db.update_draft(draft_id, acct=acct, meta_json=meta)
    try:
        from . import brain as _brain
        _brain.note_outcome(int(draft_id), False)  # cited rules logged
    except Exception:  # noqa: BLE001 — never block a rejection
        pass


def _rejected(acct: Optional[int] = None, limit: int = 60) -> list[dict]:
    return db.drafts_by_status("rejected", limit, acct=acct)


def pending_owner_rejections(acct: Optional[int] = None) -> list[dict]:
    """Rejected drafts carrying a real owner decision, not yet learned."""
    out = []
    for d in _rejected(acct):
        meta = d.get("meta") or {}
        if meta.get("rejected_reason") in (None, "expired"):
            continue
        if meta.get("rejection_learned"):
            continue
        out.append(d)
    out.sort(key=lambda d: d["id"], reverse=True)
    return out


def _fmt(d: dict) -> str:
    meta = d.get("meta") or {}
    kind = d.get("kind") or "post"
    src = meta.get("source") or "loop"
    topic = meta.get("topic") or meta.get("idea") or ""
    line = f"- [{kind} · {src}" + (f" · {topic}" if topic else "") + "] "
    body = d["text"] or ""
    if d.get("thread"):
        body = " / ".join([body] + [t for t in d["thread"][1:3]])
    return line + body[:150]


def build_material(acct: Optional[int] = None) -> str:
    """REJECTED (owner decisions, unlearned) vs APPROVED contrast → the LLM
    extracts what the owner refuses, not what merely failed a metric."""
    rejected = pending_owner_rejections(acct)[:BATCH]
    if not rejected:
        return ""
    approved = db.drafts_by_status("approved", 6, acct=acct) + \
        db.drafts_by_status("published", 6, acct=acct)
    with db.connect() as c:
        (expired_n,) = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE account_id=? AND status='rejected'",
            (db._acct(acct),)).fetchone()
    out = [f"REJECTED BY THE OWNER ({len(rejected)} drafts — what they refused to publish):"]
    out += [_fmt(d) for d in rejected]
    if approved:
        out += ["", "APPROVED/QUEUED BY THE OWNER (contrast — what they accepted):"]
        out += [_fmt(d) for d in approved[:6]]
    out += ["",
            f"({int(expired_n) - len(rejected)} other rejected drafts expired "
            "unapproved after 3 days — queue hygiene, weaker signal, listed "
            "only for context)"]
    return "\n".join(out)


def _mark_learned(ids: list[int], acct: Optional[int] = None) -> None:
    for did in ids:
        d = db.get_draft(did, acct=acct)
        if not d or d["status"] != "rejected":
            continue
        meta = dict(d.get("meta") or {})
        meta["rejection_learned"] = True
        db.update_draft(did, acct=acct, meta_json=meta)


def run_reflection(cfg, acct: Optional[int] = None) -> dict:
    """One rejection-learning pass over the pending owner rejections.

    Rides brain.reflect() so every existing guarantee applies (secret
    sanitization, applied-edits journaling, conservative prompt). The
    material is passed explicitly — reflect's default MATERIALS lookup is
    per-process stateless about which drafts are pending.
    """
    from . import brain as brain_mod
    pending = pending_owner_rejections(acct)
    if not pending:
        return {"ok": True, "learned_from": 0, "reason": "nothing pending"}
    material = build_material(acct)
    res = brain_mod.reflect(cfg, "rejection",
                            payload={"material": material}, acct=acct)
    res["consolidated"] = _consolidate_rules(cfg, acct)
    _mark_learned([d["id"] for d in pending[:BATCH]], acct)
    res["learned_from"] = min(len(pending), BATCH)
    return res


# token-overlap dedupe for learned rules — the reflect prompt asks for one
# shared pattern but the model still emits near-copies per draft (live:
# 18 rules from 10 rejections, the same 3 lessons x5+). Rules cost prompt
# budget in brain_context; duplicates are pure bloat.
_OVERLAP = 0.55


def _tokens(text: str) -> set[str]:
    import re as _re
    return {t.lower() for t in _re.findall(r"[\w؀-ۿ]+", text)}


MERGE_SYSTEM = """You maintain a rule list for an AI content agent. The rules
below were learned from the owner's draft rejections — the LLM that wrote
them tends to restate the SAME lesson in different words (live: 18 rules,
3 actual lessons). Merge semantic duplicates: pick the clearest rule of
each cluster to KEEP, list the rest to retire. NEVER merge rules that
teach different behavior. Return STRICT JSON:
{"clusters": [{"keep": <rule id>, "retire": [<rule ids>]}], "solo": [<ids of
unmatched rules>]}
Every input id must appear exactly once across keep/retire/solo."""

LLM_MERGE_MIN = 8  # below this the token pass is enough; paraphrase
                   # clusters only matter once the list grows


def _token_pass(rules: list[dict]) -> list[int]:
    kept: list[dict] = []
    retired: list[int] = []
    for r in sorted(rules, key=lambda x: x["id"]):
        toks = _tokens(r["text"])
        if any(toks and _tokens(k["text"]) and
               len(toks & _tokens(k["text"])) / max(len(toks | _tokens(k["text"])), 1) >= _OVERLAP
               for k in kept):
            retired.append(r["id"])
        else:
            kept.append(r)
    return retired


def _llm_merge(cfg, rules: list[dict]) -> list[int]:
    """One bounded LLM call clustering PARAPHRASE duplicates. Every id must
    be accounted for exactly once and belong to the input set — anything
    else rejects the whole proposal (deterministic validation, LLM only
    proposes)."""
    from .llm import chat as llm_chat, extract_json, LLMError
    listing = chr(10).join(f"R{r['id']}: {r['text']}" for r in rules)
    raw = llm_chat(cfg.llm, system=MERGE_SYSTEM, user=listing,
                   temperature=0.0, json_mode=True)
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise LLMError("merge proposal not an object")
    def _rid(v) -> int:
        """The model echoes the listing's 'R72' format — accept both."""
        t = str(v).strip()
        if t[:1] in ("R", "r"):
            t = t[1:]
        return int(t)

    seen: set[int] = set()
    retired: list[int] = []
    valid = {r["id"] for r in rules}
    for cl in data.get("clusters") or []:
        if not isinstance(cl, dict):
            raise LLMError("bad cluster")
        keep = _rid(cl.get("keep"))
        drop = [_rid(x) for x in (cl.get("retire") or [])]
        for rid in [keep] + drop:
            if rid not in valid or rid in seen:
                raise LLMError(f"id {rid} unknown or repeated")
            seen.add(rid)
        retired.extend(drop)
    for rid in data.get("solo") or []:
        rid = _rid(rid)
        if rid not in valid or rid in seen:
            raise LLMError(f"solo id {rid} unknown or repeated")
        seen.add(rid)
    if seen != valid:
        raise LLMError("proposal does not account for every rule exactly once")
    return retired


def _consolidate_rules(cfg, acct: Optional[int] = None) -> int:
    """Retire duplicate rules among ACTIVE rejection/directive rules.
    Small lists: cheap token-overlap pass. Large lists (>= LLM_MERGE):
    one validated LLM clustering — paraphrases ('warn/scold/lecture' vs
    'cynical meta-takes scolding') share too few tokens for the cheap
    pass (live: 18 rules, 0 caught, all paraphrase dupes)."""
    from . import brain as brain_mod
    rules = [r for r in brain_mod.parse_rules(brain_mod.read("rules", acct))
             if r["status"] == "active" and r["source"] in ("rejection",
                                                            "directive")]
    retired: list[int] = []
    try:
        if len(rules) >= LLM_MERGE_MIN:
            # one retry: the merge call is one big thinking-mode pass and
            # can outlive a slow read window (live: ReadTimeout, fallback
            # fired, 0 merged — a retry usually lands it)
            for attempt in (1, 2):
                try:
                    retired = _llm_merge(cfg, rules)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    import time as _time
                    _time.sleep(3)
        else:
            retired = _token_pass(rules)
    except Exception as e:  # noqa: BLE001 — consolidation never breaks learning
        db.log("brain", f"rule consolidation fell back to token pass: {e}",
               level="warn")
        retired = _token_pass(rules)
    for rid in retired:
        brain_mod.retire_rule(rid, acct=acct)
    if retired:
        brain_mod.journal_append(
            "rejection", f"consolidated {len(retired)} duplicate learned "
            f"rules (kept {len(rules) - len(retired)} distinct)",
            [f"retired R{r}" for r in retired], acct=acct)
    return len(retired)


def maybe_reflect_async(cfg, acct: Optional[int] = None) -> bool:
    """Fire a reflection in a daemon thread once enough owner rejections
    pile up. Never blocks the caller (the reject tap must stay instant)."""
    try:
        pending = pending_owner_rejections(acct)
    except Exception as e:  # noqa: BLE001 — never break a reject tap
        db.log("brain", f"rejection-learn check failed: {e}", level="warn")
        return False
    if len(pending) < REFLECT_THRESHOLD:
        return False

    def _worker():
        try:
            res = run_reflection(cfg, acct)
            db.log("brain", f"rejection reflection: learned from "
                            f"{res.get('learned_from', 0)} rejected drafts")
        except Exception as e:  # noqa: BLE001 — background best-effort
            db.log("brain", f"rejection reflection failed: {e}", level="warn")

    threading.Thread(target=_worker, daemon=True,
                     name="brain-rejection-learn").start()
    return True
