"""Eval suites — each takes an EvalContext, returns {"score": 0-100, "details": …}."""
from __future__ import annotations

ALL_SUITES = ("voice", "algorithm", "bilingual", "tools", "safety")

# weighted total: voice + algorithm dominate — they measure the core product
SUITE_WEIGHTS = {
    "voice": 0.30,
    "algorithm": 0.25,
    "bilingual": 0.15,
    "tools": 0.15,
    "safety": 0.15,
}
