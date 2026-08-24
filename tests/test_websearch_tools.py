"""Live-search tools — web (DDG), X (cookie, no API), trends, trend_post."""
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
from openstanley.gen import websearch, tools                       # noqa: E402

CFG = Config()


def test_web_search_live_returns_real_results():
    """Real DDG hit — proves the no-key pipeline works end to end."""
    res = websearch.web_search("openai news", limit=4)
    assert res and len(res) >= 2, "expected live results"
    assert all("title" in r and "url" in r for r in res)
    assert any("openai" in (r["title"] + r["snippet"]).lower() for r in res)


def test_web_search_failure_is_empty_not_crash(monkeypatch):
    import httpx as _hx
    def boom(*a, **k):
        raise _hx.ConnectError("down")
    monkeypatch.setattr(websearch.httpx, "get", boom)
    assert websearch.web_search("anything") == []


def test_tools_registered():
    for name in ("web_search", "x_search", "x_trends", "trend_post"):
        assert name in tools.TOOL_REGISTRY, name


def test_web_search_tool_shape():
    out = tools.execute_tool(CFG, "web_search", {"query": "ai agents", "limit": 3})
    assert out["ok"] is True and isinstance(out["results"], list)


def test_trend_post_drafts_from_findings(monkeypatch):
    monkeypatch.setattr(websearch, "web_search",
                        lambda q, limit=6: [{"title": "Big Launch",
                                             "snippet": "acme shipped agents v2 today, 10x speed",
                                             "url": "https://example.com/x"}])
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: '{"text": "acme shipped agents v2 today. 10x speed. my human is not impressed, he wants 100x"}')
    out = tools.execute_tool(CFG, "trend_post",
                             {"topic": "acme agents", "source": "web"})
    assert out["ok"] is True and out.get("draft_id")
    d = db.get_draft(out["draft_id"])
    assert d["status"] == "draft"
    assert d["meta"]["source"] == "trend-post"
    assert "10x" in d["text"]
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (out["draft_id"],))


def test_trend_post_no_findings_is_honest(monkeypatch):
    monkeypatch.setattr(websearch, "web_search", lambda q, limit=6: [])
    out = tools.execute_tool(CFG, "trend_post", {"topic": "zzz nothing"})
    assert out["ok"] is True and "nothing found" in (out.get("error") or "")
