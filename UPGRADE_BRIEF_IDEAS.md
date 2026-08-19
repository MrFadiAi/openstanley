# OpenStanley v0.4.3 — Idea Bank Replenishment (never run dry)

The idea bank hit "8 ideas left" in today's digest. When it empties, create/autopilot
silently degrade to nothing — the create loop has no fallback when `ideas_remaining=0`.
OpenStanley's actual product loop (study → CREATE) dies quietly. Today ideas come only
from manual import + occasional scans.

Make the bank self-replenishing:

## 1. `openstanley/gen/ideas.py` — idea generation engine
- `replenish(cfg, min_bank=15, batch=8) -> {added, sources}` — runs ONLY when bank
  below `min_bank`, generating a batch of NEW ideas from (in priority order):
  a. **unmined scan data**: engagement outliers in stored posts (top 10% by rate)
     whose topic/hook angle isn't represented in the current bank (token overlap)
  b. **brain journal**: recent reflect entries' insights not yet ideas
  c. **fresh study reads**: if a+b yield < batch/2 and reads are allowed (caps respected),
     pull `study()` results and distill angles
  d. **evergreen synthesis**: config themes × brain strategy statements → angles
- Each idea: `{text, angle, source, score_hint}` — angle = the take/frame (not just a
  topic), stored via existing idea store; `source` recorded for analytics.
- Dedupe within batch AND against bank (token-overlap threshold) before storing.
- Voice-lock NOT applied here (ideas are seeds, not drafts) but ideas carry the niche
  keywords for later drafting.

## 2. Wiring
- `Agent.create()` checks bank level first: below `min_bank` → run replenish before
  drafting (log line "bank low (N) — replenished +M from X,Y").
- Autopilot `study` phase ALSO triggers replenish check (cheap, DB-only when possible).
- `POST /api/ideas/replenish` — manual trigger, returns what was added.
- Ideas page: "Bank health" chip (count + last replenish), sources shown per idea
  (scan/brain/study/evergreen badges), "Replenish now" button.

## 3. Tests (hermetic)
- Outlier mining picks top-rate posts; angle-novelty dedupe (bank + batch); journal
  distillation; evergreen synthesis shape; replenish skips when bank >= min;
  a+b sufficient → no X reads (spy); fallback chain order; create-loop wiring (low bank
  → replenish called before drafting); API endpoint; idea source persisted.
  ~11 tests → aim ~216. No network, suite green.

Hard rules: hermetic; X reads only in path (c) and only through existing throttled
methods; ONE new module + minimal touches; approval gate untouched.
