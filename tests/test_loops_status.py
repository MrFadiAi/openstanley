"""GET /api/loops/status — last/next run per loop for the Write-page TaskRows."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["XOPENSTANLEY_NO_SCHEDULER"] = "1"  # no cron loop inside tests

from openstanley.core import db                                    # noqa: E402
db.init_db()

from fastapi.testclient import TestClient                       # noqa: E402
from openstanley.server.__main__ import app, _loops_status_data    # noqa: E402


def test_loops_status_endpoint():
    db.log("study", "study loop done: bank=12")  # seed one real last-run row
    with TestClient(app) as client:
        r = client.get("/api/loops/status")
        assert r.status_code == 200
        body = r.json()
        names = [lp["name"] for lp in body["loops"]]
        assert set(names) == {"import", "study", "create", "engage", "mentions",
                              "publish", "learn", "scan"}
        assert body["scheduler_running"] is False  # disabled for tests
        study = next(lp for lp in body["loops"] if lp["name"] == "study")
        assert study["last_run"] and study["last_status"] == "ok"
        assert "bank=12" in study["last_message"]
        assert study["next_run"] is None  # no scheduler → no next run
        assert set(study) == {"name", "last_run", "last_status",
                              "last_message", "next_run"}
    print("[ok] /api/loops/status: 7 loops, last-run from log, next-run None")


def test_voice_settings_roundtrip():
    with TestClient(app) as client:
        r = client.post("/api/settings", json={"voice_temperature": "experimental",
                                               "voice_formality": 80,
                                               "voice_emoji_density": 99})
        assert r.status_code == 200
        body = r.json()
        assert body["voice_temperature"] == "experimental"
        assert body["voice_formality"] == 80
        assert body["voice_emoji_density"] == 10  # clamped to max
        # invalid ladder value is rejected, not stored
        r2 = client.post("/api/settings", json={"voice_temperature": "meh"})
        assert r2.json()["voice_temperature"] == "experimental"
        # restore defaults
        client.post("/api/settings", json={"voice_temperature": "bold",
                                           "voice_formality": 50,
                                           "voice_emoji_density": 3})
    print("[ok] /api/settings: voice_* roundtrip + clamping + validation")
