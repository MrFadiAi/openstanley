"""GitHub → posts — draft from the user's own real repos.

Reads the public GitHub API (no key): latest pushed repos + their newest
commit messages, then grounds one on-voice post per repo in those facts.
Never invents a feature — the repo description and commits ARE the material.
"""
from __future__ import annotations

from typing import Optional

import httpx

from ..core import db
from ..core.config import Config

API = "https://api.github.com"
UA = {"User-Agent": "openstanley", "Accept": "application/vnd.github+json"}

# repos we never post about (this product itself posting about itself is fine,
# but forked/noise repos aren't the user's work)
SKIP_REPOS = set()


def latest_repos(user: str, count: int = 2) -> list[dict]:
    """The most recently pushed repos with a description or commits."""
    r = httpx.get(f"{API}/users/{user}/repos",
                  params={"sort": "pushed", "per_page": 15}, headers=UA,
                  timeout=15)
    r.raise_for_status()
    out = []
    for repo in r.json():
        if repo.get("fork") or repo["name"] in SKIP_REPOS:
            continue
        if repo["name"] == user:  # profile README repo
            continue
        out.append({"name": repo["name"],
                    "desc": repo.get("description") or "",
                    "stars": repo.get("stargazers_count", 0),
                    "lang": repo.get("language") or "",
                    "pushed_at": repo.get("pushed_at", ""),
                    "url": repo.get("html_url", "")})
        if len(out) >= count:
            break
    return out


def latest_commits(user: str, repo: str, count: int = 4) -> list[str]:
    """Newest commit subjects — the concrete 'what shipped' material."""
    r = httpx.get(f"{API}/repos/{user}/{repo}/commits",
                  params={"per_page": count}, headers=UA, timeout=15)
    if r.status_code != 200:
        return []
    return [c["commit"]["message"].split("\n")[0][:100] for c in r.json()]


def github_handle(cfg: Config, acct: Optional[int] = None) -> str:
    """The user's GitHub username: brain strategy file first, then config."""
    from . import brain as brain_mod
    try:
        strat = brain_mod.read("strategies", acct=acct) if hasattr(
            brain_mod, "read") else ""
        import re
        m = re.search(r"github(?:\.com/|:)?\s*([A-Za-z0-9-]{2,39})",
                      strat or "", re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return str(getattr(cfg.agent, "github_user", "") or "")


_GH_SYSTEM = (
    "You write ONE X post in the user's voice about THEIR OWN open-source "
    'project. Output STRICT JSON: {"tweet": "..."}. Under 240 chars. Ground '
    "it in a concrete fact from the repo description or the newest commits. "
    "The tone is a builder sharing their work, quietly proud, no hype words, "
    "no hashtags, no question mark at the end.")


def draft_repo_post(cfg: Config, repo: dict, commits: list[str],
                    acct: Optional[int] = None) -> Optional[int]:
    """One on-voice draft grounded in repo facts. Approval-gated as ever."""
    from .llm import chat, extract_json
    from . import voice as voice_mod
    from . import diversity as div
    from ..core.text import scrub_ai_punctuation

    own = div.recent_draft_texts(acct)
    material = (f"REPO: {repo['name']} ({repo.get('lang') or 'mixed'})" + chr(10)
                + f"DESCRIPTION: {repo['desc']}" + chr(10)
                + "NEWEST COMMITS:" + chr(10)
                + chr(10).join(f"- {c}" for c in commits))
    fmt = ("story", "open with the concrete thing that shipped, then why it "
           "matters to builders like the account's audience")
    vb = div.variety_block(own, fmt, div.question_budget(own))
    voice = voice_mod.voice_prompt_block()  # ACTIVE account voice+style
    user = (f"YOUR OWN PROJECT, just pushed:" + chr(10) + material[:1800]
            + chr(10) + f"USER VOICE: {str(voice)[:350]}" + vb
            + chr(10) + "Write the post now.")
    raw = chat(cfg.llm, _GH_SYSTEM, user, json_mode=True)
    data = extract_json(raw)
    text = scrub_ai_punctuation(
        (data.get("tweet") or "").strip()) if isinstance(data, dict) else ""
    if not text or div.too_similar(text, own):
        db.log("github", f"post for {repo['name']} rejected "
                        f"(empty or too similar to recent drafts)")
        return None
    image = None
    try:
        from . import quote_card
        image = quote_card.make_card(text)
    except Exception:  # noqa: BLE001
        image = None
    did = db.add_draft(text=text, kind="post", temperature="bold",
                       image=image, acct=db._acct(acct),
                       meta={"source": "github", "repo": repo["name"],
                             "repo_url": repo["url"],
                             "commits": commits[:3]})
    db.log("github", f"draft #{did} for {repo['name']}")
    return did


def run(cfg: Config, user: str, count: int = 2,
        acct: Optional[int] = None) -> list[int]:
    """Latest `count` pushed repos → one grounded draft each."""
    ids: list[int] = []
    repos = latest_repos(user, count)
    if not repos:
        db.log("github", f"no repos found for {user}")
        return ids
    for repo in repos:
        commits = latest_commits(user, repo["name"])
        did = draft_repo_post(cfg, repo, commits, acct)
        if did:
            ids.append(did)
    return ids
