"""Slot stampede fix — mass approvals and cap bounces spread, never stack."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import slots as slots_mod                     # noqa: E402

CFG = Config()


def _clear():
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id >= 950000")


def test_nudge_free_walks_to_next_free_slot():
    taken = {(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + "T09:00"}
    busy = datetime.now() + timedelta(days=1)
    at = busy.replace(hour=9, minute=0, second=0, microsecond=0)
    got, why = slots_mod.nudge_free(at, CFG, taken)
    assert got != at, "must move off the taken 09:00"
    assert got.isoformat(timespec="minutes")[:16] not in taken
    assert why


def test_free_slot_passes_through_untouched():
    at = (datetime.now() + timedelta(days=2)).replace(hour=13, minute=0,
                                                      second=0, microsecond=0)
    got, why = slots_mod.nudge_free(at, CFG, set())
    assert got == at and why == ""


def test_mass_approval_spreads_via_tg_picker():
    _clear()
    from openstanley.integrations import telegram as tg
    # three drafts already approved at tomorrow 09:00 → picker must move on
    base = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    for i in range(3):
        d = db.add_draft(text=f"busy {i}", acct=1)
        db.update_draft(d, acct=1, status="approved",
                        scheduled_at=f"{base}T09:00:00")
    picked = tg._next_static_slot(CFG)
    assert picked[:10] != base or picked[11:16] != "09:00", picked
    _clear()


def test_taken_slots_reads_approved_only():
    _clear()
    d1 = db.add_draft(text="approved one", acct=1)
    db.update_draft(d1, acct=1, status="approved",
                    scheduled_at="2026-09-01T09:00:00")
    d2 = db.add_draft(text="still pending", acct=1)  # draft w/ no schedule
    taken = slots_mod.taken_slots()
    assert "2026-09-01T09:00" in taken
    _clear()
