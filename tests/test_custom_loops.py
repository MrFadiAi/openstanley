"""Custom loops — owner-built automation (Loops page)."""
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

from openstanley.gen import custom_loops as cl                   # noqa: E402


def _clean():
    db.set_setting(cl.CUSTOM_LOOPS_KEY, [])


def test_create_list_toggle_delete():
    _clean()
    loop = cl.create_loop("Trending GitHub", "github_trending", "",
                          interval_h=24)
    assert loop["id"].startswith("cl_") and loop["enabled"] is True
    assert len(cl.list_loops()) == 1
    # trending needs no param; user/topic do
    cl.create_loop("My repos", "github_user", "mrfadiai")
    try:
        cl.create_loop("bad", "github_user", "")
        assert False, "missing param must raise"
    except ValueError:
        pass
    try:
        cl.create_loop("bad", "nope", "")
        assert False, "unknown source must raise"
    except ValueError:
        pass
    # toggle + due
    cl.update_loop(loop["id"], enabled=False)
    assert loop["id"] not in [l["id"] for l in cl.due_loops()]
    cl.update_loop(loop["id"], enabled=True)
    assert loop["id"] in [l["id"] for l in cl.due_loops()]
    # delete
    assert cl.delete_loop(loop["id"]) is True
    assert cl.delete_loop(loop["id"]) is False
    _clean()


def test_trending_parser(monkeypatch):
    """The trending page is scraped — the regex must pull owner/repo
    pairs and skip site chrome paths."""
    import openstanley.gen.custom_loops as m

    class _Page:
        status_code = 200

        def raise_for_status(self):
            return None
        text = ('<html><a href="/torvalds/linux">linux</a>'
                '<a href="/microsoft/markitdown">md</a>'
                '<a href="/features">chrome</a>'
                '<a href="/explore">more</a></html>')

    class _Repo:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"name": "linux", "description": "kernel",
                    "language": "C", "stargazers_count": 180000}

    def fake_get(url, **kw):
        return _Page() if "trending" in url else _Repo()

    monkeypatch.setattr(m.httpx, "get", fake_get)
    repos = m._trending_repos(limit=2)
    assert repos and repos[0]["name"] == "linux"
    assert repos[0]["stars"] == 180000
