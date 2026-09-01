# OpenStanley v0.4.1 — Scheduler Clock Alignment (post at the RIGHT time, not just any time)

Real analytics show best-hours 9/13/18 from only 15 posts — thin data. The publish
loop schedules approved drafts into `post_times` slots (9/13/18) but there's a real
defect class the analytics just exposed: drafts approved mid-morning all pile into the
NEXT slot even when that slot is 8 minutes away (too soon for the audience), while a
better slot may be hours later. Slot choice is static config; it never adapts to what
the metrics engine now knows.

Make scheduling metrics-aware:

## 1. `openstanley/gen/slots.py` — smart slot picker
- `best_slots(cfg, now, k=3) -> [{hour, score, reason}]` — merges three signals:
  a. **metrics times** (real engagement-by-hour, weight 0.6) when ≥20 own posts,
     else heuristic base (current behavior)
  b. **spread**: penalize slots within 2h of the last published post (weight 0.25)
  c. **freshness**: prefer next 48h windows; slots in the past today roll to tomorrow
     (weight 0.15)
- `pick_slot(cfg, draft_kind, now) -> datetime` — replies/mentions: nearest sensible
  slot within 90 min (conversation decay), else next best_slots[0]; posts: best_slots[0]
  honoring spread. Deterministic, testable, logged with reason.

## 2. Wiring
- Replace static `post_times` scheduling in the approve/publish path with `pick_slot`
  (keep `post_times` as the fallback when metrics absent — zero behavior change on
  fresh installs). Config flag `[agent] smart_slots` default ON.
- Approval response + DraftCard meta now include `scheduled_reason`
  ("metrics peak 13:00 · 5h after last post").

## 3. UI
- Schedule panel on Calendar page: "smart slots" badge, per-day slot chips with score
  + reason tooltip, manual override still possible (drag/time input unaffected).
- Insights best-hours chart links to the same reason strings (single source).

## 4. Tests (hermetic)
- Signal math: metrics-override at ≥20 posts; heuristic fallback below; spread penalty;
  past-slot rolls to tomorrow; reply-window 90min vs post behavior; determinism
  (same inputs → same slot); approve path uses pick_slot (spy) with reason attached;
  config off → old static behavior bit-identical. ~11 tests → aim ~194.

Hard rules: hermetic; no new X reads at approval time (uses stored analytics only);
ONE new module + minimal touches; approval gate untouched.
