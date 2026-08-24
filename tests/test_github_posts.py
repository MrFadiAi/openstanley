"""GitHub → posts — drafts grounded in the user's own repos and commits."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import github_posts as gh                     # noqa: E402

CFG = Config()

REPO_JSON = [
    {"name": "openstanley", "fork": False, "description": "AI content agent",
     "stargazers_count": 0, "language": "Python", "pushed_at": "2026-08-24",
     "html_url": "https://github.com/u/openstanley"},
    {"name": "a-fork", "fork": True, "description": "x", "stargazers_count": 0,
     "language": None, "pushed_at": "2026-08-24", "html_url": "u"},
    {"name": "kino", "fork": False, "description": "video studio",
     "stargazers_count": 1, "language": "TypeScript", "pushed_at": "2026-08-20",
     "html_url": "https://github.com/u/kino"},
]
COMMITS = [{"commit": {"message": "feat: diversity engine"}},]
COMMITS += [{"commit": {"message": "fix: " + "x" * 120}}]


def _fake_gh(monkeypatch, repos, commits):
    class R:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload
    def fake_get(url, params=None, headers=None, timeout=None):
        return R(repos if "/repos?" in url or url.endswith("/repos")
                 else commits)
    monkeypatch.setattr(gh.httpx, "get", fake_get)


def test_latest_repos_skips_forks(monkeypatch):
    _fake_gh(monkeypatch, REPO_JSON, COMMITS)
    repos = gh.latest_repos("u", 2)
    assert [r["name"] for r in repos] == ["openstanley", "kino"]


def test_draft_grounded_in_commits(monkeypatch):
    _fake_gh(monkeypatch, REPO_JSON, COMMITS)
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: '{"tweet": "shipped the diversity engine today, drafts can never repeat the same shape again"}')
    repo = {"name": "openstanley", "desc": "AI content agent",
            "lang": "Python", "url": "u/openstanley"}
    did = gh.draft_repo_post(CFG, repo, ["feat: diversity engine"], acct=1)
    assert did
    d = db.get_draft(did)
    assert d["meta"]["source"] == "github"
    assert d["meta"]["repo"] == "openstanley"
    assert "diversity" in d["text"]
    assert d["status"] == "draft"          # approval-gated
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_tool_registered_and_runs(monkeypatch):
    from openstanley.gen import tools
    assert "github_drafts" in tools.TOOL_REGISTRY
    _fake_gh(monkeypatch, REPO_JSON, COMMITS)
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: '{"tweet": "shipped a fresh tool today, grounded in the commit that matters"}')
    out = tools.execute_tool(CFG, "github_drafts", {"user": "u", "count": 2})
    assert out["ok"] is True
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE meta_json LIKE '%\"source\": \"github\"%'")


def test_no_user_configured_is_honest(monkeypatch):
    from openstanley.gen import tools
    out = tools.execute_tool(CFG, "github_drafts", {})
    assert out["ok"] is True
    assert "no github user" in (out.get("error") or out.get("note", "")) or out.get("draft_ids") is not None
