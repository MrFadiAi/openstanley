"""Harness runner — orchestrates suites, persists runs, emits progress events.

Events (JSON dicts, consumed as SSE frames by the server):
  start {run_id, suites, label, real_llm, use_brain}
  suite_start {run_id, suite}
  suite_done {run_id, suite, score, delta}
  done {run_id, total, deltas, report_path, regression_notes}
  error {run_id, message}

The per-run event bus buffers everything so a late SSE subscriber gets a full
replay, then tails live until the run finishes.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from ..core import db
from ..core.config import Config, load_config
from ..gen import llm as llm_mod
from . import fakellm
from .base import EvalContext
from .suites import voice_eval, algorithm_eval, bilingual_eval, tool_eval, safety_eval
from . import report as report_mod

SUITES: dict[str, Callable[[EvalContext], dict]] = {
    "voice": voice_eval.run,
    "algorithm": algorithm_eval.run,
    "bilingual": bilingual_eval.run,
    "tools": tool_eval.run,
    "safety": safety_eval.run,
}
SUITE_WEIGHTS = report_mod.SUITE_WEIGHTS


class RunBus:
    """Fan-out event bus for one run: buffer + live subscriber queues.

    kind="ab" buses carry a whole A/B pair — arm 'done' events do NOT close
    the stream; only ab_done (or error) does."""

    def __init__(self, kind: str = "run") -> None:
        self.kind = kind
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._subs: list[queue.Queue] = []
        self._closed = False

    def _terminal(self, evt: dict) -> bool:
        if evt.get("type") == "error":
            return True
        if self.kind == "ab":
            return evt.get("type") == "ab_done"
        return evt.get("type") == "done"

    def emit(self, evt: dict) -> None:
        with self._lock:
            self._events.append(evt)
            if self._terminal(evt):
                self._closed = True
            subs, closed = list(self._subs), self._closed
        for q in subs:
            q.put(evt)
        if closed:
            for q in subs:
                q.put(None)  # tail sentinel

    def subscribe(self) -> queue.Queue:
        """Replay buffered events, then tail live (None = stream over)."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            for evt in self._events:
                q.put(evt)
            if self._closed:
                q.put(None)
            else:
                self._subs.append(q)
        return q


# run_id → bus (server SSE streams subscribe here)
BUSES: dict[int, RunBus] = {}


def _bus(run_id: int, kind: str = "run") -> RunBus:
    with threading.Lock():
        if run_id not in BUSES:
            BUSES[run_id] = RunBus(kind)
        return BUSES[run_id]


def resolve_suites(requested: Optional[list[str]], cfg: Config) -> list[str]:
    """'all'/None → configured suites; unknown names raise ValueError."""
    if not requested or requested == ["all"]:
        return [s for s in cfg.harness.suites if s in SUITES]
    bad = [s for s in requested if s not in SUITES]
    if bad:
        raise ValueError(f"unknown suites {bad} — valid: {sorted(SUITES)}")
    return list(requested)


