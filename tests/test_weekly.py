"""Weekly owner report — the Friday wrap."""
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

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import weekly as wk                           # noqa: E402


def test_weekly_report_contents():
    """One message: what shipped, what it earned, what the brain learned,
    what awaits approval — all from real DB state."""
    from datetime import datetime as _dt
    did = db.add_draft(text="weekly probe shipped post zebra", acct=1,
                       status="published")
    db.update_draft(did, x_id="wk-probe-1",
                    published_at=_dt.now().isoformat(timespec="seconds"))
    try:
        text = wk.build_weekly(Config())
        assert "Weekly Report" in text
        assert "published" in text          # shipped section
        assert "brain learned" in text      # learning section
        assert "Followers" in text          # account state
    finally:
        with db.connect() as c:
            c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_weekly_delivers_via_tg(monkeypatch):
    import openstanley.integrations.telegram as tg
    sent = []
    monkeypatch.setattr(tg, "is_enabled", lambda: True)
    monkeypatch.setattr(tg, "notify", lambda text, **kw: sent.append(text))
    ok = wk.push_weekly(Config())
    assert ok and len(sent) == 1
    assert "Weekly Report" in sent[0]
