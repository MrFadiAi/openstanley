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
}


# ---------- CRUD ----------

def list_loops() -> list[dict]:
    return db.get_setting(CUSTOM_LOOPS_KEY) or []


def _save(loops: list[dict]) -> None:
    db.set_setting(CUSTOM_LOOPS_KEY, loops)


def create_loop(name: str, source: str, param: str,
                interval_h: int = 24, draft_count: int = 1) -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("name required")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")
    param = (param or "").strip()[:120]
    if source in ("github_user", "x_topic") and not param:
        raise ValueError(f"{source} needs a param (user handle / topic)")
    interval_h = max(1, min(int(interval_h), 168))
    loop = {"id": f"cl_{secrets.token_hex(4)}",
            "name": name, "source": source, "param": param,
            "interval_h": interval_h,
            "draft_count": max(1, min(int(draft_count), 3)),
            "enabled": True, "last_run": None, "last_result": None,
            "created": datetime.now().isoformat(timespec="seconds")}
    loops = list_loops()
    loops.append(loop)
    _save(loops)
    db.log("custom_loops", f"created '{name}' ({source}"
                          f"{f' {param}' if param else ''}) every "
                          f"{interval_h}h")
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
    for l in list_loops():
        if not l.get("enabled"):
            continue
        last = l.get("last_run")
        if last:
            try:
                elapsed = (datetime.now()
                           - datetime.fromisoformat(last)).total_seconds()
                if elapsed < int(l.get("interval_h", 24)) * 3600:
                    continue
            except ValueError:
                pass
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
