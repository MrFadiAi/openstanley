"""Daily digest v0.4.2 — assembly, rendering, webhook delivery, API, scheduler.

All hermetic: the digest reads DB/settings/brain only (no X, no LLM), the
brain is sandboxed into a tmp dir, HTTP is faked at the httpx seam, and
digest files land in a tmp dir via XOPENSTANLEY_DIGEST_DIR.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["XOPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

from openstanley.core import db               # noqa: E402
db.init_db()

import openstanley.server.__main__ as server  # noqa: E402

client = TestClient(server.app)

from openstanley.core.config import Config    # noqa: E402
from openstanley.gen import brain             # noqa: E402
from openstanley.gen import digest as digest_mod  # noqa: E402

TODAY = datetime.now().date().isoformat()
YESTERDAY = (datetime.now() - timedelta(days=1)).date().isoformat()
CFG = Config()


@pytest.fixture(autouse=True)
def _brain_sandbox(tmp_path, monkeypatch):
    """Digest journal/rules reads never touch the real data/brain/."""
    sandbox = tmp_path / "brain"
    monkeypatch.setattr(brain, "BRAIN_DIR", sandbox)
    monkeypatch.setattr(brain, "FILES_DIR", sandbox / "files")
    monkeypatch.setattr(brain, "PHOTOS_DIR", sandbox / "photos")
    brain.ensure()
    yield


@pytest.fixture(autouse=True)
def _digest_dir_sandbox(tmp_path, monkeypatch):
    """Digest files land in tmp, never the real data/digests/."""
    d = tmp_path / "digests"
    monkeypatch.setenv(digest_mod.DIGEST_DIR_ENV, str(d))
    yield d


class _FakeHttpx:
    """Captures webhook POSTs — no network, ever."""

    def __init__(self, status_code: int = 200):
        self.calls: list[tuple[str, dict]] = []
        self.status_code = status_code

    def post(self, url, json=None, timeout=None, **kw):  # noqa: A002 — httpx kw
        self.calls.append((url, dict(json or {})))

        class _R:
            text = "ok"

            def __init__(self, code):
                self.status_code = code

        return _R(self.status_code)


def _seed_populated_day() -> None:
    """One busy day in the fixture DB: loops, posts, rejections, learning."""
    with db.connect() as c:
        # exact-day accounting: clear today's test log rows + global queues
        c.execute("DELETE FROM agent_log WHERE substr(ts, 1, 10) = ?", (TODAY,))
        c.execute("DELETE FROM drafts")
        c.execute("DELETE FROM posts WHERE is_own = 1")
        c.execute("DELETE FROM seen_mentions")
        c.execute("DELETE FROM identity_snapshots WHERE substr(captured_at, 1, 10) >= ?",
                  (YESTERDAY,))
        c.execute("DELETE FROM ideas")

    for loop, n in (("study", 2), ("create", 1), ("autopilot", 3)):
        for _ in range(n):
            db.log(loop, "seeded loop run")
    db.log("voice", "voice lock rejected draft (score 40, reasons: corporate "
                    "phrase: 'delve'; length 340 outside persona band 15-280)",
           level="warn")
    db.log("voice", "voice lock rejected draft (score 45, reasons: corporate "
                    "phrase: 'delve')", level="warn")
    db.log("engage", "gate: rejected 4/6 reply targets — "
                     "stale 52h > 48h — hard reject ×3, off the niche map ×1")

    db.upsert_post({"x_id": "digest-p1", "author_handle": "me", "is_own": 1,
                    "created_at": f"{TODAY}T09:00:00",
                    "text": "the ugly version teaches you things",
                    "impressions": 2000, "likes": 40, "reposts": 6, "replies": 5})
    db.upsert_post({"x_id": "digest-p2", "author_handle": "me", "is_own": 1,
                    "created_at": f"{TODAY}T13:00:00",
                    "text": "ship the boring stack",
                    "impressions": 1000, "likes": 10, "reposts": 1, "replies": 1})
    d1 = db.add_draft(text="the ugly version teaches you things")
    db.update_draft(d1, status="published", x_id="digest-p1",
                    published_at=f"{TODAY}T09:00:05")
    d2 = db.add_draft(text="ship the boring stack")
    db.update_draft(d2, status="published", x_id="digest-p2",
                    published_at=f"{TODAY}T13:00:05")
    d3 = db.add_draft(text="solid question — boring on purpose", kind="reply",
                      meta={"source": "mention", "reply_to_x_id": "m-1"})
    db.update_draft(d3, status="published", x_id="digest-r1",
                    published_at=f"{TODAY}T10:00:05")
    db.add_draft(text="still waiting for approval one")
    db.add_draft(text="still waiting for approval two")

    with db.connect() as c:
        c.executemany(
            "INSERT INTO seen_mentions (x_id, author, text, created_at, "
            "first_seen, handled) VALUES (?,?,?,?,?,?)",
            [("m-1", "alice", "hey thoughts?", f"{TODAY}T09:30:00",
              f"{TODAY}T09:31:00", 1),
             ("m-2", "bob", "check this out", f"{TODAY}T10:30:00",
              f"{TODAY}T10:31:00", 0)])
        c.executemany(
            "INSERT INTO identity_snapshots (captured_at, followers) VALUES (?,?)",
            [(f"{YESTERDAY}T06:00:00", 1390),
             (f"{TODAY}T06:00:00", 1395),
             (f"{TODAY}T20:00:00", 1407)])
    db.add_idea("idea one", "angle", "one-liner", "study", 8.0)
    db.add_idea("idea two", "angle", "one-liner", "study", 6.0)

    brain.journal_append("reflect:chat", "Replies with questions outperform "
                          "statements in this niche.",
                          ["added R1: DO end posts with a question",
                           "retired R2",
                           "strategy: question-first replies [working]"])


# ---------- assembly ----------

def test_digest_assembly_populated():
    _seed_populated_day()
    d = digest_mod.build_digest(CFG, TODAY)

    did = d.did
    # the engage-gate rejection line itself proves the engage loop ran
    assert did["loops"] == {"study": 2, "create": 1, "autopilot": 3, "engage": 1}
    assert did["posts_published"]["count"] == 2
    assert did["posts_published"]["top"]["text"].startswith("the ugly version")
    assert did["posts_published"]["top"]["likes"] == 40
    assert did["replies_sent"] == 1
    assert did["voice_rejected"]["count"] == 2
    assert "corporate phrase" in did["voice_rejected"]["top_violation"]
    assert did["engage_rejected"]["count"] == 4
    assert "stale 52h" in did["engage_rejected"]["top_reason"]
    assert did["mentions"] == {"handled": 1, "pending": 1}
    assert did["mentions_handled_today"] == 1

    learned = d.learned
    assert learned["journal"][0]["trigger"] == "reflect:chat"
    assert "question" in learned["journal"][0]["summary"].lower()
    assert learned["rules_added"]["count"] == 1
    assert learned["rules_retired"] == 1
    assert learned["strategy_updates"] == ["question-first replies [working]"]

    needs = d.needs_you
    assert needs["pending_approvals"]["count"] == 2
    assert needs["pending_approvals"]["previews"][0] == "still waiting for approval one"
    assert needs["autopilot"]["enabled"] is False

    numbers = d.numbers
    assert numbers["followers_delta"] == 17  # 1407 - 1390 (yesterday's carry)
    # eng = (40+6+5 w/ 1:3:8 → 40 + 18 + 40) + (10 + 3 + 8) = 119 ; imp 3000
    assert abs(numbers["avg_engagement_rate"] - 119 / 3000) < 1e-4
    assert numbers["best_post"]["text"].startswith("the ugly version")

    assert d.tomorrow["ideas_remaining"] == 2
    assert len(d.tomorrow["slots"]) >= 1
    assert d.tomorrow["slots"][0]["reason"]


def test_digest_empty_day_is_honest():
    d = digest_mod.build_digest(CFG, "2001-01-01")
    assert d.did["posts_published"]["count"] == 0
    assert d.did["replies_sent"] == 0
    assert d.did["loops"] == {}
    assert d.learned["journal"] == []
    assert d.numbers["followers_delta"] is None
    assert d.numbers["avg_engagement_rate"] is None
    assert d.numbers["best_post"] is None
    md = digest_mod.render_markdown(d)
    assert "nothing shipped today" in md
    assert "no reflections today" in md


# ---------- rendering ----------

def test_render_markdown_shape():
    _seed_populated_day()
    d = digest_mod.build_digest(CFG, TODAY)
    md = digest_mod.render_markdown(d)
    assert md.startswith(f"# 📰 OpenStanley daily digest · {TODAY}")
    for head in ("## ✅ What I did", "## 🧠 What I learned", "## ⚠️ Needs you",
                 "## 📊 Numbers", "## 📅 Tomorrow"):
        assert head in md, head
    assert "study ×2" in md
    assert "still waiting for approval one" in md
    assert "followers +17" in md


def test_render_text_compact():
    _seed_populated_day()
    d = digest_mod.build_digest(CFG, TODAY)
    text = digest_mod.render_text(d)
    assert text.startswith(f"📰 OpenStanley daily digest · {TODAY}")
    assert "##" not in text          # no markdown syntax in the webhook body
    for head in ("✅ What I did", "🧠 What I learned", "⚠️ Needs you",
                 "📊 Numbers", "📅 Tomorrow"):
        assert head in text, head
    assert "study ×2" in text


def test_render_ar_exists():
    _seed_populated_day()
    d = digest_mod.build_digest(CFG, TODAY)
    text = digest_mod.render_text(d, "ar")
    md = digest_mod.render_markdown(d, "ar")
    for head in ("ما أنجزته", "ما تعلمته", "يحتاج قرارك", "الأرقام", "غداً"):
        assert head in text, head
        assert head in md, head
    assert "What I did" not in text


# ---------- webhook delivery ----------

def test_webhook_send_posts_text_payload(monkeypatch):
    _seed_populated_day()
    db.set_setting("digest_last", None)  # delivery state is per-test
    db.set_setting("digest_webhook_url", "https://relay.example/hook")
    fake = _FakeHttpx()
    monkeypatch.setattr(digest_mod, "httpx", fake)
    try:
        result = digest_mod.deliver(CFG, TODAY, lang="en", force=True)
        assert result["sent"] and result["ok"]
        assert len(fake.calls) == 1
        url, payload = fake.calls[0]
        assert url == "https://relay.example/hook"
        assert set(payload) == {"text"} and payload["text"].startswith("📰")
        assert "study ×2" in payload["text"]
    finally:
        db.set_setting("digest_webhook_url", "")


def test_webhook_absent_skips_send(monkeypatch):
    db.set_setting("digest_webhook_url", "")
    fake = _FakeHttpx()
    monkeypatch.setattr(digest_mod, "httpx", fake)
    result = digest_mod.deliver(CFG, TODAY, force=True)
    assert result["ok"] and not result["sent"]
    assert fake.calls == []


def test_deliver_stores_file_setting_and_dedupes(_digest_dir_sandbox):
    _seed_populated_day()
    db.set_setting("digest_last", None)  # dedupe counter starts clean
    db.set_setting("digest_webhook_url", "https://relay.example/hook")
    fake = _FakeHttpx()
    orig_httpx = digest_mod.httpx
    digest_mod.httpx = fake
    try:
        first = digest_mod.deliver(CFG, TODAY, force=False)
        assert first["sent"] and len(fake.calls) == 1
        path = _digest_dir_sandbox / f"{TODAY}.md"
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith(f"# 📰")
        last = db.get_setting("digest_last")
        assert last["day"] == TODAY and last["sent"] and last["status_code"] == 200

        # cron-style rerun same day without force → no duplicate POST
        second = digest_mod.deliver(CFG, TODAY, force=False)
        assert not second["sent"] and second["already_sent"]
        assert len(fake.calls) == 1
        # forced rerun (the "Send test digest" button) → POSTs again
        digest_mod.deliver(CFG, TODAY, force=True)
        assert len(fake.calls) == 2
    finally:
        digest_mod.httpx = orig_httpx
        db.set_setting("digest_webhook_url", "")


# ---------- API endpoints ----------

def test_api_digest_endpoints(monkeypatch):
    _seed_populated_day()
    r = client.get("/api/digest")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["day"] == TODAY and not data["stored"]
    assert data["markdown"].startswith("# 📰")
    assert data["text"] and data["text"].startswith("📰")

    fake = _FakeHttpx()
    monkeypatch.setattr(digest_mod, "httpx", fake)
    db.set_setting("digest_webhook_url", "https://relay.example/api")
    try:
        r = client.post("/api/digest/send", json={})
        assert r.status_code == 200, r.text
        assert r.json()["sent"] and len(fake.calls) == 1
        assert "relay.example" in fake.calls[0][0]
    finally:
        db.set_setting("digest_webhook_url", "")

    # stored day serves the file verbatim
    r = client.get("/api/digest", params={"day": TODAY})
    assert r.status_code == 200 and r.json()["stored"] is True
    assert r.json()["markdown"].startswith("# 📰")

    r = client.get("/api/digest/history")
    assert r.status_code == 200
    assert TODAY in r.json()["days"]
    assert client.get("/api/digest", params={"day": "not-a-day"}).status_code == 200


def test_settings_digest_fields():
    url = "https://api.telegram.org/bot123456:AAFF/sendMessage"
    r = client.post("/api/settings", json={"digest_webhook_url": url,
                                           "digest_hour": 21})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["digest_webhook_set"] is True
    assert "AAFF" not in data["digest_webhook_url"]      # token masked
    assert data["digest_webhook_url"].startswith("https://api.telegram.org/")
    assert data["digest_hour"] == 21
    # junk scheme ignored, clear works
    assert client.post("/api/settings", json={"digest_webhook_url": "ftp://x"}
                       ).json()["digest_webhook_set"] is True  # unchanged
    data = client.post("/api/settings", json={"digest_webhook_url": ""}).json()
    assert data["digest_webhook_set"] is False and data["digest_webhook_url"] == ""
    assert data["digest_hour"] == 21
    db.set_setting("agent_digest_hour", None)
    server.cfg.agent.digest_hour = 20


def test_scheduler_registers_digest_at_configured_hour(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    monkeypatch.setattr(AsyncIOScheduler, "start", lambda self: None)

    def _hour(sched) -> str:
        job = sched.get_job("digest")
        assert job is not None, "digest job must register"
        return next(str(f) for f in job.trigger.fields if f.name == "hour")

    try:
        db.set_setting("agent_digest_hour", None)
        server.cfg.agent.digest_hour = 9
        assert _hour(server.start_scheduler()) == "9"
        # db setting wins over the config file value
        db.set_setting("agent_digest_hour", 21)
        assert _hour(server.start_scheduler()) == "21"
    finally:
        db.set_setting("agent_digest_hour", None)
        server.cfg.agent.digest_hour = 20
