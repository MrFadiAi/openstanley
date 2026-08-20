"""Steal-this-hook — pattern mining from niche winners + remix to draft."""
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
from openstanley.gen import hooks                                  # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402
from openstanley.server.__main__ import app                        # noqa: E402

CFG = Config()


def _seed_winners(n=3):
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE is_own=0")
        for i in range(n):
            c.execute("INSERT INTO posts (account_id, x_id, is_own, text, "
                      "author_handle, impressions, engagement) "
                      "VALUES (1,?,0,?,'nicheperson',10000,500)",
                      (f"w{i}", f"winner post number {i} about shipping fast"))


def _fake_llm(reply: str, monkeypatch):
    # chat is imported inside the functions at call time — patch the source
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: reply)


def test_extract_stores_and_dedupes(monkeypatch):
    db.set_setting(hooks.SETTING_KEY, [])
    _seed_winners()
    reply = '{"hooks": [{"pattern": "nobody talks about X", "why": "curiosity gap", "example": "nobody talks about cold DMs"}, {"pattern": "I stopped doing Y and everything changed", "why": "contrarian result", "example": "I stopped networking"}]}'
    _fake_llm(reply, monkeypatch)
    res = hooks.extract(CFG)
    assert res["added"] == 2 and res["total"] == 2
    ids = [h["id"] for h in hooks.list_hooks()]
    assert len(ids) == 2

    # re-run with an overlapping pattern → deduped
    reply2 = '{"hooks": [{"pattern": "nobody talks about Z now", "why": "x", "example": "y"}]}'
    _fake_llm(reply2, monkeypatch)
    res2 = hooks.extract(CFG)
    assert res2["added"] == 0 and res2["total"] == 2


def test_extract_without_winners_is_noop(monkeypatch):
    db.set_setting(hooks.SETTING_KEY, [])
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE is_own=0")
    res = hooks.extract(CFG)
    assert res["added"] == 0 and "no niche" in res.get("reason", "")


def test_remix_creates_draft(monkeypatch):
    db.set_setting(hooks.SETTING_KEY, [
        {"id": 7, "pattern": "nobody talks about X", "why": "gap",
         "example": "cold DMs"}])
    _fake_llm('{"text": "nobody talks about agent DM etiquette. my human learned the hard way", "kind": "post"}', monkeypatch)
    did = hooks.remix(CFG, 7)
    assert did
    d = db.get_draft(did)
    assert d["status"] == "draft"
    assert d["meta"]["source"] == "hook-remix"
    assert "agent DM" in d["text"]


def test_remix_unknown_hook(monkeypatch):
    assert hooks.remix(CFG, 999) is None


def test_endpoints_roundtrip(monkeypatch):
    db.set_setting(hooks.SETTING_KEY, [{"id": 3, "pattern": "p", "why": "w",
                                        "example": "e"}])
    _seed_winners()
    _fake_llm('{"hooks": []}', monkeypatch)
    with TestClient(app) as client:
        r = client.get("/api/hooks")
        assert r.status_code == 200 and len(r.json()["hooks"]) == 1
        r2 = client.post("/api/hooks/extract")
        assert r2.status_code == 200
        _fake_llm('{"text": "remixed", "kind": "post"}', monkeypatch)
        r3 = client.post("/api/hooks/3/remix")
        assert r3.status_code == 200 and r3.json()["draft_id"]
        r4 = client.post("/api/hooks/999/remix")
        assert r4.status_code == 404
