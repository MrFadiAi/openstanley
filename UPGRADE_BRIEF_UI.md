# OpenStanley v0.3.1 — BeautifulUI Chat Experience

Upgrade the Write (OpenStanley chat) experience using the **beautifului skill** at
`C:\Users\Fadinl\.claude\skills\beautifului\` (19 production-grade React primitives,
MIT). "Use the most of it" is the directive — apply every component that fits the
agent chat experience naturally.

## Setup (read SKILL.md first)
1. Read `C:\Users\Fadinl\.claude\skills\beautifului\SKILL.md` completely.
2. Copy `components/` + `atoms/` into `web/src/components/bui/` (keep attribution headers + LICENSE).
3. Import `theme/tokens.css` into the app's global CSS. IMPORTANT: the project uses
   Tailwind v3 (tailwind.config.js). tokens.css uses Tailwind v4 `@theme` syntax —
   EITHER upgrade the project to Tailwind v4 (preferred, vite plugin @tailwindcss/vite)
   OR port the token vars into tailwind.config.js + index.css so classes like
   `bg-surface text-ink-2 border-line ring-accent` resolve. Choose one, make it work.
4. Override `:root` token values to OpenStanley's identity: accent stays purple
   (#7c6cff family), dark theme preserved. Components must restyle from tokens alone.
5. npm deps as needed: `glimm` (PromptBar), `iconoir-react` (SelectionActions). Skip
   `liveline` unless you use InsightCards.

## The new Write page — component map (apply ALL of these)
- **Chat** (07) — the core tabbed chat panel: reasoning tab (OpenStanley's context-gathering:
  idea bank scan, voice check, algorithm pre-score), reply tab (with sources), composer.
- **PromptBar** (08, Rounded) — the composer, replacing the plain textarea:
  @sources → @niche accounts + @ideas (autocomplete from real data),
  /commands → /draft /schedule /quote /scan /strategy /best-post,
  model picker → temperature ladder (safe/bold/experimental) + language (AR/EN/mixed).
- **StreamingText** (03) — every OpenStanley reply: inline source chips (idea #refs,
  draft ids), action buttons, follow-up suggestion chips. Wire to existing SSE
  /api/chat/stream.
- **Thinking** (02) — expandable "how I chose this" trace under each reply: the
  steps OpenStanley actually took (context built from: voice rubric, top ideas,
  analytics, strategy). Backend already injects context — surface it as steps.
- **LoadingState** (01, Drive variant) — shown between send and first SSE token.
- **ToolChips** (05) — when chat executes tools (schedule_draft, create_quote_draft,
  query_analytics, pick_idea, scan_account, regenerate_draft), render compact
  expandable chips with the payload/result (existing /api/chat returns actions —
  extend backend to return tool-call results as structured events if needed).
- **ApprovalCard** (04) — when OpenStanley proposes a post candidate: question ("Ship this?"),
  context (algorithm score + voice-match % + factor notes), alternatives (regenerate
  = decline-with-variant), accept → saves approved draft. THE approval gate UI.
- **RecommendationCard** (09) — scheduling suggestions: "post at 21:00 — your best
  hour, confidence 78%" with confidence meter + rationale (from analytics heatmap).
- **ContextCards** (10) — when OpenStanley references the idea bank / strategy /
  voice profile, show the retrieved chunks as cards with source + relevance.
- **SelectionActions** (19) — select any text in OpenStanley's reply → floating actions:
  "make it shorter", "more Arabic", "punchier" → inline rewrite streamed in place.
- **TaskRows** (06, Capsules) — mini agent-status strip on Write page: live loop
  states (study/create/engage/publish with last-run + next-run from /api/health or
  scheduler status endpoint — add one if missing).
- **FineTuneCard** (17) — "voice inspector" panel: tune temperature, formality,
  Arabic/English mix, emoji density — feeds into draft generation params (persist
  via /api/settings extension; add fields as needed).
- SidebarNav (14) — upgrade the app sidebar with counts (pending drafts, new
  mentions) + search; keep existing tabs + AR/EN labels + RTL.
- CodeBlock (16) — use for showing thread JSON / raw draft text when user asks
  "show me the raw post" (copy button). Light use is fine.

(Skip DiffTable/RecordsTable/FilterTable/InsightCards/Search unless they fit
somewhere natural — do not force them.)

## Backend touchpoints (keep minimal)
- /api/chat/stream already exists — extend events: `tool`, `thinking_steps`,
  `approval` (post candidate with scores) so the UI can render rich blocks.
- Tool registry exists (openstanley/gen/tools.py) — return structured results.
- Add GET /api/loops/status (last/next run per loop) for TaskRows if missing.
- NO real X calls. NO posting. Approval gate ALWAYS enforced.

## Constraints
- Windows, Git-Bash, venv .venv/Scripts/python, npm in web/.
- Keep AR/EN toggle + RTL fully working (test Arabic labels after swap).
- Keep existing pages (Calendar/Inbox/etc.) working — only Write page + sidebar
  get the beautifului treatment (Inbox may adopt ApprovalCard + score popovers
  if trivial).
- All 39 tests must stay green; add/extend tests for new SSE event shapes and
  /api/loops/status. Run: .venv/Scripts/python -m pytest tests/ -q
- Verify in real browser (Playwright MCP available): send a chat message, watch
  streaming, see ToolChips/ApprovalCard render, toggle AR/EN, zero console errors.
- Build web/ (npm run build), restart server on 7878, curl /api/health + / ,
  commit at milestones.

## Win condition
The Write page feels like a premium agent product: streaming replies with thinking
traces, tool chips firing, approval cards with algorithm scores, a command bar with
@autocomplete — all in OpenStanley's dark purple identity, bilingual, RTL-perfect.
