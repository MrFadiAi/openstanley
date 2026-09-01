# OpenStanley — Calendar redesign (editorial paper sheet)

User-provided spec, verbatim intent: minimalist content scheduling calendar,
clean editorial aesthetic — serif "Content Calendar" heading, Queue panel on
the left with empty state (None yet · big 0 · "Posts you queue in Write
appear here, ready to schedule." · dark "Start drafting in Write" button),
3-day vertical columns of large rounded slot cards (posting times as
"9:00 AM — Open" + dashed "Custom Time"), UTC/tz chip + date range + ‹ Today ›
+ 3 Days/Week/Month switcher + settings gear. White canvas, subtle gray
borders, generous whitespace, black primary button, no distracting colors.

## Integration decisions (user-confirmed)
- **Queue = unscheduled drafts** (status draft|approved, `scheduled_at` NULL).
  No new state — it's a live view of `GET /api/drafts`.
- **Slot cards come from Settings `post_times`** (e.g. 09:00/13:00/18:00) ×
  each visible day; smart-slot scores render as a small badge with reason on
  hover; one dashed "Custom Time" slot per day opens a time picker.
- **Drag kept**: Queue item → slot/day schedules it (existing
  `POST /api/drafts/{id}/reschedule` accepts draft status); post → slot/day
  reschedules; drop on a day without a slot = that day's best open time.

## Visual system
- **Paper sheet**: the Calendar page is a deliberately LIGHT surface inside
  the dark app — all theme tokens flipped via a scoped `.cal-sheet` wrapper
  (warm paper `#faf9f7`, near-black ink, black accent). Nothing outside the
  page changes; portaled popovers get the class too.
- **Typography**: Newsreader (self-hosted latin woff2, ~24KB each, works
  offline) — 500 for headings/day numerals, italic 400 for the subtitle.
  Existing sans stays for UI text.
- **Layout**: header (serif title + controls right) → grid [Queue rail |
  day columns]. 3 Days (default) / Week (7 narrower columns) / Month (compact
  grid, restyled). Fully RTL/i18n EN+AR; times format per locale.

## Out of scope
Backend, algorithm, smart-slot computation, approvals — all reused as-is.
362 backend tests untouched.
