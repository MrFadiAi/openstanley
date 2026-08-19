# Reference docs (research inputs)

| file | source | bytes |
|---|---|---|
| `stanley-product-intel.md` | research agent (2026-08-18), 23 API calls — Stanley product deep-dive | ~14K |
| `competitive-landscape-2026.md` | research agent, 12 calls — X growth tools landscape | ~23K |
| `oss-building-blocks.md` | research agent, 30 calls — twikit/tweepy/stack research | ~25K |
| `x-api-landscape.md` | research agent (re-run) — X API v2 pay-per-use model + free alternatives | ~8K |
| `stanley-deep-dive.md` | my initial fetch of getstanley.ai/welcome | ~3K |
| `stack-decisions.md` | build-time stack rationale | ~1.4K |

Key findings applied to the build:
- twikit 2.3.3 signatures verified against the installed lib (`get_notifications(type="Mentions")`,
  `create_tweet(reply_to=...)`) — cookie client now matches reality.
- Research recommends **hybrid X access**: twikit for free reads + tweepy/official for reliable
  posting when it matters. Our three-mode client already supports both.
- twikit is maintenance-fragile (2025-26 breakage history) → keep `dryrun` default,
  pin `twikit==2.3.3` (requirements.txt unpinned → see ROADMAP v0.2 to pin).
- Stanley's real loop confirmed: STUDY → PLAN → PUBLISH → ENGAGE → MEASURE → FEEDBACK —
  matches our study/create/publish/engage/learn loops.
- Stanley's moat is a messaging-native UI (SMS/Telegram); ours is local-first + X-depth.
