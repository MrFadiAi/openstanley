# OpenStanley v0.3.6 — Analytics Ground Truth (real metrics → everything)

The account is LIVE now (real posts, real replies). OpenStanley's core promise is
"grow you" — but growth is currently unmeasured: dashboard stats come from scan-time
snapshots only, engagement over time doesn't exist, and the brain never sees fresh numbers.

Build real analytics on the ground truth we can already read:

## 1. `openstanley/gen/metrics.py` — metrics refresh engine
- `refresh_metrics(x, limit=60)` — pull own recent tweets w/ metrics (likes/reposts/
  replies/bookmarks/impressions), upsert into posts (keep earliest snapshot, add
  `metrics_json` latest), and append a row to a new `metric_snapshots` table
  (post_x_id, captured_at, likes, reposts, replies, impressions) so we get TIME SERIES.
- Compute per-post engagement rate: (likes+reposts+replies) / max(followers,1).
- Called from: autopilot learn phase (replaces bare user_tweets refresh), and the
  existing `learn` loop. Throttled reads, safety caps respected (reads only).

## 2. Growth analytics API
- `GET /api/analytics/growth?days=14` — daily aggregates: followers (from identity
  snapshots — also append identity (followers count) to a `identity_snapshots` table
  in refresh), posts published, avg engagement rate, best post of day.
- `GET /api/analytics/top?limit=10` — top posts by engagement rate in window.
- `GET /api/analytics/times` — engagement by hour-of-day (from own posts), feeding
  the existing best-times recommendation with REAL data instead of heuristics.

## 3. UI — upgrade **Insights** page
- Growth chart (Recharts, area): followers + engagement rate over days.
- Top posts list (rank, text, rate, link).
- "Best hours" bar chart from real data (fallback to heuristic when <20 posts).
- Beautifului polish, EN+AR strings, RTL-safe.

## 4. Brain integration
- `refresh_metrics` ends with `brain.reflect("metrics", {...summary...})` so the brain
  learns what actually worked (its rules/strategies update from real performance).
- Guard: reflect only if summary changed materially since last metrics reflection
  (hash compare, stored in settings) — no journal spam on every tick.

## 5. Tests (hermetic)
- Snapshot upsert keeps earliest, updates latest; time-series rows appended;
  engagement-rate math; growth aggregation over fixture days; top-N ordering;
  times-of-day aggregation; brain reflect dedupe (hash gate); API shape tests.
- No network. Keep 115/115 → aim ~125.

Hard rules: hermetic only; ONE new module + minimal touches; real X reads ONLY via
existing client methods (never new raw calls); no writes to X from this feature.
