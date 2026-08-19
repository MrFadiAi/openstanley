"""Smart slot picker v0.4.1 — post at the RIGHT time, not just any time.

The static post_times cadence (9/13/18) never adapts: a draft approved at
12:52 piles into the 13:00 slot 8 minutes before it, while the audience's
real peak may be hours away. This module scores candidate slots from three
signals and picks deterministically:

  metrics   (0.60) real engagement-by-hour once >=20 own posts exist, else
                   the configured post_times heuristic — fresh installs
                   keep their current behavior
  spread    (0.25) penalize slots within 2h of the LAST published post —
                   bursts read as spam and split the audience
  freshness (0.15) prefer windows in the next 48h; a slot whose time
                   already passed today rolls to tomorrow

Reads stored analytics only (posts table) — never X, never LLM, safe to
call inside the approve path. Reason strings are the single source shared
by the approve response, draft meta, Calendar slot chips, and the Insights
best-hours captions.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

from ..core import db
from ..core.config import Config

W_METRICS = 0.60
W_SPREAD = 0.25
W_FRESHNESS = 0.15
SPREAD_HOURS = 2.0          # slots closer than this to the last post lose spread score
FRESH_WINDOW_H = 48.0       # freshness decays linearly to zero across this window
REPLY_WINDOW_MIN = 90       # conversation replies decay — ride only slots inside this
HORIZON_DAYS = 2            # occurrences considered: today, tomorrow, day after
TOP_K = 3
MAX_CANDIDATES = 64         # pick_slot's reply window needs every occurrence scored


# ---------- signals ----------

def _signals(cfg: Config) -> tuple[list[tuple[int, int]], str, dict[int, float]]:
    """(candidates, source, base scores) — candidates are (hour, minute).

    Real metrics peaks win once >= REAL_TIMES_MIN_POSTS own posts exist;
    below that the configured post_times ARE the candidates (heuristic,
    i.e. exactly the slots the static scheduler would have used).
    """
    from . import metrics as metrics_mod  # lazy: keeps module import side-effect free
    data = metrics_mod.times_of_day()
    if data["source"] == "real":
        avgs = {h["hour"]: h["avg_engagement"] for h in data["hours"]}
        peaks = list(data["best_hours"])
        top = max((avgs.get(h, 0) for h in peaks), default=0.0)
        base = {h: (avgs.get(h, 0) / top) if top > 0 else 1.0 for h in peaks}
        return [(h, 0) for h in peaks], "real", base
    candidates: list[tuple[int, int]] = []
    for t in (cfg.agent.post_times or ["09:00"]):
        parts = str(t).split(":")
        try:
            hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            continue
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            candidates.append((hh, mm))
    candidates = sorted(set(candidates)) or [(9, 0)]
    return candidates, "heuristic", {h: 1.0 for h, _ in candidates}


def source(cfg: Config) -> str:
    """'real' once metrics drive slot choice, else 'heuristic'."""
    return _signals(cfg)[1]


def last_published_at() -> Optional[datetime]:
    """When the most recent own post went out (posts table, first-seen truth)."""
    with db.connect() as c:
        row = c.execute(
            "SELECT created_at FROM posts WHERE is_own=1 AND created_at IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row or not row["created_at"]:
        return None
    try:
        dt = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=None)  # everything here is naive local time


def _spread_score(dt: datetime, last: Optional[datetime]) -> float:
    """1.0 outside the spread zone, linear to 0 at the last post itself."""
    if last is None:
        return 1.0
    gap_h = (dt - last).total_seconds() / 3600
    if gap_h >= SPREAD_HOURS:
        return 1.0
    if gap_h <= 0:
        return 0.0
    return gap_h / SPREAD_HOURS


def _freshness_score(dt: datetime, now: datetime) -> float:
    """1.0 right now, linear to 0 across the 48h window."""
    hours_ahead = (dt - now).total_seconds() / 3600
    if hours_ahead <= 0:
        return 0.0
    return max(0.0, 1.0 - hours_ahead / FRESH_WINDOW_H)


def _next_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    """The slot's next start: today while still ahead, else tomorrow."""
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt


def _fmt_gap(gap_h: float) -> str:
    if gap_h >= 1:
        return f"{gap_h:.0f}h after last post"
    return f"{max(1, int(gap_h * 60))}m after last post"


