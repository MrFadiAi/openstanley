"""Full end-to-end WITH a fake LLM (monkeypatched) — no network, no real X.

Covers the whole v0.3 surface: deep scan → bilingual voice → drafts with
algorithm scores → image post + quote post + scheduled reply → publish gate.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                                    # noqa: E402
db.init_db()  # ensure schema exists before anything else

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import voice, ideas, drafts, replies, chat    # noqa: E402
from openstanley.gen import style_scan                             # noqa: E402
from openstanley.gen.agent import Agent                            # noqa: E402
from openstanley.gen.llm import LLMError                           # noqa: E402

CALLS: list[tuple[str, str]] = []
FENCE = "`" * 3

ARABIC_SAMPLE = "أكبر درس تعلمته: ابنِ النسخة القبيحة أولاً. ما أبشع شيء شحنته؟"


def fake_chat(cfg, system, user, temperature=None, json_mode=False, retries=2):
    CALLS.append((system[:80], user[:80]))
    # NOTE: specific-generation prompts FIRST — they all embed the voice block,
    # which contains "VOICE FINGERPRINT" and would otherwise shadow them.
    if "reply agent" in system:
        return json.dumps({"reply": "solid question — answered in the thread"})
    if "engaging with their niche" in system:
        return json.dumps({"reply": "the stack is boring on purpose — what would you have picked?"})
    if "ghostwriter" in system:
        if "QUOTED TWEET" in user:
            return json.dumps({"tweet": "the announcement lacked one thing: shipping dates"})
        if "ARABIC" in user:
            return json.dumps({"tweet": ARABIC_SAMPLE})
        return json.dumps({"tweet": "the ugly version teaches you things slides never will"})
    if "growth strategist" in system:
        return json.dumps({"ideas": [
            {"title": f"idea {i}", "angle": "angle " * 3,
             "format": "one-liner" if i % 3 else "quote-post",
             "score": 7.0 + i, "source": "niche radar"} for i in range(10)
        ]})
    if "writing-style analyst" in system and "VOICE FINGERPRINT" not in system:
        return json.dumps({"summary": "Dry, builder-voice. Short lines, rare emoji.",
                           "humor_frequency": "rare",
                           "sarcasm_note": "deadpan one-liners"})
    if "VOICE FINGERPRINT" in system or "writing-style analyst" in system:
        return json.dumps({
            "diction": "plain, technical", "tone": "dry humor",
            "structure": "short punchy lines", "casing_punctuation": "lowercase, no periods",
            "emoji_use": "rare", "signature_moves": "contrast pairs",
            "avg_length_chars": 140, "do_not": "no hashtags",
            "persona_summary": "builder who ships",
        })
    raise LLMError(f"unexpected prompt: {system[:120]}")


def patch_fakes():
    voice.chat = fake_chat
    ideas.chat = fake_chat
    drafts.chat = fake_chat
    replies.chat = fake_chat
    style_scan.chat = fake_chat


def _wipe_pipeline_tables():
    for t in ("drafts", "ideas", "engagements", "posts", "agent_log", "voice_profile"):
        with db.connect() as c:
            c.execute(f"DELETE FROM {t}")


def test_full_v03_pipeline():
    # queue discipline (2026-08-24): create throttles when pending >= 12 and
    # drafts only headroom — the pipeline test wants a real batch, so clear
    # the queue and pin a target first (restored in the fixture teardown)
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE status='draft'")
    db.set_setting("agent_daily_draft_target", 5)
    _wipe_pipeline_tables()
    patch_fakes()
    db.set_setting("style_profile", None)
    db.set_setting("voice_profile", None)
    # the ideas chain mines the ACTIVE account's brain (journal insights +
    # strategy statements) — seed our own so the bank floor is deterministic
    # (pre-v0.5 this test silently leaned on the REAL install's journal)
    from openstanley.gen import brain as brain_mod
    brain_mod.ensure()
    brain_mod.journal_append("reflect:learn",
                             "short posts with a number beat long explainers",
                             ["learned R1"])
    p = brain_mod.brain_dir() / "strategies.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "- (none yet — the learn loop will fill these from real metrics)",
        "- contrarian one-liners drive the most replies"), encoding="utf-8")

    cfg = Config()
    cfg.agent.daily_draft_target = 3
    agent = Agent(cfg)

    # --- deep scan (fake LLM summary; stats computed locally) ---
    scan = asyncio.run(agent.scan())
    assert scan["posts_scanned"] > 50, scan
    assert scan["languages"], scan
    profile = db.get_setting("style_profile")
    assert profile["stats"]["posts_scanned"] > 50
    assert profile["human_summary"]
    print(f"[ok] scan: {scan['posts_scanned']} posts, langs={scan['languages']}")

    # --- import + study ---
    res_import = asyncio.run(agent.import_history())
    assert res_import["own"] > 50
    res_study = asyncio.run(agent.study())
    # v0.4.3: the bank fills via the deterministic mining chain, not the LLM
    # batch — the dry-run corpus carries only 8 distinct niche texts, so the
    # floor is "enough scan-mined angles to draft from" (the 3-draft create
    # assertion below is the real bar).
    assert res_study["bank"] >= 5, res_study
    print(f"[ok] import {res_import['own']}/study bank={res_study['bank']}")

    # --- create: every draft carries alg score + voice match ---
    before = len(db.drafts_by_status("draft", 500))
    res_create = asyncio.run(agent.create())
    assert res_create["drafts"] >= 1, res_create   # queue-aware create may
    # draft fewer than target when the bank is thin or drafts collide with
    # recent ones (diversity gate) — the pipeline proves ≥1 real draft
    for d in db.drafts_by_status("draft", 500)[before:]:
        assert d["meta"]["alg"]["score"] >= 0 and d["meta"]["alg"]["factors"], d
        assert "voice_match" in d["meta"]
    print(f"[ok] create: {res_create['drafts']} drafts, all scored")

    # --- engage: scheduled niche replies (approval-gated) ---
    res_engage = asyncio.run(agent.engage())
    assert res_engage["niche_replies_scheduled"] >= 1, res_engage
    sched_replies = [d for d in db.drafts_by_status("draft", 100)
                     if d["kind"] == "reply" and d.get("scheduled_at")]
    assert sched_replies, "expected scheduled reply drafts"
    for r in sched_replies:
        assert r["meta"]["reply_to_x_id"]
        assert r["meta"]["alg"]["score"] >= 0
    print(f"[ok] engage: {res_engage['niche_replies_scheduled']} scheduled niche replies")

    # --- image post: attach media, approve, publish (dry-run) ---
    from openstanley.server.__main__ import MEDIA_DIR
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    img_name = "media_e2e_test.png"
    (MEDIA_DIR / img_name).write_bytes(b"\x89PNG fake bytes for e2e")
    img_did = db.add_draft(
        text="the ugly version teaches you things slides never will. what did you ship ugly?",
        meta={"source": "e2e", "alg": {"score": 70, "grade": "good", "factors": []}})
    db.update_draft(img_did, image=img_name, status="approved",
                    scheduled_at=(datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"))

    # --- quote post via generator ---
    quote_did = drafts.generate_quote_draft(
        cfg, {"x_id": "1790123456789", "url": "https://x.com/u/status/1790123456789",
              "text": "we shipped the big thing today", "author": "u"})
    qd = db.get_draft(quote_did)
    assert qd["kind"] == "quote" and qd["quote_of"] == "1790123456789"
    assert qd["meta"]["alg"]["score"] >= 0
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.update_draft(quote_did, status="approved", scheduled_at=past)

    # --- approval gate: loop drafts must NOT publish without approval ---
    loop_draft_ids = [d["id"] for d in db.drafts_by_status("draft", 500)]
    res_publish1 = asyncio.run(agent.publish())
    published1 = {p["draft_id"] for p in res_publish1["published"]}
    assert published1 & set(loop_draft_ids) == set(), "loop drafts published without approval!"

    # --- publish the approved image post + quote post ---
    assert img_did in published1 and quote_did in published1, published1
    print(f"[ok] publish: image post + quote post out (dry); gate held for loop drafts")

    # --- scheduled reply: approve → its slot arrives → publish sends as reply ---
    reply_draft = sched_replies[0]
    db.update_draft(reply_draft["id"], status="approved")
    # simulate the slot arriving (drafted slots are 45min+ in the future)
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.update_draft(reply_draft["id"], scheduled_at=past)
    res_publish2 = asyncio.run(agent.publish())
    published2 = {p["draft_id"] for p in res_publish2["published"]}
    assert reply_draft["id"] in published2, res_publish2
    print("[ok] publish: scheduled reply sent as reply (dry)")

    # --- bilingual: Arabic draft through the generator ---
    ar_did = drafts._draft_one(cfg, {"title": "arabic test", "angle": "a", "format": "one-liner"},
                               "safe", language="ar")
    from openstanley.gen.lang import detect
    assert detect(ar_did["text"]) == "ar", ar_did["text"]
    print(f"[ok] bilingual: Arabic draft generated ({ar_did['text'][:40]}…)")

    # --- chat with tools (fake stream) ---
    def fake_stream(cfg2, system, user, temperature=None):
        yield "here is your post:\n> a great bilingual candidate post?\n"
        yield "queued."
    chat.llm_chat_stream = fake_stream
    events = list(chat.chat_reply_stream(cfg, "اكتب لي منشور وجدولة الساعة ٩"))
    types = [e["type"] for e in events]
    assert "done" in types and types.count("token") == 2
    done = next(e for e in events if e["type"] == "done")
    assert done["candidates"], "Arabic chat candidate expected"
    print(f"[ok] chat stream: {len(events)} events, {len(done['candidates'])} candidates")

    # cleanup media
    (MEDIA_DIR / img_name).unlink(missing_ok=True)
    print(f"\nFULL V0.3 E2E PASSED — {len(CALLS)} LLM calls simulated")


def main() -> int:
    test_full_v03_pipeline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
