"""OpenStanley harness — evaluation + quality measurement.

Runs the five eval suites (voice, algorithm, bilingual, tools, safety) against
a configurable LLM (deterministic fake in tests/dev, real GLM with --real),
persists every run to SQLite (eval_runs / eval_results), streams progress as
SSE events, and produces markdown reports with deltas vs the previous run.

The Brain integration is two-fold:
- a suite regressing >10 points writes a brain journal note (self-improvement)
- A/B mode runs every suite with and without brain context to prove lift
"""
from .runner import run_all, run_ab, SUITES, SUITE_WEIGHTS  # noqa: F401
