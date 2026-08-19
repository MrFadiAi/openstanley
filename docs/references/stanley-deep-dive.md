# Stanley (getstanley.ai) — Product Deep-Dive
> Compiled from getstanley.ai marketing pages, 2026-08. Feeds SPEC.md.

## Product overview
Stanley is an **AI social-media growth agent** ("One Agent. Your Entire Content Operation."). Positioned not as a scheduler but as an *agent* that reads your socials, learns your niche + voice, and shows up every day with content built for you. Tagline: "Own Your Distribution."

Core promise: grow your following across platforms while you focus on the work you love.

## Feature-by-feature breakdown

| Stanley feature | What it does | Our X-only equivalent |
|---|---|---|
| Reads your socials | Ingests your past posts + niche on onboarding | Import last ~500 posts via API/twikit, extract voice + topics |
| Overnight Strategy | Studies your niche nightly, drafts posts while you sleep; wake up to a queue | Nightly cron: niche scan → idea gen → drafts ready for morning review |
| Daily Content Planning | Insights become a curated "story bank" so best ideas always ready | Idea bank table (evergreen + trending), refreshed nightly |
| Posts scripted in your voice | Voice cloning from your history; gets sharper over time | Few-shot voice prompt built from top-K posts by engagement + style rubric auto-extracted |
| Top performing ideas every day | Surfaces what's working in your niche | Niche radar: track N competitor/topic accounts, extract patterns from their winners |
| Winning Content Strategy | Tells you what's working, where to double down | Weekly analytics digest: format/topic/length breakdown vs your baseline |
| Outreach & comments 24/7 | Agent replies/engages on your behalf | Reply assistant: mentions/notifications inbox, draft replies in voice, human approve → send |
| Send a voice note → get a post | Voice-to-post pipeline | (Later) whisper + voice note → post draft |
| Every Platform done for you | LinkedIn, Instagram, X | **X only** — that's the whole point |
| Saves 80% of content time | You ideate, he writes/edits/publishes | Same loop: human approves, agent executes |

## Onboarding flow (from marketing copy)
1. Connect accounts → 2. Agent studies your history + niche → 3. Wakes you with a draft plan → 4. You approve/edit → 5. He publishes + engages → 6. Loop learns from each post's performance.

## Pricing
Marketing page /welcome showed no pricing tiers (post-signup). Public info suggests subscription SaaS. Our version: local, free (only LLM API cost).

## Tech-stack hints
Site built with Framer. No public stack details; agentic features imply LLM orchestration + platform APIs or browser automation. For our clone we choose **official X API v2 if user has paid tier; twikit cookie client as free fallback** — both behind one interface.

## Reviews / social proof
Testimonial on site (Elly Walton, 135K). No major review base found yet.

## Gaps we can exploit
- Stanley is SaaS-only, cloud, subscription → ours is **local-first, own your data, no monthly fee**
- X-only focus → deeper X-specific optimization (threads, hooks, timing)
- Review-before-post + full transparency of the agent's reasoning
