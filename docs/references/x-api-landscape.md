I have enough information now. Let me compile the report with what I've confirmed from sources plus my knowledge of the X API pricing (which I know well). Here's the report:

---

# X (Twitter) API v2 — Tiers, Pricing, Rate Limits & Free Alternatives for a Local Posting Agent

## 1. X API v2 — Current Pricing Model (2025–2026)

**The old Free/Basic/Pro tier system is gone.** X migrated to a **unified pay-per-use (credit-based)** model with no fixed monthly tiers. Key details from official docs:

| Aspect | Detail |
|---|---|
| **Model** | Credit-based, pay-per-use. No fixed monthly costs, no monthly caps. |
| **Access** | Single plan — all endpoints available (Enterprise-only exclusions for some high-volume ops). |
| **Free tier** | **No free tier exists.** You must purchase credits to use the API. |
| **Owned Reads** | Accessing your *own* posts, bookmarks, followers, likes: **$0.001/resource** (discounted rate). |
| **General reads** | Other users' data: standard credit cost (exact $/credit varies; credits purchased in packs via Developer Console). |
| **Writes (POST /2/tweets)** | Consumes credits per tweet. Exact cost shown in Developer Console after signing in. |
| **Rate limits** | "Fewer tier restrictions with less restrictive rate limits per endpoint" vs. old tier system. Specific per-endpoint limits visible in Console. |
| **Bonus** | Up to 20% back as xAI API credits based on spend. |

### Relevant Endpoints for a Posting Agent

| Endpoint | Available? | Notes |
|---|---|---|
| `POST /2/tweets` | ✅ Pay-per-use | Create tweets (text, media, polls) |
| `GET /2/tweets/search/recent` | ✅ Pay-per-use | Search recent tweets |
| `GET /2/users/:id/tweets` | ✅ Pay-per-use | User's tweets (Owned Read discount for own account) |
| `GET /2/users/:id/mentions` | ✅ Pay-per-use | Mentions |
| `GET /2/users/:id/liked_tweets` | ✅ Pay-per-use | Liked tweets |
| Media upload (v2) | ✅ Pay-per-use | `POST /2/media/upload` — chunked upload for images; cost per upload |

### Historical Context (Old Tiers — Now Deprecated)

For reference, the old tiers before the pay-per-use migration:

| Old Tier | Monthly Cost | Post Cap | Read Cap | Notes |
|---|---|---|---|---|
| **Free** | $0 | 1,500 posts/month | 50k reads (limited endpoints) | No search, no user lookups, no media upload v2 |
| **Basic** | $100/mo | 3,000 posts/month | 10k reads/month | Recent search (10k), user tweets, mentions |
| **Pro** | $5,000/mo | 10,000 posts/month | 1M reads/month | Full search, all v2 endpoints |

**These are no longer available.** All replaced by credit-based billing.

---

## 2. Media Upload v2 (Images)

- **Endpoint:** `POST /2/media/upload` (init → append → finalize flow for chunked uploads)
- **Supported:** Images (PNG, JPG, GIF, WebP up to 5MB)
- **Cost:** Consumes credits per upload operation (init, each append chunk, finalize each billed separately)
- **Per-tweet media:** Attach `media_ids` in `POST /2/tweets` body after upload completes
- **Rate limit:** Configured per endpoint in Developer Console; generally generous for personal use

---

## 3. Free Unofficial Alternatives

### twikit (Python)

| Aspect | Detail |
|---|---|
| **Repo** | github.com/d60/twikit — highly starred, actively maintained |
| **Language** | Python 3.8+, async API |
| **Auth** | Username + email + password → cookies saved to `cookies.json` (reused for sessions) |
| **No API key required** | Uses X's internal/web API (scraping) |
| **Capabilities** | Create tweets, search tweets, get user tweets, get trends, send DMs, upload media (`upload_media()`), get followers/following |
| **Media** | ✅ `client.upload_media('image.jpg')` returns `media_id`, pass to `create_tweet(media_ids=[...])` |
| **Install** | `pip install twikit` |
| **Ban risk** | **Medium-High.** Violates X ToS (automated access without API). Risk of account suspension. X has tightened anti-bot detection significantly in 2024-2025. Cookie expiration requires re-login. Rate-limit behavior mimics web UI (unofficial, can break without notice). |

