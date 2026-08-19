"""Run aggregation + markdown report + regression → brain journal feed."""
from __future__ import annotations

from pathlib import Path

from ..core import db
from ..gen import brain as brain_mod
from .suites import SUITE_WEIGHTS

HARNESS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "harness"
REGRESSION_THRESHOLD = 10.0  # points vs previous run → journal note

SUITE_ICONS = {"voice": "🎙", "algorithm": "📊", "bilingual": "🌐",
               "tools": "🛠", "safety": "🛡"}


def weighted_total(results: list[dict]) -> float:
    """Weighted mean of per-suite scores (missing suites just drop out)."""
    tw = ts = 0.0
    for r in results:
        w = SUITE_WEIGHTS.get(r["suite"], 1.0)
        tw += w
        ts += w * float(r["score"])
    return round(ts / tw, 1) if tw else 0.0


def compute_deltas(run: dict, prev: dict | None) -> dict:
    """Per-suite + total delta vs the previous (non-A/B) run."""
    if not prev:
        return {}
    prev_scores = {r["suite"]: r["score"] for r in prev.get("results", [])}
    deltas: dict = {}
    for r in run.get("results", []):
        if r["suite"] in prev_scores:
            deltas[r["suite"]] = round(r["score"] - prev_scores[r["suite"]], 1)
    if prev.get("total") is not None and run.get("total") is not None:
        deltas["total"] = round(run["total"] - prev["total"], 1)
    return deltas


def _arrow(d: float | None) -> str:
    if d is None:
        return ""
    return f" (▲{d})" if d >= 0 else f" (▼{abs(d)})" if d < 0 else ""


def build_markdown(run: dict, deltas: dict, prev: dict | None) -> str:
    lines = [
        f"# Harness run #{run['id']} — {run.get('label', 'manual')}",
        "",
        f"- **date**: {run.get('ts', '')}",
        f"- **llm**: {'real GLM' if run.get('real_llm') else 'fake (deterministic)'}"
        f" · brain: {'on' if run.get('use_brain') else 'off'}",
        f"- **total**: **{run.get('total', 0)}/100**{_arrow(deltas.get('total'))}"
        + (f"  ·  vs run #{prev['id']}" if prev else "  ·  (first run — no baseline)"),
        "",
        "| suite | score | Δ |",
        "|---|---|---|",
    ]
    for r in run.get("results", []):
        icon = SUITE_ICONS.get(r["suite"], "·")
        d = deltas.get(r["suite"])
        lines.append(f"| {icon} {r['suite']} | {r['score']}/100 | "
                     f"{_arrow(d) or '—'} |")
    lines.append("")
    for r in run.get("results", []):
        det = r.get("details") or {}
        lines.append(f"## {SUITE_ICONS.get(r['suite'], '·')} {r['suite']}")
        if det.get("note"):
            lines.append(f"> {det['note']}")
        # suite-specific highlights, kept compact
        for key, label in (("mean_voice_match", "voice-match mean"),
                           ("mean", "algorithm mean"), ("pct_strong", "% strong (≥65)"),
                           ("pct_weak", "% weak (<35)")):
            if key in det:
                lines.append(f"- {label}: {det[key]}")
        checks = det.get("checks")
        if isinstance(checks, dict):
            for name, c in checks.items():
                mark = "✅" if c.get("passed") else "❌"
                lines.append(f"- {mark} {name}: {c.get('note', '')}")
        scenarios = det.get("scenarios")
        if isinstance(scenarios, list):
            for sc in scenarios:
                mark = "✅" if sc.get("passed") else "❌"
                lines.append(f"- {mark} {sc['scenario']}: {sc['expected']}"
                             f" ← {sc.get('got')}")
        cases = det.get("cases")
        if isinstance(cases, list):
            for cs in cases:
                mark = "✅" if cs["detected"] == cs["requested"] else "❌"
                lines.append(f"- {mark} {cs['requested']} detected as "
                             f"{cs['detected']} ({cs['passed']} checks)")
        samples = det.get("samples")
        if isinstance(samples, list) and samples:
            lines.append("- samples:")
            for s in samples[:5]:
                text = (s.get("text") or "")[:90].replace("\n", " ")
                lines.append(f"  - “{text}”")
        lines.append("")
    return "\n".join(lines)


def save_report_file(run_id: int, markdown: str) -> Path:
    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    p = HARNESS_DIR / f"run_{run_id}.md"
    p.write_text(markdown, encoding="utf-8")
    return p


def journal_regressions(run: dict, deltas: dict) -> list[str]:
    """Regression >10 pts in any suite → brain journal note (self-improvement
    feed). Returns the notes written."""
    notes: list[str] = []
    for suite, d in deltas.items():
        if suite == "total" or d >= -REGRESSION_THRESHOLD:
            continue
        prev = next((r["score"] for r in run["results"] if r["suite"] == suite), None)
        note = (f"harness regression: suite '{suite}' dropped {abs(d):.1f} points "
                f"(now {prev}/100) in run #{run['id']} vs the previous run. "
                f"Review recent prompt, voice, or brain changes for what "
                f"broke {suite} quality.")
        try:
            brain_mod.journal_append("harness:regression", note)
            notes.append(note)
        except brain_mod.BrainSecurityError:
            pass  # never possible here, but never break the run over it
    return notes


def finalize(run_id: int, prev_run: dict | None) -> dict:
    """Compute totals + deltas + markdown report, persist, journal regressions."""
    run = db.get_eval_run(run_id)
    run["total"] = weighted_total(run["results"])
    deltas = compute_deltas(run, prev_run)
    run["deltas"] = deltas
    md = build_markdown(run, deltas, prev_run)
    path = save_report_file(run_id, md)
    db.update_eval_run(run_id, status="done", total=run["total"],
                       deltas=deltas, report_md=md)
    regression_notes = journal_regressions(run, deltas)
    db.log("harness", f"run #{run_id} done: total {run['total']}"
                   f"{_arrow(deltas.get('total'))} — report {path.name}")
    return {"run": db.get_eval_run(run_id), "deltas": deltas,
            "report_path": str(path), "regression_notes": regression_notes}
