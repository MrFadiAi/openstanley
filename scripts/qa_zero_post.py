"""HARD RULE enforcement check — never publish on the named account.

The QA loop runs this every pass. Written once, correctly, because the
ad-hoc version kept rediscovering the mixed-timestamp trap (2026-08-31:
a space-format cutoff against T-format published_at made the counter
report 2 phantom violations — both pre-dated the kill switch).

Exit 0 = clean. Exit 1 = REAL violation, printed loudly.
Read-only: connects URI mode=ro, never writes.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The named account (owner rule 2026-08-30) and the moment the kill switch
# went in: immediately after #2758 shipped at 09:01:09. T-format compares
# against T-format published_at — no mixed formats, no phantom hits.
FROZEN_ACCOUNT = 2
SWITCH_INSTALLED_AT = "2026-08-30T09:02:00"


def main() -> int:
    db_path = os.environ.get("OPENSTANLEY_TEST_DB") \
        or ROOT / "data" / "openstanley.db"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, published_at, substr(text,1,60) t FROM drafts "
        "WHERE account_id=? AND status='published' "
        "AND published_at > ? ORDER BY published_at",
        (FROZEN_ACCOUNT, SWITCH_INSTALLED_AT)).fetchall()
    # OWNER OVERRIDE posts (owner directive 2026-08-31: explicit 'publish
    # #N' commands must ship) are marked in agent_log at publish time —
    # they are legitimate, never violations. Only AUTONOMOUS posts trip.
    overrides = {r["message"].split("draft ")[1].split(" ")[0]
                 for r in con.execute(
                     "SELECT message FROM agent_log WHERE loop='publish' "
                     "AND message LIKE 'OWNER OVERRIDE%'")}
    paused = con.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"publish_paused_{FROZEN_ACCOUNT}",)).fetchone()
    con.close()
    auto = [r for r in rows if str(r["id"]) not in overrides]
    owner = [r for r in rows if str(r["id"]) in overrides]
    for r in owner:
        print(f"owner-directed (legitimate): #{r['id']} "
              f"{r['published_at']}  {r['t']}")
    if auto:
        print(f"HARD RULE VIOLATION — {len(auto)} AUTONOMOUS post(s) on "
              f"account {FROZEN_ACCOUNT} since {SWITCH_INSTALLED_AT}:")
        for r in auto:
            print(f"  #{r['id']} {r['published_at']}  {r['t']}")
        return 1
    print(f"HARD RULE holds: 0 autonomous posts on account {FROZEN_ACCOUNT} "
          f"since {SWITCH_INSTALLED_AT} "
          f"(kill switch: {paused['value'] if paused else 'MISSING'}; "
          f"{len(owner)} owner-directed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
