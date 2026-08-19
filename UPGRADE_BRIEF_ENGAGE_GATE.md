# OpenStanley v0.3.8 — Engage Quality Gate (real reply-surface detection)

Engage is the weakest loop now: it picks target tweets by search and drafts replies,
but reply quality decides everything on X — a generic reply on a dead tweet is wasted
cap budget (10/day) and looks botty. Two real problems:

1. **Dead-tweet targeting**: engage can draft a reply to a 3-week-old tweet with 2 likes.
   Reply window on X is ~2-6 hours; after that it's noise.
2. **No reply-surface check**: replying to a tweet whose author never engages back, or
   where the conversation already has 40 replies from other bots, is wasted budget.

Build an engage quality gate that scores TARGETS before drafting:

## 1. `openstanley/gen/engage_gate.py`
- `score_target(cfg, tweet, now) -> TargetScore` with fast heuristic layers:
  - `recency`: tweet age — full points < 3h, linear decay to 0 at 24h, hard reject > 48h
    (use tweet created_at when parseable; if missing, WARN not reject)
  - `traction`: log-scaled likes+reposts of the TARGET tweet (a rising tweet = surface)
  - `author_surface`: from DB stats if the author was seen before (their avg engagement),
    neutral 0.5 when unknown
  - `crowding`: reply_count — mild penalty as it grows (0 replies is also slightly bad:
    nobody's talking there), sweet spot 1-15
  - `niche_fit`: keyword/embedding-lite match of the tweet text against the brain's
    niche map (reuse existing matching util if present; else token overlap)
- Composite 0-100 with weights in config `[agent] engage_gate` (recency 0.35,
  traction 0.25, author 0.15, crowding 0.1, fit 0.15). Threshold default 55.
- `filter_targets(cfg, tweets, now) -> (kept, rejected)` — sorted by score, cap kept at 12.

## 2. Wire into the engage loop
- `agent.engage()` filters candidate tweets through the gate BEFORE the LLM drafts
  replies (cheaper: no LLM calls on rejected targets).
- Rejected targets logged (count + top reasons) for the Insights/debug view.
- The score attaches to draft meta (`meta.target_score`) so the approval card can
  show "target 78/100 · fresh 2h · rising".

## 3. UI — ApprovalCard target chips
- On reply drafts: show target score + component breakdown (fresh/traction/crowd chips).
- EN+AR strings. Small, clean, consistent with existing chips.

## 4. Tests (hermetic)
- Recency decay math incl. hard reject; traction log-scale; crowding sweet spot;
  composite weights; threshold filtering + cap 12; missing-created_at WARN path;
  engage loop integration (LLM never called on rejected targets — spy); meta attach.
- ~10 tests → aim ~148. No network, keep suite green.

Hard rules: hermetic, ONE new module + minimal touches, no X behavior changes beyond
the gate (same reads, same caps), approval gate untouched.