def _reason(hour: int, minute: int, src: str, dt: datetime,
            last: Optional[datetime]) -> str:
    label = (f"metrics peak {hour:02d}:00" if src == "real"
             else f"cadence slot {hour:02d}:{minute:02d}")
    if last is None:
        spread = "no recent post"
    else:
        spread = _fmt_gap((dt - last).total_seconds() / 3600)
    return f"{label}, {spread}"


def _score(base: float, dt: datetime, now: datetime,
           last: Optional[datetime]) -> float:
    return (W_METRICS * base
            + W_SPREAD * _spread_score(dt, last)
            + W_FRESHNESS * _freshness_score(dt, now))


# ---------- public API ----------

def best_slots(cfg: Config, now: datetime, k: int = TOP_K) -> list[dict]:
    """Top-k candidate slots as [{hour, at, score, reason}] — deterministic
    for the same (cfg, now, db state): sorted by score desc, then earliest."""
    candidates, src, base = _signals(cfg)
    last = last_published_at()
    scored: list[dict] = []
    for hour, minute in candidates:
        first = _next_occurrence(now, hour, minute)
        for offset in range(HORIZON_DAYS + 1):
            dt = first + timedelta(days=offset)
            scored.append({
                "hour": hour,
                "at": dt,
                "score": round(_score(base.get(hour, 1.0), dt, now, last), 4),
                "reason": _reason(hour, minute, src, dt, last),
            })
    scored.sort(key=lambda s: (-s["score"], s["at"]))
    return scored[:max(1, int(k))]


def pick_slot(cfg: Config, draft_kind: str, now: datetime) -> datetime:
    """When this draft should go out (see pick_slot_with_reason for the why)."""
    return pick_slot_with_reason(cfg, draft_kind, now)[0]


def pick_slot_with_reason(cfg: Config, draft_kind: str,
                          now: datetime) -> tuple[datetime, str]:
    """The scheduling decision, with its reason.

    posts/quotes: best_slots[0] — metrics-peak, spread and freshness are
    already priced into the score. replies/mentions: conversation decay
    beats peak-hunting — take the NEAREST slot inside the 90-minute window
    when one exists, else fall back to best_slots[0].
    """
    ranked = best_slots(cfg, now, k=MAX_CANDIDATES)
    if draft_kind == "reply":
        window = [s for s in ranked
                  if timedelta(0) < s["at"] - now
                  <= timedelta(minutes=REPLY_WINDOW_MIN)]
        chosen = (min(window, key=lambda s: s["at"]) if window else ranked[0])
    else:
        chosen = ranked[0]
    db.log("slots", f"picked {chosen['at'].isoformat(timespec='minutes')} for "
                    f"{draft_kind}: {chosen['reason']}")
    return chosen["at"], chosen["reason"]


def day_slots(cfg: Config, day: date, now: Optional[datetime] = None,
              k: int = TOP_K) -> list[dict]:
    """Scored candidate slots for one calendar day (Calendar page chips).
    Same math as best_slots, anchored to that day's occurrences."""
    now = now or datetime.now()
    candidates, src, base = _signals(cfg)
    last = last_published_at()
    out: list[dict] = []
    for hour, minute in candidates:
        dt = datetime.combine(day, dtime(hour, minute))
        if dt <= now:
            continue  # already started — not a chip, it rolled to tomorrow
        out.append({
            "time": f"{hour:02d}:{minute:02d}",
            "hour": hour,
            "at": dt,
            "score": round(_score(base.get(hour, 1.0), dt, now, last), 4),
            "reason": _reason(hour, minute, src, dt, last),
        })
    out.sort(key=lambda s: (-s["score"], s["time"]))
    return out[:max(1, int(k))]


def hour_reasons(cfg: Config, now: Optional[datetime] = None) -> dict[str, str]:
    """Reason string per candidate hour — the single source the Insights
    best-hours captions reuse (keys are hour-of-day as str)."""
    now = now or datetime.now()
    candidates, src, _base = _signals(cfg)
    last = last_published_at()
    reasons: dict[str, str] = {}
    for hour, minute in candidates:
        dt = _next_occurrence(now, hour, minute)
        reasons[str(hour)] = _reason(hour, minute, src, dt, last)
    return reasons