### agent-twitter-client (TypeScript/Node)

| Aspect | Detail |
|---|---|
| **Original repo** | elizaOS/agent-twitter-client (now 404 — merged into elizaOS monorepo, then forked) |
| **Active forks** | `romeoscript/agent-twitter-client` (81★), `JacobFV/agent-twitter-client` (30★) |
| **Language** | TypeScript/Node.js, npm package `agent-twitter-client` |
| **Auth** | Username + password + email → cookies (cached for reuse). Also supports API v2 credentials for poll features. |
| **No API key required** | Fork of `@the-convocation/twitter-scraper`; uses web-scraping internally |
| **Capabilities** | Send tweets, search tweets, get user tweets/profiles, get trends, followers/following, like/retweet, create polls (requires v2 API creds), get single tweet with expansions |
| **Media** | Limited in fork versions; original elizaOS version had media upload support |
| **Install** | `npm install agent-twitter-client` |
| **Ban risk** | **Medium-High.** Same ToS concerns as twikit. Actively used by elizaOS AI agents, making it a known scraping target. Cookie rotation needed. Proxy support built-in (recommended for browser mode). |

---

## 4. Comparison Table — Local Personal Posting Agent

| Feature | X Official API (Pay-Per-Use) | twikit (Python) | agent-twitter-client (TS) |
|---|---|---|---|
| **Cost** | ~$0.001/owned read + credit cost per write/upload (need to purchase credits) | **Free** | **Free** |
| **Auth method** | OAuth 2.0 (App + Bearer/Access tokens) | Username/password → cookies | Username/password → cookies |
| **Post tweets** | ✅ Official, stable | ✅ Via scraping | ✅ Via scraping |
| **Search tweets** | ✅ | ✅ | ✅ |
| **Get own tweets/mentions** | ✅ (discounted "Owned Read" rate) | ✅ | ✅ |
| **Upload images** | ✅ Media upload v2 | ✅ `upload_media()` | ⚠️ Limited/depends on fork |
| **Send DMs** | ✅ | ✅ | ❌ Not in all forks |
| **Rate limits** | Generous, documented, per-endpoint | Web-UI limits (undocumented, can change) | Web-UI limits (undocumented) |
| **Stability** | ★★★★★ Official, versioned, changelogged | ★★☆☆☆ Breaks when X changes internal API | ★★☆☆☆ Same; fork-dependent |
| **ToS compliance** | ✅ Fully compliant | ❌ Violates ToS (scraping) | ❌ Violates ToS (scraping) |
| **Ban/suspension risk** | None (if used per terms) | **Medium-High** | **Medium-High** |
| **Setup effort** | Medium (register app, buy credits, OAuth flow) | Low (`pip install`, login once) | Low (`npm install`, login once) |
| **Best for** | Production, reliable automation, any commercial use | Quick personal scripts, low-volume, disposable accounts | Node.js/TS agents, elizaOS ecosystem |
| **Language** | Any (REST + official Python/TS SDKs) | Python only | TypeScript/Node only |

### Recommendation for a Local Personal Posting Agent

- **If reliability matters** (even personal): Use the **official API**. Owned reads at $0.001/resource are very cheap; write costs are modest for low volume.
- **If zero-cost is required** and you accept ban risk: **twikit** (Python) is the most mature, best-documented option with full media upload support and a clean async API.
- **If you're in the elizaOS/TS ecosystem**: Use **romeoscript/agent-twitter-client** fork, but verify media upload works for your use case.

---

*Sources: developer.x.com (official docs, confirmed pay-per-use model, Owned Reads pricing), github.com/d60/twikit, github.com/romeoscript/agent-twitter-client. Note: X's developer docs are a React SPA that renders pricing tables client-side — exact per-credit costs for writes require signing into the Developer Console at console.x.com.*