def run_all(cfg: Config, suites: Optional[list[str]] = None, *,
            use_brain: bool = True, real_llm: bool = False,
            label: str = "manual", bus: Optional[RunBus] = None,
            run_id: Optional[int] = None) -> dict:
    """Run the selected suites, persist everything, return the finalized run.

    `run_id` reuses a pre-created eval_runs row (server posts the id back
    before the worker thread starts)."""
    suite_names = resolve_suites(suites, cfg)
    real = bool(real_llm)
    llm_fn = llm_mod.chat if real else fakellm.fake_chat
    if run_id is None:
        run_id = db.add_eval_run(label=label, real_llm=real, use_brain=use_brain,
                                 config={"suites": suite_names,
                                         "sample_count": cfg.harness.sample_count})
    bus = bus or _bus(run_id)
    BUSES.setdefault(run_id, bus)
    bus.emit({"type": "start", "run_id": run_id, "suites": suite_names,
              "label": label, "real_llm": real, "use_brain": use_brain})
    db.log("harness", f"run #{run_id} started ({label}, suites={suite_names}, "
                      f"real_llm={real}, brain={'on' if use_brain else 'off'})")

    prev_run = None  # baseline for deltas (A/B rows never shift the baseline)
    prev_candidate = db.previous_eval_run(run_id)
    if prev_candidate and not label.startswith("ab:"):
        prev_run = prev_candidate
    prev_scores = {r["suite"]: r["score"]
                   for r in (prev_run or {}).get("results", [])}

    for name in suite_names:
        bus.emit({"type": "suite_start", "run_id": run_id, "suite": name})
        ctx = EvalContext(cfg=cfg, llm=llm_fn, real=real,
                          n=max(1, int(cfg.harness.sample_count)),
                          use_brain=use_brain, run_id=run_id, label=label)
        try:
            result = SUITES[name](ctx)
            score = round(float(result.get("score", 0)), 1)
            details = result.get("details") or {}
        except Exception as e:  # noqa: BLE001 — one suite failing must not
            score, details = 0.0, {"error": str(e)[:300]}  # kill the run
            db.log("harness", f"suite {name} crashed: {e}", level="error")
        db.add_eval_result(run_id, name, score, details)
        delta = (round(score - prev_scores[name], 1)
                 if name in prev_scores else None)
        bus.emit({"type": "suite_done", "run_id": run_id, "suite": name,
                  "score": score, "delta": delta})

    final = report_mod.finalize(run_id, prev_run)
    bus.emit({"type": "done", "run_id": run_id,
              "total": final["run"]["total"], "deltas": final["deltas"],
              "report_path": final["report_path"],
              "regression_notes": final["regression_notes"]})
    return final


def run_ab(cfg: Config, suites: Optional[list[str]] = None, *,
           real_llm: bool = False, bus: Optional[RunBus] = None,
           base_run_id: Optional[int] = None) -> dict:
    """A/B brain-lift: same suites with and without brain context.

    The base row is a pure pair marker: it stays 'running' until BOTH arms
    finished, then closes once with the lift — so a status poll can never
    observe a finished pair without its lift."""
    suite_names = resolve_suites(suites, cfg)
    if base_run_id is None:
        base_run_id = db.add_eval_run(label="ab:pair", real_llm=real_llm,
                                      use_brain=True,
                                      config={"suites": suite_names, "ab": True})
    base_id = base_run_id
    bus = bus or _bus(base_id, kind="ab")
    BUSES.setdefault(base_id, bus)
    bus.emit({"type": "ab_start", "run_id": base_id, "suites": suite_names})

    no_brain = run_all(cfg, suite_names, use_brain=False, real_llm=real_llm,
                       label="ab:no-brain", bus=bus)
    with_brain = run_all(cfg, suite_names, use_brain=True, real_llm=real_llm,
                         label="ab:with-brain", bus=bus)

    no_scores = {r["suite"]: r["score"] for r in no_brain["run"]["results"]}
    lift = {}
    for r in with_brain["run"]["results"]:
        s = r["suite"]
        if s in no_scores:
            lift[s] = round(r["score"] - no_scores[s], 1)
    lift["total"] = round(with_brain["run"]["total"] - no_brain["run"]["total"], 1)

    brain_on = True
    try:
        from ..gen import brain as brain_mod
        brain_on = brain_mod.has_meaningful_brain()
    except Exception:  # noqa: BLE001
        pass
    brain_state = ("meaningful brain present" if brain_on
                   else "brain still at seed stubs — lift not yet meaningful")
    note = f"harness A/B: brain lift per suite {lift} ({brain_state})."
    try:
        from ..gen import brain as brain_mod
        brain_mod.journal_append("harness:ab", note)
    except Exception:  # noqa: BLE001
        pass

    bus.emit({"type": "ab_done", "run_id": base_id,
              "no_brain_run_id": no_brain["run"]["id"],
              "with_brain_run_id": with_brain["run"]["id"], "lift": lift})
    # the base row is just the A/B marker — close it so history stays clean
    db.update_eval_run(base_id, status="done", total=lift.get("total"),
                       deltas_json={"ab_arms": [no_brain["run"]["id"],
                                                with_brain["run"]["id"]],
                                    "lift": lift})
    return {"no_brain": no_brain, "with_brain": with_brain, "lift": lift,
            "run_id": base_id}
