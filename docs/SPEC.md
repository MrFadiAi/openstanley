# SPEC — OpenStanley (working name)
> Local-first, single-user AI agent that grows your X following — a OpenStanley-style content operation for X only.

## 1. Mission
Replicate getstanley.ai's product behavior for **X only**, running **100% locally**:
the agent reads your X history, learns your voice and niche, generates daily post ideas,
drafts posts in your voice, maintains a story bank, drafts replies to your notifications,
and publishes on a schedule — all reviewable in a local web dashboard.

## 2. Core loops (mirroring OpenStanley)
1. **Study loop** (nightly): pull niche/competitor posts → extract what's landing → update story bank with fresh angles.
2. **Create loop** (daily): generate ideas → draft 3–7 posts in user voice → queue for human review.
3. **Engage loop** (hourly): fetch mentions/notifications → draft on-voice replies → queue for approval.
3. **Publish loop** (by schedule): approved queue items go out at configured times.
4. **Learn loop** (weekly): ingest own post metrics → rank voice examples → update voice fingerprint.

## 3. Operating principles
- **Human approval gate** before anything is published (dry-run by default).
- **Local-first**: all data in SQLite on D:, no cloud dependency beyond the LLM API + X itself.
- **Provider-agnostic LLM**: any OpenAI-compatible endpoint (GLM/z.ai, OpenAI, Ollama…).

## 4. Feature spec

### 4.1 X account connection
- Three modes: `api` (official v2), `cookie` (twikit), `dryrun` (no posting).
- Onboarding: import last N posts + profile stats; store in SQLite.

### 4.2 Voice learning
- Extract a **style rubric** (auto): diction, casing habits, punctuation, emoji use, humor, structure, avg length, thread habits.
- Maintain `voice_examples`: top-K posts by engagement, auto-refreshed weekly.
- Every generation prompt = style rubric + few-shot examples + task.

### 4.3 Idea engine / story bank
- Sources: niche radar accounts (user-configured list), trending topics from X search, evergreen user themes, "remix" of user's past winners.
- Each idea: {title, angle, format (one-liner/thread/hook+insight), source, freshness}. Nightly refresh keeps bank 20+ deep.

### 5.4 Draft generation
- Daily target (default 4) drafts from the idea bank, in-voice.
- Temperature ladder: 1 safe, 2 edgy, 1 experimental.
 ladder not applied per draft but per run config.
- Thread builder: hook + 3–8 tweets, numbered, hooks re-usable.

### 4.4 Draft generation
- Daily target (default 4) drafts from the idea bank, in-voice.
- Temperature ladder: 1 safe, 2 bold, 1 experimental per run.

### 4.5 Reply/engage
- Pull mentions + notifications; for each, draft an on-voice reply.
- Never auto-send by default. "Auto-approve safe replies" toggle (off by default).

### 4.6 Publisher
- Queue with scheduled times (default 9:00/13:00/18:00 local). Approval gate before send.
- Dry-run logs what *would* be posted.

### 4.7 Analytics
- Track per-post: impressions, likes, reposts, replies, profile visits → engagement rate.
- Weekly digest text: what format/topic/length is winning for YOU.

### 4.8 Dashboard
- Tabs: Inbox (approvals) · Drafts · Ideas/Story Bank · Queue · Analytics · Settings.
- Actions: approve/edit/regenerate/discard, edit voice profile, edit schedule.

## 6. Non-goals (v1)
- No DMs, no cross-platform, no browser UI automation of X web, no fine-tuning of local models.

## 7. Acceptance criteria
1. `python -m openstanley.server` starts server at 127.0.0.1:7878 with dashboard.
2. Dry-run mode: full pipeline works with zero X credentials (simulated data).
3. Cookie mode: can log in with twikit cookies, import history, post for real (user-supplied cookies).
4. API mode: OAuth handled via paste-flow; supports posting + reads within tier limits.
5. Nightly study + daily create + hourly engage loops run on APScheduler and are visible in logs + dashboard.
6. All generations pass through voice rubric prompt; drafts list their idea source.
7. Everything stored in SQLite; exportable.

## 8. Naming
Working name **OpenStanley**. (Rename anytime — `openstanley` package name is cosmetic.)
