"""Custom loops — owner-built automation on the Loops page.

Owner ask 2026-09-13: 'there is no button to make my own loop — I want
for example a loop for trending GitHub repos.' A custom loop = a source
(gitHub trending, a GitHub user, an X topic) + a cadence + on/off. The
runner drafts from the source into the approval queue like every other
loop — nothing ships without the owner's tap.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import httpx

from ..core import db
from ..core.config import Config

CUSTOM_LOOPS_KEY = "custom_loops"     # list[dict]
MIN_INTERVAL_MIN = 30                  # the runner ticks; loops space off it

SOURCES = {
    "github_trending": "GitHub trending repos (daily)",
    "github_user": "A GitHub user's latest pushed repos",
    "x_topic": "An X topic — what's being said right now",
    "web_news": "Latest news on a topic (web search)",
}


# ---------- CRUD ----------

def list_loops() -> list[dict]:
    return db.get_setting(CUSTOM_LOOPS_KEY) or []


def _save(loops: list[dict]) -> None:
    db.set_setting(CUSTOM_LOOPS_KEY, loops)


def create_loop(name: str, source: str, param: str,
                interval_h: int = 24, draft_count: int = 1,
                at_hour: Optional[int] = None,
                link_first_reply: bool = False,
                instruction: str = "") -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("name required")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")
    param = (param or "").strip()[:120]
    needs_param = ("github_user", "x_topic", "web_news")
    if source in needs_param and not param:
        raise ValueError(f"{source} needs a param (user handle / topic)")
    interval_h = max(1, min(int(interval_h), 168))
    at_hour = int(at_hour) if at_hour is not None else None
    if at_hour is not None and not (0 <= at_hour <= 23):
        at_hour = None
    loop = {"id": f"cl_{secrets.token_hex(4)}",
            "name": name, "source": source, "param": param,
            "interval_h": interval_h,
            "draft_count": max(1, min(int(draft_count), 3)),
            "at_hour": at_hour,
            "link_first_reply": bool(link_first_reply),
            "instruction": (instruction or "").strip()[:600],
            "enabled": True, "last_run": None, "last_result": None,
            "created": datetime.now().isoformat(timespec="seconds")}
    loops = list_loops()
    loops.append(loop)
    _save(loops)
    when = (f"daily at {at_hour:02d}:00" if at_hour is not None
            else f"every {interval_h}h")
    db.log("custom_loops", f"created '{name}' ({source}"
                          f"{f' {param}' if param else ''}) {when}"
                          f"{f' +link-first-reply' if link_first_reply else ''}")
    return loop


def update_loop(loop_id: str, **fields) -> Optional[dict]:
    loops = list_loops()
    for i, l in enumerate(loops):
        if l["id"] == loop_id:
            for k in ("name", "param", "interval_h", "draft_count",
                      "enabled"):
                if k in fields and fields[k] is not None:
                    loops[i][k] = fields[k]
            _save(loops)
            return loops[i]
    return None


def delete_loop(loop_id: str) -> bool:
    loops = list_loops()
    kept = [l for l in loops if l["id"] != loop_id]
    if len(kept) == len(loops):
        return False
    _save(kept)
    return True


def due_loops() -> list[dict]:
    out = []
    now = datetime.now()
    for l in list_loops():
        if not l.get("enabled"):
            continue
        at_hour = l.get("at_hour")
        last = l.get("last_run")
        try:
            last_dt = datetime.fromisoformat(last) if last else None
        except ValueError:
            last_dt = None
        if at_hour is not None:
            # daily at HH:00 — due inside that hour, once per day
            if now.hour != int(at_hour):
                continue
            if last_dt and last_dt.date() == now.date():
                continue
        else:
            if last_dt and (now - last_dt).total_seconds() < \
                    int(l.get("interval_h", 24)) * 3600:
                continue
        out.append(l)
    return out


# ---------- sources ----------

def _trending_repos(limit: int = 3) -> list[dict]:
    """GitHub trending (daily) — the page is the only source; the API has
    no trending endpoint. Repo details come from the normal API."""
    r = httpx.get("https://github.com/trending?since=daily", timeout=15,
                  headers={"User-Agent": "openstanley",
                           "Accept": "text/html"})
    r.raise_for_status()
    full_names = list(dict.fromkeys(re.findall(
        r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', r.text)))[:limit * 3]
    out = []
    for full in full_names:
        if "/" not in full or full.count("/") != 1:
            continue
        try:
            d = httpx.get(f"https://api.github.com/repos/{full}",
                           timeout=10,
                           headers={"User-Agent": "openstanley"}).json()
            if d.get("description"):
                out.append({"name": d["name"], "full_name": full,
                            "desc": d["description"][:200],
                            "lang": d.get("language") or "mixed",
                            "stars": d.get("stargazers_count", 0)})
        except Exception:  # noqa: BLE001 — one bad repo never stops the batch
            continue
        if len(out) >= limit:
            break
    return out


def run_custom_loop(cfg: Config, loop: dict) -> dict:
    """Draft from the loop's source into the approval queue."""
    from . import github_posts as gh
    src, param = loop.get("source"), loop.get("param", "")
    n = int(loop.get("draft_count", 1))
    ids: list[int] = []
    note = ""
    try:
        if src == "github_trending":
            repos = _trending_repos(limit=n)
            for repo in repos[:n]:
                commits = gh.latest_commits(repo["full_name"].split("/")[0],
                                            repo["name"]) if gh else []
                did = gh.draft_repo_post(cfg, repo, commits)
                if did:
                    ids.append(did)
                    _apply_loop_extras(did, loop, repo)
            note = f"{len(repos)} trending repos → {len(ids)} draft(s)"
        elif src == "github_user":
            ids = gh.run(cfg, param, count=n)
            note = f"@{param} repos → {len(ids)} draft(s)"
        elif src == "x_topic":
            from . import websearch
            from . import trend_scout as ts
            found = websearch.x_search(cfg, param, limit=8)
            did = ts.draft_from_findings(cfg, found)
            if did:
                ids.append(did)
            note = f"x:{param} → {len(ids)} draft(s)"
        elif src == "web_news":
            from . import websearch
            from . import trend_scout as ts
            results = websearch.web_search(cfg, f"latest {param} news", 6)
            items = [str(r.get("title", "")) + " - " +
                     str(r.get("snippet", ""))[:150]
                     for r in (results.get("results") or [])][:6]
            did = ts.draft_from_findings(cfg, items)
            if did:
                ids.append(did)
            note = f"news:{param} → {len(ids)} draft(s)"
        else:
            note = f"unknown source {src}"
    except Exception as e:  # noqa: BLE001 — the loop logs and stays alive
        note = f"failed: {type(e).__name__}: {e}"[:200]
        db.log("custom_loops", f"loop '{loop['name']}' {note}", level="warn")
    update_loop(loop["id"], last_run=datetime.now().isoformat(
        timespec="seconds"), last_result=note)
    if ids:
        db.log("custom_loops", f"loop '{loop['name']}': {note}")
        try:
            from ..integrations import telegram as tg
            if tg.is_enabled():
                tg.notify_new_drafts(ids)
        except Exception:  # noqa: BLE001
            pass
    return {"draft_ids": ids, "note": note}


