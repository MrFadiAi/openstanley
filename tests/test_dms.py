"""DM triage — honest availability reporting per X mode."""
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
from openstanley.server.__main__ import app                        # noqa: E402
from openstanley.x.client import XClient, XDry                      # noqa: E402


def test_default_client_reports_unavailable():
    res = XClient.get_dms.__wrapped__ if hasattr(XClient.get_dms, "__wrapped__") else None
    # call through a dryrun instance (inherits the honest default)
    import asyncio
    out = asyncio.run(XDry().get_dms())
    assert out["available"] is False
    assert out["messages"] == []
    assert "cookie" in out["reason"].lower() or "api" in out["reason"].lower()


def test_dms_endpoint_shape():
    with TestClient(app) as client:
        r = client.get("/api/dms")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"available", "reason", "messages"}
