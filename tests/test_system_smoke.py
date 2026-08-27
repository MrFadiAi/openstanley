"""System smoke (v0.3.7) — REAL-wiring self-check, tested hermetically.

Covers openstanley/system/smoke.py + its server integration:
  * report shape: 7 probes, {name, ok, ms, detail}, green when all required pass
  * identity 401 → overall red; notifications flaky → amber only (warn, not fail)
  * per-probe timeout: one hanging probe fails alone, the run still completes
  * read-only by construction: post_tweet/post_thread are never touched
  * db probe writes+reads+deletes a _smoke key and leaves nothing behind
  * GET/POST /api/system/smoke: stored report, fresh run, 1-per-5-min rate limit
  * startup smoke runs as a background task — server serves / while it runs

All X clients are fakes; the LLM is a fake; nothing touches the network.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ["OPENSTANLEY_NO_SMOKE"] = "1"  # default off; the startup test re-enables it

from openstanley.core import db                                      # noqa: E402
db.init_db()

from openstanley.core.config import Config                           # noqa: E402

PROBE_NAMES = {"identity", "timeline_read", "search_read",
               "notifications_read", "llm", "brain", "db"}
REQUIRED = PROBE_NAMES - {"notifications_read"}


class FakeX:
    """Read-only fake X client: reads return fixtures, variants raise/slow.
    Any write method raises — smoke must never call one."""

    def __init__(self, fail_identity=False, fail_notifications=False,
                 hang_search=False):
        self.fail_identity = fail_identity
        self.fail_notifications = fail_notifications
        self.hang_search = hang_search
        self.calls: list[str] = []

    async def me(self) -> dict:
        self.calls.append("me")
        if self.fail_identity:
            raise RuntimeError("HTTP 401: Could not authenticate you")
        return {"username": "orbexai", "name": "Orb Exai", "followers": 421}

    async def user_tweets(self, username: str, limit: int = 100) -> list[dict]:
        self.calls.append("user_tweets")
        return [{"x_id": f"t{i}", "text": f"tweet {i}"} for i in range(min(limit, 5))]

    async def search(self, query: str, limit: int = 50) -> list[dict]:
        self.calls.append("search")
        if self.hang_search:
            await asyncio.sleep(5)
        return [{"x_id": f"s{i}", "text": f"result {i}"} for i in range(min(limit, 5))]

    async def mentions(self, limit: int = 5) -> list[dict]:
        self.calls.append("mentions")
        if self.fail_notifications:
            raise RuntimeError("HTTP 404: notifications endpoint flaked")
        return [{"x_id": "n1", "author_handle": "@fan", "text": "great post"}]

    async def post_tweet(self, *a, **kw):  # pragma: no cover — must never run
        raise AssertionError("smoke wrote to X")

    async def post_thread(self, *a, **kw):  # pragma: no cover — must never run
        raise AssertionError("smoke wrote to X")


def fake_llm(cfg, system: str = "", user: str = "", **kw) -> str:
    return "pong"


def failing_llm(cfg, system: str = "", user: str = "", **kw) -> str:
    raise RuntimeError("HTTP 401: bad key")


def run(cfg=None, x=None, llm=None):
    from openstanley.system.smoke import run_smoke
    return asyncio.run(run_smoke(cfg or Config(), x_client=x or FakeX(),
                                 llm=llm or fake_llm))


# ---------------- module: run_smoke ----------------

def test_all_pass_green_report_shape():
    rep = run()
    assert rep.ok is True
    assert rep.status == "green"
    names = {p.name for p in rep.probes}
    assert names == PROBE_NAMES
    for p in rep.probes:
        assert p.ok is True and p.ms >= 0 and p.detail, p
        assert set(p.to_dict()) == {"name", "ok", "ms", "detail", "warn"}
    d = rep.to_dict()
    assert d["ran_at"] and "T" in d["ran_at"]
    assert d["x_reads"] <= 8          # read budget respected
    assert d["x_reads"] == 4          # me + timeline + search + mentions
    ident = next(p for p in rep.probes if p.name == "identity")
    assert "orbexai" in ident.detail and "421" in ident.detail
    tl = next(p for p in rep.probes if p.name == "timeline_read")
    assert tl.detail.startswith("5 ")
    print("[ok] smoke: all-pass green, 7 probes, x_reads=4, shape right")


def test_identity_401_is_red():
    rep = run(x=FakeX(fail_identity=True))
    assert rep.ok is False
    assert rep.status == "red"
    ident = next(p for p in rep.probes if p.name == "identity")
    assert ident.ok is False and "401" in ident.detail
    print("[ok] smoke: identity 401 → red overall")


def test_notifications_fail_is_amber_warn_only():
    rep = run(x=FakeX(fail_notifications=True))
    assert rep.ok is True                  # warn-only never fails the run
    assert rep.status == "amber"
    notif = next(p for p in rep.probes if p.name == "notifications_read")
    assert notif.ok is False and notif.warn is True
    print("[ok] smoke: notifications flake → amber, not red")


def test_hanging_probe_times_out_independently():
    from openstanley.system import smoke
    old = smoke.PROBE_TIMEOUTS["search_read"]
    smoke.PROBE_TIMEOUTS["search_read"] = 0.2
    try:
        rep = run(x=FakeX(hang_search=True))
    finally:
        smoke.PROBE_TIMEOUTS["search_read"] = old
    assert rep.status == "red"             # search_read is a required probe
    s = next(p for p in rep.probes if p.name == "search_read")
    assert s.ok is False and "timeout" in s.detail.lower()
    others = [p for p in rep.probes if p.name != "search_read"]
    assert all(p.ok for p in others)       # one hang fails one probe, not the run
    print("[ok] smoke: hanging probe times out alone, run completes")


def test_smoke_never_writes_to_x():
    x = FakeX()
    run(x=x)
    assert set(x.calls) == {"me", "user_tweets", "search", "mentions"}
    print("[ok] smoke: read-only — no post/post_thread calls")


def test_db_probe_leaves_no_trace():
    run()
    assert db.get_setting("_smoke") is None  # key deleted after the roundtrip
    print("[ok] smoke: db probe cleans up its _smoke key")


def test_slow_llm_does_not_block_concurrent_probes():
    """The LLM call is sync httpx — it must run in a worker thread so a slow
    provider never stalls the event loop (startup smoke shares the server loop)."""
    import time as _time

    def slow_llm(cfg, system="", user="", **kw):
        _time.sleep(0.6)
        return "pong"

    rep = run(llm=slow_llm)
    assert rep.status == "green"
    dbp = next(p for p in rep.probes if p.name == "db")
    assert dbp.ms < 400, f"db probe took {dbp.ms}ms — sync LLM call blocked the loop"
    print("[ok] smoke: slow LLM runs off-loop (db probe not stalled)")


# ---------------- server: GET/POST + startup ----------------

def _clear_smoke_settings():
    with db.connect() as c:
        c.execute("DELETE FROM settings WHERE key IN ('smoke_last', 'smoke_last_run_epoch')")


def test_get_smoke_returns_last_stored_report():
    from fastapi.testclient import TestClient
    from openstanley.server.__main__ import app
    stored = {"ok": True, "status": "green", "ms": 640, "x_reads": 4,
              "ran_at": "2026-08-19T09:00:00", "probes": []}
    db.set_setting("smoke_last", stored)
    with TestClient(app) as client:
        r = client.get("/api/system/smoke")
        assert r.status_code == 200
        assert r.json() == stored
    print("[ok] GET /api/system/smoke: last stored report")


def test_post_smoke_runs_fresh_and_stores(monkeypatch):
    from fastapi.testclient import TestClient
    from openstanley.server.__main__ import app
    from openstanley.system import smoke as smoke_mod

    async def fake_run(cfg, x_client=None, llm=None):
        return smoke_mod.SmokeReport(
            ok=True, status="green", ms=12, x_reads=4,
            ran_at="2026-08-19T09:05:00",
            probes=[smoke_mod.ProbeResult("identity", True, 3, "ok")])

    monkeypatch.setattr(smoke_mod, "run_smoke", fake_run)
    db.set_setting("smoke_last_run_epoch", 0)  # no recent run
    with TestClient(app) as client:
        r = client.post("/api/system/smoke")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "green" and body["probes"][0]["name"] == "identity"
        assert db.get_setting("smoke_last")["ran_at"] == body["ran_at"]
        assert float(db.get_setting("smoke_last_run_epoch")) > time.time() - 60
    print("[ok] POST /api/system/smoke: fresh run stored")


def test_post_smoke_rate_limited_to_one_per_5min(monkeypatch):
    from fastapi.testclient import TestClient
    from openstanley.server.__main__ import app
    from openstanley.system import smoke as smoke_mod

    async def fake_run(cfg, x_client=None, llm=None):
        return smoke_mod.SmokeReport(
            ok=True, status="green", ms=1, x_reads=4, ran_at="t",
            probes=[smoke_mod.ProbeResult("identity", True, 1, "ok")])

    monkeypatch.setattr(smoke_mod, "run_smoke", fake_run)
    db.set_setting("smoke_last_run_epoch", time.time())  # just ran
    with TestClient(app) as client:
        r = client.post("/api/system/smoke")
    assert r.status_code == 429
    assert "5 min" in r.json()["detail"] or "rate" in r.json()["detail"].lower()
    db.set_setting("smoke_last_run_epoch", time.time() - 301)  # 5min+1s ago
    with TestClient(app) as client:
        r2 = client.post("/api/system/smoke")
    assert r2.status_code == 200
    print("[ok] POST /api/system/smoke: 429 within 5 min, ok after")


def test_identity_fail_logs_live_wiring_warning(monkeypatch):
    """Brief §2: identity probe failure must warn 'live X wiring broken'."""
    from fastapi.testclient import TestClient
    from openstanley.server.__main__ import app
    from openstanley.system import smoke as smoke_mod

    async def broken_identity(cfg, x_client=None, llm=None):
        return smoke_mod.SmokeReport(
            ok=False, status="red", ms=5, x_reads=1, ran_at="t",
            probes=[smoke_mod.ProbeResult("identity", False, 5,
                                          "RuntimeError: HTTP 401: bad cookies")])

    monkeypatch.setattr(smoke_mod, "run_smoke", broken_identity)
    db.set_setting("smoke_last_run_epoch", 0)
    with TestClient(app) as client:
        r = client.post("/api/system/smoke")
    assert r.status_code == 200 and r.json()["status"] == "red"
    with db.connect() as c:
        rows = c.execute("SELECT level, message FROM agent_log "
                         "WHERE loop='system' ORDER BY id DESC LIMIT 5").fetchall()
    warn = next((row for row in rows if "live X wiring broken" in row["message"]), None)
    assert warn is not None, f"no wiring-broken warn line in: {[r['message'] for r in rows]}"
    assert warn["level"] == "warn" and "Connect tab" in warn["message"]
    print("[ok] identity fail → 'live X wiring broken — check Connect tab' warn log")


def test_startup_smoke_does_not_block_boot(monkeypatch):
    """Server must serve / while the startup smoke is still running."""
    from fastapi.testclient import TestClient
    from openstanley.server.__main__ import app
    from openstanley.system import smoke as smoke_mod

    state = {"done": False}

    async def slow_run(cfg, x_client=None, llm=None):
        await asyncio.sleep(1.5)
        state["done"] = True
        return smoke_mod.SmokeReport(
            ok=True, status="green", ms=1500, x_reads=4, ran_at="t",
            probes=[smoke_mod.ProbeResult("identity", True, 1, "ok")])

    monkeypatch.setattr(smoke_mod, "run_smoke", slow_run)
    monkeypatch.delenv("OPENSTANLEY_NO_SMOKE", raising=False)
    _clear_smoke_settings()  # prove the report below comes from THIS startup run
    with TestClient(app) as client:
        t0 = time.monotonic()
        r = client.get("/api/health")       # must answer BEFORE smoke finishes
        assert r.status_code == 200
        assert time.monotonic() - t0 < 1.0  # boot not blocked by the 1.5s smoke
        assert state["done"] is False       # smoke genuinely still running
        deadline = time.monotonic() + 10
        while db.get_setting("smoke_last") is None and time.monotonic() < deadline:
            time.sleep(0.1)
        stored = db.get_setting("smoke_last")
        assert stored is not None and stored["status"] == "green"
    print("[ok] startup smoke: background task, / served during, report stored")


def test_identity_transient_failure_retries_once(monkeypatch):
    """A transient identity 404 (live 08-25..27: boot blip pinned a false
    red for an hour while a fresh client succeeded 2/2) must retry once and
    pass — and never write to X doing it."""
    from openstanley.system import smoke

    class FlakyOnceX(FakeX):
        def __init__(self):
            super().__init__()
            self.me_calls = 0

        async def me(self):
            self.calls.append("me")
            self.me_calls += 1
            if self.me_calls == 1:
                raise RuntimeError("NotFound: status: 404")
            return await super().me()

    monkeypatch.setattr(smoke, "IDENTITY_RETRY_S", 0)
    rep = run(x=FlakyOnceX())
    ident = next(p for p in rep.probes if p.name == "identity")
    assert ident.ok is True and "404" not in ident.detail


def test_identity_hard_failure_still_fails_after_retry(monkeypatch):
    from openstanley.system import smoke

    class DeadX(FakeX):
        async def me(self):
            self.calls.append("me")
            raise RuntimeError("NotFound: status: 404")

    monkeypatch.setattr(smoke, "IDENTITY_RETRY_S", 0)
    rep = run(x=DeadX())
    ident = next(p for p in rep.probes if p.name == "identity")
    assert ident.ok is False
