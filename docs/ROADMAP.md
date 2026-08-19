# Roadmap

> History lives in PROGRESS.md (one line per ship). This file is the plan forward.

## Shipped (condensed)

- **v0.1–0.2** ✅ 2026-08-18 — dry-run/cookie/api X modes, onboarding wizard,
  safety caps + jittered delays, chat-first Write screen (SSE streaming, tool
  calls), Algorithm Score, Calendar, Ideas, Strategy, Insights, bilingual AR/EN,
  deep-scan style profile. `twikit==2.3.3` pinned.
- **v0.3.x** ✅ 2026-08-19 — cookie auto-heal via Brave CDP, autopilot
  (study→create→engage→learn, publish never auto), real metrics ground truth,
  live smoke self-check, engage quality gate, mention inbox, voice lock,
  smart slots, daily digest, self-replenishing idea bank.
- **v0.4.x** ✅ 2026-08-19 — Telegram second frontend (chat, /status /ideas
  /drafts /approve /post /digest, approval cards, digest push).
- **v0.5.0** ✅ 2026-08-19 — multi-account: account registry + per-account DB
  scoping, per-account brains/data dirs, loops pin the active account,
  AccountSwitcher UI + Connect bootstrap, TG /account. Rename → OpenStanley.
- **v0.5.1** ✅ 2026-08-19 — TG output polish (web-agent parity), bare-token
  cookie paste on every cookie surface (no JSON required), bootstrap cookie
  validation with auto-heal disabled.

## Next up (v0.6 candidates, priority order)

1. **Media/images end-to-end** — image attach on drafts (twikit
   `upload_media` / media upload v2), thumbnails in Inbox/TG approval cards,
   image-aware Algorithm Score (media factor already scored). Biggest single
   engagement lever ("2x algorithm boost").
2. **Thread composer** — Typefully-style multi-tweet editing, per-tweet
   voice-lock, thread scheduling in Calendar.
3. **A/B variants** — approve 2 versions of a draft, alternate slots,
   compare in the learn loop.
4. **Voice-note → post** — whisper/ASR input → draft (Stanley's signature
   input mode).
5. **Inspiration tab** — remixable viral posts from niche (not just abstract
   ideas); "steal this hook" pattern library.

## Later / ideas

- DM inbox triage (read-only)
- Browser-extension cookie grabber
- Export/import full state (JSON bundle)
- Media generation (inline image gen for drafts)
