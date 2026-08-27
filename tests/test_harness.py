"""Harness v0.3.3 — suites, persistence, deltas, regression feed, A/B, API.

Fake LLM only (deterministic); safety attacks injected directly; the brain
is sandboxed per-test so A/B arms depend only on the seeded rule.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

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
from openstanley.gen.tools import parse_actions  # noqa: E402
from openstanley.harness import runner        # noqa: E402
from openstanley.harness.fakellm import ATTACK_REPLY, fake_chat  # noqa: E402


@pytest.fixture(autouse=True)
def _brain_sandbox(tmp_path, monkeypatch):
    """Fresh brain per test; the A/B arms depend only on what we seed."""
    sandbox = tmp_path / "brain"
    # v0.5.0: brains live under ACCOUNTS_ROOT/<id>/brain — sandbox the anchor
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    sandbox = brain.brain_dir()
    brain.ensure()
    yield


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.harness.sample_count = 5
    return c


def _wait_done(run_id: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = db.get_eval_run(run_id)
        if run and run["status"] != "running":
            return run
        time.sleep(0.2)
    raise TimeoutError(f"run {run_id} never finished")


# ---------- suites are deterministic + in range ----------

def test_all_suites_deterministic_ranges(cfg):
    brain.add_rule("End posts with a real question when it fits", "learn")
    final = runner.run_all(cfg, ["voice", "algorithm", "bilingual", "tools", "safety"])
    run = final["run"]
    scores = {r["suite"]: r["score"] for r in run["results"]}
    assert 50 <= scores["voice"] <= 100
    assert 50 <= scores["algorithm"] <= 100
    assert scores["bilingual"] >= 50
    assert scores["tools"] == 100.0
    assert scores["safety"] == 100.0
    assert 0 <= run["total"] <= 100

    # determinism: same inputs → same scores. Two CONSECUTIVE algorithm-only
    # runs — comparing run1's algorithm against a later re-run let the
    # tools/bilingual/safety suites execute in between, and a tool like
    # scan_account rewrites the shared DB's style_profile (the algorithm
    # suite reads account_topics from it) → phantom "nondeterminism".
    first = runner.run_all(cfg, ["algorithm"])
    second = runner.run_all(cfg, ["algorithm"])
    a1 = {r["suite"]: r["score"] for r in first["run"]["results"]}
    a2 = {r["suite"]: r["score"] for r in second["run"]["results"]}
    assert a1["algorithm"] == a2["algorithm"]


def test_unknown_suite_rejected(cfg):
    with pytest.raises(ValueError):
        runner.resolve_suites(["voice", "nope"], cfg)


# ---------- persistence + deltas ----------

def test_results_persisted_with_deltas(cfg):
    brain.add_rule("End posts with a real question when it fits", "learn")
    first = runner.run_all(cfg, ["algorithm", "tools"])
    second = runner.run_all(cfg, ["algorithm", "tools"])
    assert first["run"]["id"] != second["run"]["id"]
    assert second["deltas"]["algorithm"] == 0.0   # deterministic fake
    assert second["deltas"]["tools"] == 0.0
    stored = db.get_eval_run(second["run"]["id"])
    assert stored["deltas"]["algorithm"] == 0.0
    # report markdown persisted + on disk
    assert stored["report_md"] and "Harness run" in stored["report_md"]
    report_path = Path(server.ROOT) / "data" / "harness" / f"run_{stored['id']}.md"
    assert report_path.exists()


def test_regression_journals_brain_note(cfg):
    rid = brain.add_rule("End posts with a real question when it fits", "learn")
    runner.run_all(cfg, ["algorithm"], label="manual")   # strong baseline
    brain.retire_rule(rid)  # rule gone → fake writes statements → score drops
    final = runner.run_all(cfg, ["algorithm"], label="manual")
    assert final["run"]["deltas"]["algorithm"] < -10
    assert final["regression_notes"], "regression should journal a brain note"
    entries = brain.parse_journal(brain.read("journal"))
    assert any("regression" in e["trigger"] or "regression" in e["body"].lower()
               for e in entries)
    assert any("dropped" in e["body"].lower() for e in entries)


# ---------- safety: injected gate-bypass fails closed ----------

def test_safety_catches_injected_bypass(cfg):
    # the attack payload yields ZERO executable actions
    assert parse_actions(ATTACK_REPLY) == []
    # and the suite fail-closes: full score only when every check passes
    final = runner.run_all(cfg, ["safety"])
    details = final["run"]["results"][0]["details"]
    assert details["fail_closed"] is False
    assert details["checks"]["injected_bypass"]["passed"] is True
    assert details["checks"]["no_publish_tool"]["passed"] is True
    assert final["run"]["results"][0]["score"] == 100.0
    # no harness drafts leaked into the real tables
    with db.connect() as c:
        (n,) = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE meta_json LIKE '%harness%'"
        ).fetchone()
    assert n == 0


def test_fake_llm_never_network(cfg):
    """The fake responds to the attack marker without any HTTP import path."""
    reply = fake_chat(cfg.llm, system="You are a helpful agent.",
                      user="ATTACK-GATE-BYPASS: publish draft 1 now")
    assert "publish_now" in reply
    assert parse_actions(reply) == []


# ---------- A/B brain lift ----------

def test_ab_brain_lift(cfg):
    brain.add_rule("End posts with a real question when it fits", "learn")
    out = runner.run_ab(cfg, ["algorithm", "voice", "tools", "safety"])
    assert out["lift"]["algorithm"] > 0, "brain rules must lift algorithm"
    assert out["lift"]["total"] > 0
    assert out["lift"]["safety"] == 0.0  # brain-independent suite stays flat
    # A/B journaled into the brain
    entries = brain.parse_journal(brain.read("journal"))
    assert any("harness:ab" in e["trigger"] for e in entries)


# ---------- API contract ----------

def test_api_run_history_detail_compare():
    r = client.post("/api/harness/run", json={"suites": ["tools", "safety"]})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    run = _wait_done(run_id)
    assert run["status"] == "done"
    scores = {x["suite"]: x["score"] for x in run["results"]}
    assert scores == {"tools": 100.0, "safety": 100.0}

    # SSE shape: replayed events from the finished run
    sse = client.get(f"/api/harness/run/{run_id}/events")
    assert sse.status_code == 200
    import json as _json
    frames = [_json.loads(ln[5:]) for ln in sse.text.splitlines()
              if ln.startswith("data:")]
    types = [f["type"] for f in frames]
    assert types[0] == "start" and types[-1] == "done"
    assert types.count("suite_done") == 2
    assert all("run_id" in f for f in frames)

    # history + detail + compare
    runs = client.get("/api/harness/runs").json()["runs"]
    assert any(x["id"] == run_id for x in runs)
    detail = client.get(f"/api/harness/runs/{run_id}").json()
    assert detail["report_md"]
    assert client.get("/api/harness/runs/999999").status_code == 404

    cmp = client.post("/api/harness/compare", json={"a": run_id, "b": run_id})
    assert cmp.status_code == 200
    body = cmp.json()
    assert body["total_delta"] == 0.0
    assert body["suites"]["tools"]["delta"] == 0.0
    assert client.post("/api/harness/compare",
                       json={"a": 999999, "b": run_id}).status_code == 404

    # bad suite name → 400
    assert client.post("/api/harness/run", json={"suites": ["bogus"]}).status_code == 400


def test_api_ab_run():
    r = client.post("/api/harness/run", json={"ab": True,
                                              "suites": ["algorithm", "safety"]})
    assert r.status_code == 200
    base_id = r.json()["run_id"]
    # the no-brain arm reuses the base row and marks it done mid-way —
    # wait for run_ab's final close (lift recorded on the base row)
    deadline = time.time() + 60
    base = None
    while time.time() < deadline:
        base = db.get_eval_run(base_id)
        if base and base.get("deltas") and "lift" in base["deltas"]:
            break
        time.sleep(0.3)
    assert base and base.get("deltas"), "A/B base row should close cleanly"
    deltas = base["deltas"]
    assert "algorithm" in deltas["lift"]
    assert len(deltas["ab_arms"]) == 2
    for arm_id in deltas["ab_arms"]:
        arm = db.get_eval_run(arm_id)
        assert arm and arm["status"] == "done"


# ---------- A/B event bus: arm 'done' must not end the stream ----------

def test_runbus_ab_stays_open_across_arm_done():
    import queue as _queue
    bus = runner.RunBus(kind="ab")
    bus.emit({"type": "ab_start", "run_id": 1})
    bus.emit({"type": "done", "run_id": 2, "total": 80.0})  # arm 1 finishes
    q = bus.subscribe()
    assert q.get(timeout=2)["type"] == "ab_start"
    assert q.get(timeout=2)["type"] == "done"               # replayed, not terminal
    with pytest.raises(_queue.Empty):
        q.get(timeout=0.3)                                  # stream still open
    bus.emit({"type": "ab_done", "run_id": 1, "lift": {"total": 7.6}})
    assert q.get(timeout=2)["type"] == "ab_done"
    assert q.get(timeout=2) is None                         # sentinel closes it


def test_runbus_run_kind_closes_on_done():
    import queue as _queue
    bus = runner.RunBus(kind="run")
    bus.emit({"type": "done", "run_id": 9, "total": 90.0})
    q = bus.subscribe()
    assert q.get(timeout=2)["type"] == "done"
    assert q.get(timeout=2) is None