def run_due(cfg: Config) -> list[dict]:
    """One scheduler tick: run every due enabled loop."""
    return [run_custom_loop(cfg, l) for l in due_loops()]


def _apply_loop_extras(draft_id: int, loop: dict, repo: dict) -> None:
    """Owner-taught extras on a fresh loop draft: the repo link ships as
    the FIRST REPLY when the loop was taught that way (owner 2026-09-13:
    'draft a post from it and include the repo link in the first
    comment'), and the raw instruction rides in meta for the card."""
    d = db.get_draft(draft_id)
    if not d:
        return
    meta = d.get("meta") or {}
    changed = False
    if loop.get("link_first_reply") and repo.get("full_name"):
        link = f"https://github.com/{repo['full_name']}"
        text = d["text"]
        if link in text:
            text = text.replace(link, "").strip()
            db.update_draft(draft_id, text=text)
        meta["link_reply"] = link
        changed = True
    if loop.get("instruction"):
        meta["loop_instruction"] = loop["instruction"][:400]
        changed = True
    if changed:
        db.update_draft(draft_id, meta_json=meta)


PARSE_SYSTEM = (
    "You turn ONE owner instruction into a custom loop config. "
    "Sources: github_trending (GitHub daily trending repos), github_user "
    "(a GitHub user's repos), x_topic (what X says about a topic), "
    "web_news (latest news via web search). Return STRICT JSON: "
    '{"name": "short loop name", "source": "one of the four", '
    '"param": "username or topic (empty for github_trending)", '
    '"at_hour": 11, "interval_h": 24, "draft_count": 1, '
    '"link_first_reply": true}. at_hour: the hour when the owner named '
    "a daily time (else null). interval_h: fallback cadence when no time "
    "was named. draft_count: posts per run, max 3, default 1. "
    "link_first_reply: true ONLY when the owner explicitly wants the "
    "link as the first comment/reply under the post; false when they "
    "want it in the body or did not say.")


def parse_instruction(cfg: Config, instruction: str) -> dict:
    """Natural-language loop builder: the owner TEACHES the agent ('every
    day at 11am get me the most trending GitHub repo and draft a post,
    link in the first comment') and this turns it into a loop config."""
    from .llm import chat as llm_chat, extract_json, LLMError
    raw = llm_chat(cfg.llm, system=PARSE_SYSTEM,
                    user=f"INSTRUCTION:{chr(10)}{instruction}",
                    temperature=0.2, json_mode=True)
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise LLMError("loop parse returned non-object")
    return data
