# Roadmap

## v0.1 ✅ (done)
- Dry-run + cookie + api X modes; import; voice rubric; idea bank; drafts (temp ladder);
  reply drafting; approval-gated scheduler; analytics; dashboard; tests.

## v0.2 (next — informed by competitive research, docs/references/)
- [x] Real-X onboarding wizard (cookie paste validation + health check) ✅ 2026-08-18
- [x] Safety layer: daily caps + jittered delays + cap-reschedule (cookie mode) ✅ 2026-08-18
- [x] Fixed APScheduler bug: cron jobs crashed in worker thread (ensure_future) ✅ 2026-08-18
- [x] OpenStanley-parity UI: chat-first Write screen ("Ask OpenStanley"), Content Calendar,
      X Strategy one-pager, Insights, sidebar layout (from real app screenshots) ✅ 2026-08-18
- [x] Chat agent with live context (voice + ideas + drafts + analytics) + quick-action chips ✅
- [ ] Pin `twikit==2.3.3` (research shows it's maintenance-fragile — updates can break)
- [ ] Performance prediction score before publish (Tweet Hunter idea) — LLM rates each
      draft 0-10 for reply-likelihood + on-voice fit; shown in dashboard
- [ ] Media: image attach (twikit upload_media / media upload v2) — 2x algorithm boost
- [ ] Thread composer UI with per-tweet editing (Typefully idea)
- [ ] "Inspiration" tab: remixable viral posts from niche, not just abstract ideas (Taplio idea)
- [ ] Voice-note → post (whisper/ASR → draft) — OpenStanley's signature input
- [ ] Weekly digest as a dashboard card + markdown export
- [ ] Post time optimizer (learn best hours from own metrics)

## v0.3
- [ ] Thread composer UI with per-tweet editing
- [ ] "Steal this hook" — pattern library extracted from niche winners
- [ ] A/B variants: approve 2 versions, alternate slots, compare in learn loop
- [ ] DM inbox triage (read-only)

## Later / ideas
- [ ] Browser-extension cookie grabber
- [ ] Multi-account support
- [ ] Auto-engagement budget (max N replies/day, cool-downs) for cookie mode safety
- [ ] Export/import full state (JSON bundle)
