"""POST /api/chat/draft with image — Write-chat candidate save carries media."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from fastapi.testclient import TestClient                          # noqa: E402
from openstanley.server.__main__ import app, MEDIA_DIR            # noqa: E402


def test_chat_draft_with_image():
    (MEDIA_DIR / "media_test_ok.png").write_bytes(b"\x89PNG test")
    try:
        with TestClient(app) as client:
            r = client.post("/api/chat/draft",
                            json={"text": "hello with media",
                                  "image": "media_test_ok.png"})
        assert r.status_code == 200, r.text
        did = r.json()["draft_id"]
        assert db.get_draft(did)["image"] == "media_test_ok.png"
    finally:
        (MEDIA_DIR / "media_test_ok.png").unlink(missing_ok=True)


def test_chat_draft_bad_image_name_rejected():
    with TestClient(app) as client:
        r = client.post("/api/chat/draft",
                        json={"text": "x", "image": "../evil.png"})
    assert r.status_code == 400


def test_chat_draft_missing_image_rejected():
    with TestClient(app) as client:
        r = client.post("/api/chat/draft",
                        json={"text": "x", "image": "nope.png"})
    assert r.status_code == 404
