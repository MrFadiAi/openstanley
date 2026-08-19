"""Harness CLI.

  python -m openstanley.harness run [--suites all|voice,algorithm,…] [--real] [--ab] [--samples N]
  python -m openstanley.harness report [--run ID]
"""
from __future__ import annotations

import argparse
import sys

from ..core import db
from ..core.config import load_config
from .runner import SUITES, run_all, run_ab


def _arrow(d: float | None) -> str:
    if d is None:
        return ""
    return f"  (▲{d:+.1f})" if d >= 0 else f"  (▼{d:+.1f})"


def _print_run(run: dict) -> None:
    print(f"\nrun #{run['id']}  {run.get('ts', '')}  [{run.get('label')}]  "
          f"llm={'real' if run.get('real_llm') else 'fake'}  "
          f"brain={'on' if run.get('use_brain') else 'off'}")
    deltas = run.get("deltas") or {}
    for r in run.get("results", []):
        bar = "█" * int(r["score"] // 5)
        print(f"  {r['suite']:<11} {r['score']:5.1f}  {bar:<21}"
              f"{_arrow(deltas.get(r['suite']))}")
    print(f"  {'TOTAL':<11} {run.get('total', 0):5.1f}  "
          f"(weights: voice .30, algo .25, bilingual/tools/safety .15)"
          f"{_arrow(deltas.get('total'))}")
    if run.get("report_md"):
        print(f"\nreport: {len(run['report_md'])} chars stored in eval_runs "
              f"+ data/harness/run_{run['id']}.md")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    db.init_db()
    if args.samples:
        cfg.harness.sample_count = args.samples
    suites = None if args.suites in (None, "all") else args.suites.split(",")
    if args.ab:
        out = run_ab(cfg, suites, real_llm=args.real)
        print("=== A/B brain-lift (with − without) ===")
        for suite, lift in out["lift"].items():
            sign = "+" if lift >= 0 else ""
            print(f"  {suite:<11} {sign}{lift:.1f}")
        _print_run(out["with_brain"]["run"])
        return 0
    final = run_all(cfg, suites, real_llm=args.real)
    _print_run(final["run"])
    for note in final.get("regression_notes", []):
        print(f"\n⚠ regression journaled to brain: {note}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    db.init_db()
    run = db.get_eval_run(args.run) if args.run else _last_done_run()
    if not run:
        print("no completed harness runs yet — try `python -m openstanley.harness run`")
        return 1
    _print_run(run)
    print()
    print(run.get("report_md") or "(no report stored)")
    return 0


def _last_done_run() -> dict | None:
    runs = db.list_eval_runs(limit=50)
    for r in runs:
        if r["status"] == "done" and not r["label"].startswith("ab"):
            return db.get_eval_run(r["id"])
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="openstanley.harness",
                                description="OpenStanley eval harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run eval suites")
    p_run.add_argument("--suites", default="all",
                       help=f"all or csv from {sorted(SUITES)}")
    p_run.add_argument("--real", action="store_true",
                       help="use the real LLM (GLM) instead of the deterministic fake")
    p_run.add_argument("--ab", action="store_true",
                       help="run twice — with and without brain context — and diff")
    p_run.add_argument("--samples", type=int, default=0,
                       help="override sample_count from config")
    p_run.set_defaults(fn=cmd_run)

    p_rep = sub.add_parser("report", help="print a stored run")
    p_rep.add_argument("--run", type=int, default=0, help="run id (default: last)")
    p_rep.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
