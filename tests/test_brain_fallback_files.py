"""Brain reference docs never stay stubs — every scan writes all five."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.gen import brain                                  # noqa: E402

STATS = {"posts_scanned": 50, "avg_length_chars": 120.0,
         "vocabulary": {"top_terms": ["agents", "crypto", "shipping"]},
         "language_mix": {"ar": 0.6, "en": 0.4},
         "posting_times": {"best_hours": [9, 18]},
         "topics": {"ai agents": 12, "crypto builds": 8},
         "casing": {"pct_lowercase_start": 0.9},
         "emoji": {"per_post": 0.2},
         "punctuation": {"question": 0.1, "colon": 0.5, "ellipsis": 0.02}}


def test_fallback_writes_all_five_files():
    writes = brain._scan_fallback_files(STATS, {"stats": STATS})
    names = [n for n, _b in writes]
    assert set(names) >= {"niche-map", "audience-personas", "content-pillars",
                          "voice-cards", "competitor-notes"}
    for name, body in writes:
        assert "(OpenStanley writes this file itself" not in body, name
        assert len(body.splitlines()) >= 4, f"{name} too thin"


def test_pillars_prefer_weighted_topics():
    writes = dict(brain._scan_fallback_files(STATS, {"stats": STATS}))
    assert "ai agents" in writes["content-pillars"]
    assert "scan weight 12" in writes["content-pillars"]


def test_voice_cards_modes_from_punctuation():
    writes = dict(brain._scan_fallback_files(STATS, {"stats": STATS}))
    assert "Question mode" in writes["voice-cards"]
    assert "Colon-led mode" in writes["voice-cards"]
