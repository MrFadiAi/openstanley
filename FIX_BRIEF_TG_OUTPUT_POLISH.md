# FIX BRIEF — TG output polish: web-agent parity (v0.5.1)

User verdict (verbatim): TG agent output is "not clean, not clear to read, not beautiful…
weird symbols sometimes". The web agent output is "a lot better". Goal: close the gap.

## Root causes to eliminate

1. **Raw markdown leaking.** No outbound message may EVER contain literal `**`, `` ` ``,
   `##`, `— ` fence markers, or unconverted markdown. Audit EVERY send path:
   chat streaming (first bubble + every progressive edit + final edit + abort partial),
   command replies, approval cards, digest, /study chain report, error replies.
   The converter must be total: unknown markdown is stripped, never shown raw.

2. **Streaming flash artifacts.** Mid-stream edits render partial segments: a half-emitted
   `**bold` shows one literal asterisk, an incomplete bullet dangles. Fix: per-edit
   sanitization — only render *complete* markdown segments in progressive edits
   (unterminated markers deferred to the next edit / final), so no raw symbols ever flash.

3. **Markdown features TG can't render** must degrade gracefully (web renders these, TG doesn't):
   - `## Header` → `<b>` line (no hash)
   - tables → compact `<pre>` aligned block or bullet list, never raw pipes
   - nested lists → flat bullets with indent spaces
   - blockquotes → TG `<blockquote>`
   - links: keep, HTML-escaped; bare URLs → clickable via message auto-detection (no entities needed)

4. **Command/card template redesign.** The approval card below is the user's exact complaint:

   BEFORE (real, ugly):
   ```
   Waiting for approval
   · #2321 [reply] “agents know this already anxiety needs a body, i just run the loop again” · → @naval · voice 100%
   · #2320 [reply] “i made myself into an agent that works while i sleep my human just drink coffee now AI di…” · → @naval · voice 85%
   /approve <id> · /reject <id> — or open the dashboard.
   ```

   Redesign to (structure > symbols, no mid-word cuts, no · soup):
   ```
   ⏳ 5 drafts waiting for approval
   Replies drafted to @naval's recent posts:

   #2321 — voice 100%
   “agents know this already anxiety needs a body, i just run the loop again”

   #2320 — voice 85%
   “i made myself into an agent that works while i sleep. my human just drinks coffee now”

   Reply /approve <id> or /reject <id>
   ```

   Rules: quote draft text verbatim in full (drafts are ≤280 chars — they FIT, no truncation
   at all); drop `…`; one draft per block separated by blank line; no inline `·` separators;
   keep emoji only as leading section markers. Apply the same discipline to /status,
   /drafts, /ideas, /digest, /study report, new-draft cards, approve/reject confirmations.

5. **Batch crowding cap.** Engage loop may draft at most 2 replies per author per batch
   (user got 5 to @naval = spammy). Cap + test.

6. **Unicode hygiene.** Replace characters that render as boxes/weird glyphs on mobile TG:
   smart quotes are fine; strip zero-width chars; never emit `·` as a separator (bullets
   and newlines only); ellipsis `…` only at true truncation points (which for drafts no
   longer exist).

7. **System-prompt nudge.** _system_tg should instruct: "Telegram messages: short
   paragraphs, bold for key terms, bullet lists for collections. No tables, no headers,
   no horizontal rules." so the model emits TG-native shapes upstream (defense in depth
   with the converter).

## Constraints
- Hermetic tests only; no network in tests. Extend tests/test_tg_format.py + card tests:
  assert NO literal `**`/`` ` ``/`·` separators/`##` in ANY outbound text across all
  send paths; assert full draft text present in cards; assert per-author cap 2;
  assert partial-segment edits never contain unbalanced markers.
- Full suite green (310 baseline). Streaming + follow-through + multi-account unregressed.
- Frontend untouched.

## Deliverable
Commit + report with BEFORE/AFTER samples for the approval card and one chat reply.
