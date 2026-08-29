"""Full-app-surface tools — the agent drives EVERYTHING OpenStanley does.

Owner directive (2026-08-30): "I want it really to be an agent that has
all capability of the openstanley app. It can do everything I ask — both
the agent app and Telegram agent." These tools close the gap between the
app's surface and the chat: status, approvals, rescheduling, editing,
loop execution, ideas, brain inspection, account switching.
"""
from __future__ import annotations

from ..core import db
from ..core.config import Config
from .tools import register, parse_when, _tool_get_schedule


def app_status(cfg: Config) -> dict:
    """ONE call for every status question: account, autopilot, today's
    caps, queue counts, next scheduled posts, brain size. The 'what's
    scheduled / what's happening / how are we doing' answer."""
    from ..core.safety import usage
    from . import autopilot as ap
    from . import brain as brain_mod
    me = db.get_me() or {}
    ap_st = ap.get_state()
    caps = usage()
    with db.connect() as c:
        (pend,) = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE status='draft'").fetchone()
        (appr,) = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE status='approved'").fetchone()
    sched = _tool_get_schedule(cfg)
    return {"ok": True,
            "account": {"id": db.active_account(),
                        "handle": me.get("username"),
                        "followers": me.get("followers")},
            "autopilot": {"enabled": ap_st.get("enabled"),
                          "ticks": ap_st.get("ticks"),
                          "phase": ap_st.get("phase")},
            "caps_today": {"posts": caps.get("posts", 0),
                           "replies": caps.get("replies", 0)},
            "queue": {"pending": pend, "approved_scheduled": appr,
                      "idea_bank": db.idea_count()},
            "schedule_next": sched.get("upcoming_first", [])[:5],
            "brain_active_rules": len([
                r for r in brain_mod.parse_rules(brain_mod.read("rules"))
                if r["status"] == "active"]),
            "mode": cfg.x.mode}


def approve_draft(cfg: Config, draft_id: int = 0, when: str = "") -> dict:
    """Approve a pending draft by id, optionally at a given time."""
    from . import slots as slots_mod
    from datetime import datetime
    d = db.get_draft(draft_id)
    if not d or d["status"] not in ("draft", "approved"):
        return {"ok": False, "error": f"no approvable draft #{draft_id}"}
    sched = d.get("scheduled_at")
    if when:
        parsed = parse_when(when)
        if not parsed:
            return {"ok": False, "error": f"could not parse when={when!r}"}
        from datetime import datetime as _dt
        sched, _why = slots_mod.nudge_free(
            _dt.fromisoformat(parsed), cfg, slots_mod.taken_slots())
        sched = sched.isoformat(timespec="seconds")
    elif sched is None:
        base = slots_mod.day_slots(cfg, datetime.now().date())
        sched = (slots_mod.nudge_free(base[0]["at"], cfg,
                 slots_mod.taken_slots())[0] if base else
                 datetime.now().isoformat(timespec="seconds"))
    db.update_draft(draft_id, status="approved", scheduled_at=sched)
    db.log("chat", f"draft {draft_id} approved via agent tool -> {sched}")
    return {"ok": True, "draft_id": draft_id, "scheduled_at": sched}


def reschedule_draft(cfg: Config, draft_id: int = 0,
                     when: str = "") -> dict:
    """Move an approved draft to a new time."""
    from . import slots as slots_mod
    d = db.get_draft(draft_id)
    if not d or d["status"] != "approved":
        return {"ok": False, "error": f"no approved draft #{draft_id}"}
    parsed = parse_when(when)
    if not parsed:
        return {"ok": False, "error": f"could not parse when={when!r}"}
    from datetime import datetime as _dt
    at = slots_mod.nudge_free(_dt.fromisoformat(parsed), cfg,
                              slots_mod.taken_slots())[0]
    at = at.isoformat(timespec="seconds")
    db.update_draft(draft_id, scheduled_at=at)
    return {"ok": True, "draft_id": draft_id, "scheduled_at": at}


def edit_draft(cfg: Config, draft_id: int = 0, text: str = "") -> dict:
    """Edit a draft's text in place."""
    d = db.get_draft(draft_id)
    if not d or d["status"] not in ("draft", "approved"):
        return {"ok": False, "error": f"no editable draft #{draft_id}"}
    if not text.strip():
        return {"ok": False, "error": "text required"}
    db.update_draft(draft_id, text=text)
    return {"ok": True, "draft_id": draft_id, "new_text": text[:200]}


def run_loop(cfg: Config, name: str = "") -> dict:
    """Run an agent loop NOW: study, create, engage, mentions, publish,
    learn, scan. (deep-train excluded here: minutes-long, use /train.)"""
    import asyncio
    valid = ("study", "create", "engage", "mentions", "publish", "learn",
             "scan")
    if name not in valid:
        return {"ok": False, "error": f"name must be one of {valid}"}
    from .agent import Agent
    a = Agent(cfg)
    res = asyncio.run(getattr(a, name)())
    return {"ok": True, "loop": name,
            "result": {k: v for k, v in res.items()
                       if isinstance(v, (str, int, float, bool))}}


def list_ideas(cfg: Config, limit: int = 6) -> dict:
    """The idea bank's top angles."""
    ideas = db.fresh_ideas(max(1, min(int(limit), 12)))
    return {"ok": True, "count": len(ideas),
            "ideas": [{"title": i["title"], "score": i["score"]}
                      for i in ideas]}


def brain_read(cfg: Config, part: str = "rules") -> dict:
    """Read the agent's own brain: rules, journal, strategies, instructions."""
    if part not in ("rules", "journal", "strategies", "instructions"):
        return {"ok": False,
                "error": "part must be rules|journal|strategies|instructions"}
    from . import brain as brain_mod
    return {"ok": True, "part": part,
            "content": brain_mod.read(part)[:3000]}


def switch_account(cfg: Config, account_id: int = 0) -> dict:
    """Switch the active account (or list accounts when id omitted)."""
    if not account_id:
        accs = db.list_accounts()
        return {"ok": True,
                "accounts": [{"id": a["id"], "handle": a.get("handle")}
                             for a in accs],
                "active": db.active_account()}
    if not db.set_active_account(int(account_id)):
        return {"ok": False, "error": f"no account {account_id}"}
    me = db.get_me(int(account_id)) or {}
    return {"ok": True, "active": int(account_id),
            "handle": me.get("username")}


def register_all() -> None:
    register("app_status", app_status)
    register("approve_draft", approve_draft)
    register("reschedule_draft", reschedule_draft)
    register("edit_draft", edit_draft)
    register("run_loop", run_loop)
    register("list_ideas", list_ideas)
    register("brain_read", brain_read)
    register("switch_account", switch_account)
