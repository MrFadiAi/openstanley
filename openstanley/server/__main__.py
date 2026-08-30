"""FastAPI server + APScheduler — `python -m openstanley.server`."""
from __future__ import annotations

import asyncio
import json
import queue
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool

from ..core import db
from ..core.config import load_config
from ..gen.agent import Agent
from ..gen import autopilot as ap_mod
from ..gen import digest as digest_mod
from ..gen import drafts as drafts_mod
from ..gen import slots as slots_mod
from ..integrations import telegram as telegram_mod

ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = ROOT / "web"
DIST_DIR = WEB_DIR / "dist"
MEDIA_DIR = ROOT / "data" / "media"
ALLOWED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg",
                       "image/webp": ".webp", "image/gif": ".gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

cfg = load_config()
db.init_db()
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
agent = Agent(cfg)
app = FastAPI(title="OpenStanley", version="0.3.0")


# ---------------- models ----------------

class DraftAction(BaseModel):
    text: str | None = None
    scheduled_at: str | None = None


class DraftCreate(BaseModel):
    text: str
    kind: str = "post"
    image: str | None = None
    quote_of: dict | None = None
    language: str | None = None
    scheduled_at: str | None = None


class QuoteSet(BaseModel):
    url: str
    text: str | None = None
    author: str | None = None


class Reschedule(BaseModel):
    scheduled_at: str


class AttachImage(BaseModel):
    image: str | None = None


class SettingsUpdate(BaseModel):
    daily_draft_target: int | None = None
    post_times: list[str] | None = None
    niche_accounts: list[str] | None = None
    evergreen_themes: list[str] | None = None
    auto_approve_replies: bool | None = None
    smart_slots: bool | None = None
    language: str | None = None
    voice_temperature: str | None = None
    voice_formality: int | None = None
    voice_lang_mix: int | None = None
    voice_emoji_density: int | None = None
    voice_lock_enabled: bool | None = None
    voice_lock_threshold: int | None = None
    digest_webhook_url: str | None = None
    digest_hour: int | None = None
    # v0.4.4 telegram — token is write-only (masked in GET, never logged)
    tg_bot_token: str | None = None
    tinyfish_api_key: str | None = None
    tg_allowed_chats: list[str] | None = None
    tg_enabled: bool | None = None


class CookieConnect(BaseModel):
    cookies_json: str
    username: str | None = None


class AccountCreate(BaseModel):
    handle: str
    cookies_json: str | None = None   # optional — can be pasted later


class AccountCookies(BaseModel):
    cookies_json: str


# every cookie paste goes through one normalizer (x/client.py) — the user
# never hand-builds JSON. This is the 400 when nothing can be extracted.
COOKIES_PASTE_HINT = ("Paste your auth_token (the long token from the x.com "
                      "'auth_token' cookie), or the full JSON — either works")


class CapSettings(BaseModel):
    max_posts_per_day: int | None = None
    max_replies_per_day: int | None = None
    min_delay_s: int | None = None
    max_delay_s: int | None = None


class AutopilotUpdate(BaseModel):
    enabled: bool
    interval_min: int | None = None


# ---------------- dashboard ----------------

@app.get("/")
async def index():
    if (DIST_DIR / "index.html").exists():
        return FileResponse(DIST_DIR / "index.html")
    if (WEB_DIR / "index.html").exists():
        return FileResponse(WEB_DIR / "index.html")
    raise HTTPException(404, "no frontend built (web/dist) — run npm run build in web/")


# built React app assets (Vite emits to dist/assets)
if (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")


@app.get("/api/health")
async def health():
    return {"ok": True, "mode": agent.x.mode, "time": datetime.now().isoformat(timespec="seconds")}


# ---------------- loops (manual trigger) ----------------

async def run_loop_core(name: str) -> dict:
    """One agent loop by name → its raw result dict. Shared core for the API
    route below and the Telegram /study chain (identical code path); errors
    are logged here, then raised to the caller."""
    fns = {"import": agent.import_history, "study": agent.study, "create": agent.create,
           "engage": agent.engage, "mentions": agent.mentions, "publish": agent.publish,
           "learn": agent.learn, "scan": agent.scan}
    try:
        return await fns[name]()
    except Exception as e:  # noqa: BLE001
        db.log("api", f"loop {name} error: {e}", level="error")
        raise


async def _run_loop(name: str):
    try:
        result = await run_loop_core(name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e
    return JSONResponse({"ok": True, "loop": name, "result": result})


@app.post("/api/loops/{name}")
async def run_loop(name: str):
    if name not in ("import", "study", "create", "engage", "mentions",
                    "publish", "learn", "scan"):
        raise HTTPException(404, f"unknown loop {name}")
    return await _run_loop(name)


# ---------------- mention inbox (v0.3.9) ----------------

@app.get("/api/mentions")
async def mentions_ep(pending: int = 1, limit: int = 50):
    """Normalized mentions with draft status (pending=1 → unhandled only)."""
    from ..gen import mentions as mentions_mod
    limit = max(1, min(int(limit), 100))
    return mentions_mod.mentions_view(pending_only=bool(pending), limit=limit)


@app.post("/api/mentions/{x_id}/draft")
async def mention_draft_ep(x_id: str):
    """Draft a reply to one mention on demand (Inbox [draft reply] button)."""
    from ..gen import mentions as mentions_mod
    with db.connect() as c:
        row = c.execute("SELECT * FROM seen_mentions WHERE account_id=? AND x_id=?",
                        (db.active_account(), x_id)).fetchone()
    if not row:
        raise HTTPException(404, f"unknown mention {x_id}")
    did = await asyncio.to_thread(mentions_mod.draft_mention_reply,
                                  cfg, dict(row))
    if not did:
        raise HTTPException(500, "draft failed — see agent log")
    return {"ok": True, "draft_id": did}


# ---------------- autopilot (self-driving agent; publish stays human-gated) ----------------

def _autopilot_view() -> dict:
    view = ap_mod.get_state()
    view["interval_min"] = ap_mod.interval_minutes(cfg)
    view["job_active"] = bool(scheduler and scheduler.get_job("autopilot"))
    return view


async def _autopilot_job():
    """Scheduler callback — reads the CURRENT agent (rebuilt after reconnects)."""
    await ap_mod.run_tick(agent, cfg)


@app.get("/api/autopilot")
async def autopilot_get():
    return _autopilot_view()


@app.post("/api/autopilot")
async def autopilot_set(body: AutopilotUpdate):
    if body.interval_min is not None:
        if not 1 <= body.interval_min <= 1440:
            raise HTTPException(400, "interval_min must be 1-1440 minutes")
        cfg.agent.autopilot_interval_min = body.interval_min
        db.set_setting("agent_autopilot_interval_min", body.interval_min)
    ap_mod.set_enabled(body.enabled)
    if scheduler:
        from apscheduler.triggers.interval import IntervalTrigger
        if body.enabled:
            scheduler.add_job(_autopilot_job,
                              IntervalTrigger(minutes=ap_mod.interval_minutes(cfg),
                                              jitter=ap_mod.JITTER_MAX_S),
                              id="autopilot", replace_existing=True)
        else:
            if scheduler.get_job("autopilot"):
                scheduler.remove_job("autopilot")
    db.log("system", f"autopilot {'enabled' if body.enabled else 'disabled'}"
                     f" (interval {ap_mod.interval_minutes(cfg)}m)")
    return _autopilot_view()


@app.post("/api/autopilot/tick")
async def autopilot_tick():
    """Force one tick now (any phase result returned; used by tests)."""
    result = await ap_mod.run_tick(agent, cfg)
    return {"ok": result["ok"], **result, "state": _autopilot_view()}


# ---------------- daily digest (v0.4.2 — the agent reports to its owner) ----------------

class DigestSendBody(BaseModel):
    day: str | None = None
    lang: str | None = None
    force: bool = True   # API sends on demand; the cron job passes force=False


@app.get("/api/digest")
async def digest_get(day: str | None = None):
    """Today's digest rendered (built fresh); a stored day serves its file."""
    if day:
        stored = digest_mod.read_stored(day[:10])
        if stored is not None:
            return {"day": day[:10], "markdown": stored, "text": None,
                    "stored": True}
    d = await asyncio.to_thread(digest_mod.build_digest, cfg, day)
    lang = str(db.get_setting("language") or "en")
    return {"day": d.day, "markdown": digest_mod.render_markdown(d, lang),
            "text": digest_mod.render_text(d, lang), "stored": False}


@app.get("/api/digest/history")
async def digest_history_ep(limit: int = 7):
    return {"days": digest_mod.history(max(1, min(int(limit), 31)))}


@app.post("/api/digest/send")
async def digest_send_ep(body: DigestSendBody):
    """Build today's (or `day`'s) digest, store it, POST it to the webhook."""
    if body.lang not in (None, "en", "ar"):
        raise HTTPException(400, "lang must be en|ar")
    result = await asyncio.to_thread(digest_mod.deliver, cfg, body.day,
                                     body.lang, body.force)
    return result


async def _digest_job():
    """Scheduler callback — daily report, blocking send off the event loop."""
    await asyncio.to_thread(digest_mod.deliver, cfg, None, None, False)


# ---------------- telegram (v0.4.4 — second frontend) ----------------

@app.post("/api/telegram/test")
async def telegram_test_ep():
    """Send "OpenStanley online" to the first allowed chat (Settings test button)."""
    chats = telegram_mod.allowed_chats()
    if len(telegram_mod.bot_token()) < telegram_mod.TOKEN_MIN_LEN:
        raise HTTPException(400, "no bot token configured")
    if not chats:
        raise HTTPException(400, "no allowed chat ids configured — message the "
                                 "bot once, then add the chat id it replies")
    result = await asyncio.to_thread(
        telegram_mod.send_message, chats[0],
        "🤖 OpenStanley online — Telegram connected. /help for what I can do.")
    if not result["ok"]:
        raise HTTPException(400, f"send failed: {result.get('error')}")
    return {"ok": True, "chat_id": chats[0], "status_code": result["status_code"]}


@app.get("/api/telegram/status")
async def telegram_status_ep():
    """Poller state for the Settings status line (no secrets)."""
    return telegram_mod.status()


# ---------------- media (draft images) ----------------

@app.post("/api/media")
async def upload_media(file: UploadFile = File(...)):
    ext = ALLOWED_IMAGE_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(400, f"unsupported image type {file.content_type} "
                                 f"(png/jpg/webp/gif only)")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "image too large (max 5MB)")
    name = f"media_{int(datetime.now().timestamp())}_{secrets.token_hex(3)}{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(data)
    db.log("media", f"uploaded {name} ({len(data)} bytes)")
    return {"ok": True, "name": name, "url": f"/api/media/{name}"}


@app.get("/api/media/{name}")
async def serve_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = MEDIA_DIR / name
    if not p.exists():
        raise HTTPException(404, "no such media")
    return FileResponse(p)


# ---------------- quote preview ----------------

def _tweet_id_from_url(url: str) -> str | None:
    m = re.search(r"x\.com/(\w+)/status/(\d+)", url or "")
    if m:
        return m.group(2)
    if re.fullmatch(r"\d{10,}", (url or "").strip()):
        return url.strip()
    return None


@app.get("/api/tweet")
async def tweet_preview(url: str):
    x_id = _tweet_id_from_url(url)
    if not x_id:
        raise HTTPException(400, "not a tweet URL (expected x.com/<user>/status/<id>)")
    tweet = await agent.x.get_tweet(x_id)
    return {"x_id": x_id, "text": tweet.get("text", ""), "author": tweet.get("author", "")}


# ---------------- dashboard data ----------------

@app.get("/api/stats")
async def stats():
    return db.dashboard_stats()


@app.get("/api/drafts")
async def list_drafts(status: str = "draft", limit: int = 100):
    return db.drafts_by_status(status, limit)


@app.get("/api/ideas")
async def list_ideas(limit: int = 50):
    return db.fresh_ideas(limit)


@app.get("/api/ideas/bank")
async def ideas_bank_health():
    """v0.4.3 bank health — count + last replenish record (Ideas page chip)."""
    from ..gen import ideas as ideas_mod
    return ideas_mod.bank_health()


@app.post("/api/ideas/replenish")
async def ideas_replenish():
    """v0.4.3 manual trigger — runs the deterministic mining chain, returns
    what was added and from which sources."""
    from ..gen import ideas as ideas_mod
    return await ideas_mod.replenish(cfg, x=agent.x)


@app.get("/api/queue")
async def queue():
    return db.drafts_by_status("approved", 200)


@app.get("/api/log")
async def recent_log(limit: int = 60):
    with db.connect() as c:
        rows = c.execute("SELECT * FROM agent_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/analytics")
async def analytics():
    posts = db.own_posts(limit=200)
    if not posts:
        return {"posts": [], "summary": {}}
    total_imp = sum(p["impressions"] or 0 for p in posts)
    total_eng = sum((p["likes"] + 2 * p["reposts"] + 3 * p["replies"]) for p in posts)
    best = sorted(posts, key=lambda p: p["likes"] + p["reposts"], reverse=True)[:8]
    return {
        "posts": [{
            "x_id": p["x_id"], "text": p["text"], "created_at": p["created_at"],
            "impressions": p["impressions"], "likes": p["likes"], "reposts": p["reposts"],
            "replies": p["replies"], "engagement": p["engagement"],
        } for p in posts[:60]],
        "summary": {
            "total_impressions": total_imp,
            "total_engagement": total_eng,
            "avg_engagement_rate": round(total_eng / max(total_imp, 1), 5),
            "best_posts": [{"text": p["text"][:140], "likes": p["likes"]} for p in best],
        },
    }


# ---------------- growth analytics (v0.3.6 — real metrics ground truth) ----------------

@app.get("/api/analytics/growth")
async def analytics_growth(days: int = 14):
    """Daily followers (identity snapshots) + posts + avg engagement rate."""
    from ..gen import metrics as metrics_mod
    return metrics_mod.growth_series(days)


@app.get("/api/analytics/top")
async def analytics_top(limit: int = 10, days: int = 30):
    """Top own posts by follower-normalized engagement rate in the window."""
    from ..gen import metrics as metrics_mod
    return {"posts": metrics_mod.top_posts(limit, days)}


@app.get("/api/analytics/times")
async def analytics_times(days: int = 60):
    """Engagement by hour-of-day from own posts (real once >=20 posts),
    plus the scheduler's own reason per candidate hour (single source)."""
    from ..gen import metrics as metrics_mod
    data = metrics_mod.times_of_day(days)
    data["reasons"] = slots_mod.hour_reasons(cfg)
    return data


@app.get("/api/voice")
async def get_voice():
    v = db.load_voice()
    if not v:
        return {"rubric": None, "examples": [], "updated_at": None}
    try:
        v["rubric"] = json.loads(v["rubric"])
    except (json.JSONDecodeError, TypeError):
        pass
    return v


# ---------------- mutations ----------------

@app.post("/api/drafts")
async def create_draft(body: DraftCreate):
    from ..gen.algorithm import score_draft
    from ..gen.lang import detect as lang_detect
    from ..gen.style_scan import voice_match
    x_id = None
    if body.quote_of:
        x_id = _tweet_id_from_url(str(body.quote_of.get("url", "")) or "") \
            or str(body.quote_of.get("x_id", "") or "")
        if not x_id:
            raise HTTPException(400, "quote_of needs a tweet url or x_id")
    alg = score_draft(body.text, now_hour=None)
    did = db.add_draft(
        text=body.text, kind="quote" if x_id else body.kind, image=body.image,
        quote_of=x_id, scheduled_at=body.scheduled_at,
        meta={"source": "manual", "language": body.language or lang_detect(body.text),
              "alg": alg, "voice_match": voice_match(body.text),
              **({"quote": body.quote_of} if body.quote_of else {})})
    return {"ok": True, "draft_id": did}


@app.post("/api/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, body: DraftAction | None = None):
    body = body or DraftAction()
    d = db.get_draft(draft_id)
    if not d or d["status"] not in ("draft", "approved"):
        raise HTTPException(404, "draft not found in 'draft' status")
    # keep an already-proposed slot (e.g. scheduled reply) unless overridden;
    # slotless drafts get a SMART slot (metrics/spread/freshness) when the
    # flag is on, else the v0.3 static cadence — bit-identical when off
    proposed = d.get("scheduled_at")
    sched = body.scheduled_at or proposed
    reason = (d.get("meta") or {}).get("scheduled_reason")
    if body.scheduled_at and body.scheduled_at != proposed:
        reason = None  # manual override — the old reason no longer describes the slot
    if sched is None:
        if cfg.agent.smart_slots:
            picked, reason = slots_mod.pick_slot_with_reason(
                cfg, d.get("kind") or "post", datetime.now())
            sched = picked.isoformat(timespec="seconds")
        else:
            sched = _next_slot()
    meta = d.get("meta") or {}
    if reason:
        meta["scheduled_reason"] = reason
    db.update_draft(draft_id, status="approved",
                    text=body.text or d["text"],
                    scheduled_at=sched,
                    meta_json=meta)
    return {"ok": True, "scheduled_at": sched, "scheduled_reason": reason}


def _next_slot():
    """Next FREE cadence slot — collision-aware like every other picker, so
    mass-approving on the web Inbox can never stack onto one timestamp."""
    from ..gen.slots import nudge_free, taken_slots
    times = cfg.agent.post_times
    now = datetime.now()
    for offset_days in range(3):
        for t in times:
            hh, mm = map(int, t.split(":"))
            slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=offset_days)
            if slot > now:
                at, _why = nudge_free(slot, cfg, taken_slots())
                return at.isoformat(timespec="seconds")
    return (now + timedelta(hours=1)).isoformat(timespec="seconds")


@app.post("/api/drafts/{draft_id}/edit")
async def edit_draft(draft_id: int, body: DraftAction):
    db.update_draft(draft_id, text=body.text or None)
    return {"ok": True}


@app.post("/api/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int):
    from ..gen import rejection_learn
    d = next((d for d in db.drafts_by_status("draft", 500) + db.drafts_by_status("approved", 500)
              if d["id"] == draft_id), None)
    if not d:
        raise HTTPException(404, "draft not found")
    db.update_draft(draft_id, status="rejected")
    # the owner's NO teaches: stamp why it died; the brain reflects once
    # enough rejections accumulate (daemon thread — the tap stays instant)
    rejection_learn.record_rejection(draft_id, reason="owner", via="web")
    rejection_learn.maybe_reflect_async(cfg)
    return {"ok": True}


@app.post("/api/drafts/{draft_id}/regenerate")
async def regen_draft(draft_id: int):
    try:
        new_id = await asyncio.to_thread(drafts_mod.regenerate, draft_id)
        return {"ok": True, "new_draft_id": new_id}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


@app.post("/api/deep-train")
async def deep_train_ep():
    """Deep-train the brain on the ACTIVE account — the full immersion
    chain (history+replies 800, metrics, voice rebuild, niche, reflection).
    Read-only on X; can take several minutes."""
    try:
        res = await agent.deep_train()
        return {"ok": True, "report": res}
    except Exception as e:  # noqa: BLE001
        db.log("api", f"deep train error: {e}", level="error")
        raise HTTPException(500, str(e)) from e


@app.post("/api/rejection-learn")
async def rejection_learn_ep():
    """Force one rejection-learning pass now (the nightly job also runs one
    at 04:17 before the expiry sweep)."""
    from ..gen import rejection_learn
    try:
        res = await asyncio.to_thread(rejection_learn.run_reflection, cfg)
        return {"ok": True, "result": res}
    except Exception as e:  # noqa: BLE001
        db.log("api", f"rejection-learn error: {e}", level="error")
        raise HTTPException(500, str(e)) from e


@app.get("/api/watchdog")
async def watchdog_ep():
    """Chat watchdog status: chat LLM health, tool failure rate, chat-draft
    burst guard, TG handler streak."""
    from ..system import watchdog
    return {"ok": True, **watchdog.status()}


@app.post("/api/metrics/refresh")
async def metrics_refresh_ep():
    """Nightly metrics refresh — keeps Insights (heatmap/growth/milestones)
    current without a full study/scan run."""
    from ..gen import metrics as metrics_mod
    try:
        res = await metrics_mod.refresh_metrics(agent.x, cfg, limit=20)
        return {"ok": True, "result": res}
    except Exception as e:  # noqa: BLE001
        db.log("api", f"metrics refresh error: {e}", level="error")
        raise HTTPException(500, str(e)) from e


class ThreadTopic(BaseModel):
    topic: str


_THREAD_SYSTEM = (
    "You write a short X THREAD (3-6 tweets) in the user's voice. Output "
    'STRICT JSON: {"thread": ["tweet 1", "tweet 2", ...]}. Rules: the first '
    "tweet is the hook (under 200 chars, scroll-stopping); every tweet under "
    "270 chars; each ends clean (no '1/'); plain text, no hashtags."
)


@app.post("/api/threads")
async def threads_ep(body: ThreadTopic):
    """Thread composer — topic -> a 3-6 tweet thread draft (approval-gated;
    the publish loop ships it via post_thread)."""
    from ..gen.llm import chat, extract_json
    from ..gen import voice as voice_mod
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic required")
    voice = voice_mod.load_rubric() if hasattr(voice_mod, "load_rubric") else ""
    user = (f"TOPIC: {topic}" + chr(10) +
            f"USER VOICE: {str(voice)[:400]}" + chr(10) + chr(10) +
            "Write the thread now.")
    raw = await asyncio.to_thread(chat, cfg.llm, _THREAD_SYSTEM, user,
                                  None, True)
    try:
        data = extract_json(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"thread generation failed — try again ({e})") from e
    thread = ([str(t).strip() for t in (data.get("thread") or []) if str(t).strip()]
              if isinstance(data, dict) else [])
    if len(thread) < 2:
        raise HTTPException(500, "thread generation failed — try again")
    did = db.add_draft(text=thread[0], thread=thread, kind="post",
                       temperature="bold",
                       meta={"source": "thread-composer", "topic": topic})
    db.log("threads", f"thread drafted #{did}: {len(thread)} tweets on '{topic[:40]}'")
    return {"ok": True, "draft_id": did, "tweets": len(thread)}


@app.get("/api/dms")
async def dms_ep():
    """Read-only DM triage — what the current X mode can honestly see."""
    res = await agent.x.get_dms()
    return res


@app.get("/api/hooks")
async def hooks_ep():
    """Steal-this-hook bank — patterns mined from niche winners."""
    from ..gen import hooks as hooks_mod
    return {"hooks": hooks_mod.list_hooks()}


@app.post("/api/hooks/extract")
async def hooks_extract_ep():
    """(Re)mine patterns from the stored niche winners."""
    from ..gen import hooks as hooks_mod
    return await asyncio.to_thread(hooks_mod.extract, cfg)


@app.post("/api/hooks/{hook_id}/remix")
async def hooks_remix_ep(hook_id: int):
    """Pattern -> a fresh draft in the user's voice (approval-gated)."""
    from ..gen import hooks as hooks_mod
    did = await asyncio.to_thread(hooks_mod.remix, cfg, hook_id)
    if not did:
        raise HTTPException(404, f"no hook {hook_id}")
    return {"ok": True, "draft_id": did}


@app.get("/api/insights/overview")
async def insights_overview_ep():
    """Insights v2 — every aggregate the redesigned page renders, real data."""
    from ..gen import insights as insights_mod
    return insights_mod.overview()


@app.post("/api/posts/{x_id}/repost")
async def repost_post(x_id: str):
    """Best-of wall → a fresh draft in the queue. Approval-gated, as ever."""
    with db.connect() as c:
        row = c.execute(
            "SELECT p.text, d.image AS draft_image FROM posts p "
            "LEFT JOIN drafts d ON d.x_id = p.x_id "
            "WHERE p.x_id=? AND p.account_id=?",
            (x_id, db.active_account())).fetchone()
    if not row or not row["text"]:
        raise HTTPException(404, "no such post")
    did = db.add_draft(text=row["text"], kind="post",
                       meta={"source": "repost", "original_x_id": x_id},
                       image=row["draft_image"], acct=db.active_account())
    db.log("insights", f"repost: post {x_id} → draft #{did} (approval-gated)")
    return {"ok": True, "draft_id": did}


@app.post("/api/drafts/clear-scheduled")
async def clear_scheduled_drafts():
    """Calendar 'clear schedule' — delete all placed-but-unpublished drafts."""
    n = db.delete_scheduled_drafts()
    db.log("calendar", f"schedule cleared — {n} scheduled drafts deleted")
    return {"ok": True, "deleted": n}


@app.post("/api/drafts/clear-queue")
async def clear_queued_drafts():
    """Calendar 'clear queue' — delete every unscheduled draft."""
    n = db.delete_queued_drafts()
    db.log("calendar", f"queue cleared — {n} unscheduled drafts deleted")
    return {"ok": True, "deleted": n}


@app.post("/api/drafts/{draft_id}/reschedule")
async def reschedule_draft(draft_id: int, body: Reschedule):
    d = db.get_draft(draft_id)
    if not d or d["status"] not in ("approved", "draft"):
        raise HTTPException(404, "draft not found")
    if not re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", body.scheduled_at or ""):
        raise HTTPException(400, "scheduled_at must be ISO (YYYY-MM-DDTHH:MM:SS)")
    db.update_draft(draft_id, scheduled_at=body.scheduled_at,
                    status="approved" if d["status"] == "approved" else d["status"])
    return {"ok": True, "scheduled_at": body.scheduled_at}


@app.post("/api/drafts/{draft_id}/attach")
async def attach_media(draft_id: int, body: AttachImage):
    if body.image and ("/" in body.image or "\\" in body.image or ".." in body.image):
        raise HTTPException(400, "bad image name")
    if body.image and not (MEDIA_DIR / body.image).exists():
        raise HTTPException(404, "no such media file")
    db.update_draft(draft_id, image=body.image)
    return {"ok": True, "image": body.image}


@app.post("/api/drafts/{draft_id}/quote")
async def set_quote(draft_id: int, body: QuoteSet):
    x_id = _tweet_id_from_url(body.url)
    if not x_id:
        raise HTTPException(400, "not a tweet URL")
    text, author = body.text, body.author
    if not text or not author:  # fill in from a live fetch when we can
        try:
            t = await agent.x.get_tweet(x_id)
            text = text or t.get("text")
            author = author or t.get("author")
        except Exception:  # noqa: BLE001  dry-run / offline → keep what we have
            pass
    meta = db.get_draft(draft_id)
    if not meta:
        raise HTTPException(404, "draft not found")
    m = meta.get("meta") or {}
    m["quote"] = {"x_id": x_id, "url": body.url, "text": text or "",
                  "author": author or ""}
    db.update_draft(draft_id, kind="quote", quote_of=x_id, meta_json=m)
    return {"ok": True, "quote_of": m["quote"]}


@app.post("/api/drafts/{draft_id}/score")
async def score_draft_ep(draft_id: int):
    from ..gen.algorithm import score_draft_row
    d = db.get_draft(draft_id)
    if not d:
        raise HTTPException(404, "draft not found")
    alg = score_draft_row(d)
    m = d.get("meta") or {}
    m["alg"] = alg
    db.update_draft(draft_id, meta_json=m)
    return {"ok": True, "alg": alg}


@app.post("/api/replies/{draft_id}/send")
async def send_reply(draft_id: int):
    """Send an approved reply draft (engage loop output)."""
    d = db.get_draft(draft_id)
    if not d or d["kind"] != "reply":
        raise HTTPException(404, "reply draft not found")
    reply_to = d["meta"].get("reply_to_x_id")
    try:
        res = await agent.x.post_tweet(d["text"], reply_to=reply_to)
        db.update_draft(draft_id, status="published", x_id=res.get("x_id"),
                        published_at=datetime.now().isoformat(timespec="seconds"))
        eid = d["meta"].get("engagement_id")
        if eid:
            from ..gen import replies as replies_mod
            replies_mod.mark_replied(eid)
        return {"ok": True, **res}
    except Exception as e:  # noqa: BLE001
        db.update_draft(draft_id, status="failed")
        raise HTTPException(500, str(e)) from e


@app.post("/api/ideas/{idea_id}/discard")
async def discard_idea(idea_id: int):
    db.mark_idea(idea_id, "discarded")
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    return {
        "daily_draft_target": cfg.agent.daily_draft_target,
        "post_times": cfg.agent.post_times,
        "niche_accounts": cfg.agent.niche_accounts,
        "evergreen_themes": cfg.agent.evergreen_themes,
        "auto_approve_replies": cfg.agent.auto_approve_replies,
        "smart_slots": cfg.agent.smart_slots,
        "language": db.get_setting("language", "en"),
        "voice_temperature": db.get_setting("voice_temperature", "bold"),
        "voice_formality": db.get_setting("voice_formality", 50),
        "voice_lang_mix": db.get_setting("voice_lang_mix", 50),
        "voice_emoji_density": db.get_setting("voice_emoji_density", 3),
        "voice_lock_enabled": db.get_setting("voice_lock_enabled",
                                             cfg.agent.voice_lock_enabled),
        "voice_lock_threshold": db.get_setting("voice_lock_threshold",
                                               cfg.agent.voice_lock_threshold),
        "digest_webhook_url": digest_mod.mask_webhook_url(
            digest_mod.webhook_url()),
        "digest_webhook_set": bool(digest_mod.webhook_url()),
        "digest_hour": digest_mod.digest_hour(cfg),
        "digest_last_sent": (db.get_acct_setting("digest_last") or {}).get("at"),
        # v0.4.4 telegram — token masked like the webhook, never the value
        "tg_bot_token": telegram_mod.mask_token(telegram_mod.bot_token()),
        "tinyfish_api_key": (str(db.get_setting("tinyfish_api_key") or "")[:6] + "…" if db.get_setting("tinyfish_api_key") else ""),
        "tg_bot_set": len(telegram_mod.bot_token()) >= telegram_mod.TOKEN_MIN_LEN,
        "tg_allowed_chats": telegram_mod.allowed_chats(),
        "tg_enabled": telegram_mod.is_enabled(),
        "tg_status": telegram_mod.status()["state"],
        "x_mode": cfg.x.mode,
        "llm_model": cfg.llm.model,
        "llm_base_url": cfg.llm.base_url,
    }


@app.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    for k, v in body.dict().items():
        if v is None:
            continue
        if k == "language":  # UI preference, not an agent knob
            if v not in ("en", "ar"):
                continue
            db.set_setting("language", v)
            continue
        if k == "digest_webhook_url":  # delivery target — db-only, masked in GET
            url = str(v).strip()
            if url and not url.startswith(("http://", "https://")):
                continue
            db.set_setting("digest_webhook_url", url)
            continue
        if k == "digest_hour":  # reschedule the live job when the hour moves
            hour = max(0, min(23, int(v)))
            cfg.agent.digest_hour = hour
            db.set_setting("agent_digest_hour", hour)
            if scheduler and scheduler.get_job("digest"):
                from apscheduler.triggers.cron import CronTrigger
                scheduler.add_job(_digest_job, CronTrigger(hour=hour, minute=0),
                                  id="digest", replace_existing=True)
                db.log("digest", f"digest job moved to {hour:02d}:00")
            continue
        if k == "tinyfish_api_key":  # free tinyfish.ai key — masked in GET
            key = str(v).strip()
            if key and not key.startswith("tf_") and len(key) < 16:
                continue  # not plausible — ignore, keep stored one
            db.set_setting("tinyfish_api_key", key)
            db.log("websearch", "tinyfish key updated — search routes through "
                                "it first ($0), DDG stays the fallback")
            continue
        if k == "tg_bot_token":  # secret — stored, masked in GET, never logged
            token = str(v).strip()
            if token and (len(token) < telegram_mod.TOKEN_MIN_LEN
                          or any(ch.isspace() for ch in token)):
                continue  # not a plausible bot token — ignore, keep stored one
            db.set_setting("tg_bot_token", token)
            db.log("telegram", "bot token updated — restarting poller")
            await telegram_mod.restart(cfg)
            continue
        if k == "tg_allowed_chats":
            chats: list[int] = []
            for item in (v if isinstance(v, list) else str(v).split(",")):
                try:
                    chats.append(int(str(item).strip()))
                except ValueError:
                    continue
            db.set_setting("tg_allowed_chats", sorted(set(chats)))
            continue
        if k == "tg_enabled":
            db.set_setting("tg_enabled", bool(v))
            db.log("telegram", f"telegram {'enabled' if v else 'disabled'}"
                               f" — restarting poller")
            await telegram_mod.restart(cfg)
            continue
        if k.startswith("voice_"):  # FineTuneCard knobs + voice lock, db-only
            if k == "voice_temperature" and v not in ("safe", "bold", "experimental"):
                continue
            if k == "voice_lock_enabled":
                db.set_setting(k, bool(v))
                continue
            if k == "voice_lock_threshold":
                db.set_setting(k, max(0, min(100, int(v))))
                continue
            if k != "voice_temperature":
                v = max(0, min(100 if k != "voice_emoji_density" else 10, int(v)))
            db.set_setting(k, v)
            continue
        setattr(cfg.agent, k, v)
        db.set_setting(f"agent_{k}", v)
    return await get_settings()


# ---------------- voice lock (v0.4.0) ----------------

class VoiceLockCheckBody(BaseModel):
    text: str
    kind: str = "post"


@app.post("/api/voice-lock/check")
async def voice_lock_check_ep(body: VoiceLockCheckBody):
    """Settings "test a line": deterministic verdict, no LLM fix spend."""
    from ..gen import voice_lock

    def _check() -> dict:
        r = voice_lock.check_draft(cfg, body.text, body.kind, allow_fix=False)
        return {"score": r.score_0_100, "violations": r.violations,
                "passed": r.passed, "threshold": r.threshold,
                "rules_source": voice_lock.load_persona_rules()["source"]}

    return await asyncio.to_thread(_check)


# ---------------- loop status (TaskRows on the Write page) ----------------

LOOP_NAMES = ("import", "study", "create", "engage", "mentions", "publish", "learn", "scan")


def _loops_status_data() -> dict:
    """Last run per loop from the agent log + next run from the scheduler."""
    loops = []
    with db.connect() as c:
        for name in LOOP_NAMES:
            row = c.execute(
                "SELECT ts, level, message FROM agent_log "
                "WHERE loop = ? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            last = dict(row) if row else None
            job = scheduler.get_job(name) if scheduler else None
            loops.append({
                "name": name,
                "last_run": last["ts"] if last else None,
                "last_status": ("error" if last["level"] == "error" else "ok") if last else None,
                "last_message": last["message"][:120] if last else None,
                "next_run": (job.next_run_time.isoformat(timespec="seconds")
                             if job and job.next_run_time else None),
            })
    return {"loops": loops, "scheduler_running": scheduler is not None}


@app.get("/api/loops/status")
async def loops_status():
    return _loops_status_data()


# ---------------- OpenStanley chat ----------------

class ChatMessage(BaseModel):
    message: str


class ChatDraftBody(BaseModel):
    text: str
    image: str | None = None


@app.get("/api/chat/history")
async def chat_history_ep():
    return db.chat_history(limit=60)


@app.post("/api/chat")
async def chat_ep(body: ChatMessage):
    from ..gen import chat as chat_mod
    result = await asyncio.to_thread(chat_mod.chat_reply, cfg, body.message)
    return result


@app.post("/api/chat/stream")
async def chat_stream_ep(body: ChatMessage):
    """SSE: tokens as they arrive from z.ai, then tool actions, then done."""
    from ..gen import chat as chat_mod

    async def event_stream():
        gen = chat_mod.chat_reply_stream(cfg, body.message)
        # run the blocking LLM stream in a worker thread, one event per SSE frame
        async for evt in iterate_in_threadpool(gen):
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/chat/draft")
async def chat_draft_ep(body: ChatDraftBody):
    from ..gen import chat as chat_mod
    if body.image and ("/" in body.image or "\\" in body.image or ".." in body.image):
        raise HTTPException(400, "bad image name")
    if body.image and not (MEDIA_DIR / body.image).exists():
        raise HTTPException(404, "no such media file")
    did = await asyncio.to_thread(chat_mod.draft_from_chat, cfg,
                                  body.text, body.image)
    if did < 0:  # watchdog burst guard refused the save
        return JSONResponse({"ok": False, "error": "chat draft saving is "
                            "temporarily blocked by the watchdog burst guard"},
                            status_code=429)
    return {"ok": True, "draft_id": did}


# ---------------- brain (self-improving memory) ----------------

from ..gen import brain as brain_mod  # noqa: E402 — single import for the section
from ..harness import runner as harness_runner  # noqa: E402
from ..harness.runner import RunBus  # noqa: E402


@app.get("/api/brain")
async def brain_inventory():
    return {"parts": brain_mod.inventory()}


@app.get("/api/brain/journal")
async def brain_journal():
    content = brain_mod.read("journal")
    return {"name": "journal", "type": "md", "content": content,
            "entries": brain_mod.parse_journal(content)}


@app.get("/api/brain/photos/{name}")
async def brain_photo_serve(name: str):
    try:
        return FileResponse(brain_mod.photo_path(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/brain/{part}")
async def brain_read(part: str):
    try:
        if part == "photos":
            return {"name": "photos", "type": "photos",
                    "photos": brain_mod.list_photos()}
        content = brain_mod.read(part)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    out = {"name": part, "type": "md", "content": content}
    if part == "rules":
        out["rules"] = brain_mod.parse_rules(content)
    if part == "journal":
        out["entries"] = brain_mod.parse_journal(content)
    return out


class BrainEdit(BaseModel):
    content: str


@app.put("/api/brain/{part}")
async def brain_write(part: str, body: BrainEdit):
    try:
        brain_mod.write(part, body.content)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except IsADirectoryError as e:
        raise HTTPException(400, "photos part is not directly editable") from e
    except brain_mod.BrainSecurityError as e:
        raise HTTPException(400, str(e)) from e
    brain_mod.journal_append(f"user-edit:{part}", "user edited this file by hand")
    return {"ok": True, "name": part}


class BrainReflect(BaseModel):
    trigger: str = "chat"


@app.post("/api/brain/reflect")
async def brain_reflect_ep(body: BrainReflect):
    if body.trigger not in ("chat", "learn", "scan", "metrics"):
        raise HTTPException(400, "trigger must be chat|learn|scan|metrics")
    try:
        result = await asyncio.to_thread(brain_mod.reflect, cfg, body.trigger)
    except Exception as e:  # noqa: BLE001
        db.log("brain", f"manual reflect({body.trigger}) failed: {e}", level="error")
        raise HTTPException(500, str(e)) from e
    return result


@app.post("/api/brain/photos")
async def brain_photo_upload(file: UploadFile = File(...),
                             caption: str = Form(""), usage: str = Form("")):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "photo too large (max 10MB)")
    try:
        rec = brain_mod.save_photo(data, file.filename or "photo.png",
                                   caption=caption, usage=usage)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except brain_mod.BrainSecurityError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, **rec}


# ---------------- harness (eval + quality measurement) ----------------

class HarnessRunBody(BaseModel):
    suites: list[str] | None = None       # None → configured suites
    real_llm: bool = False
    ab: bool = False                      # A/B brain-lift: with vs without brain


class HarnessCompareBody(BaseModel):
    a: int
    b: int


@app.post("/api/harness/run")
async def harness_run_ep(body: HarnessRunBody):
    try:
        suites = harness_runner.resolve_suites(body.suites or ["all"], cfg)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # pre-create the row so the client gets {run_id} before the first suite;
    # the bus registers here too so SSE can subscribe without a race
    run_id = db.add_eval_run(
        label="ab" if body.ab else "manual", real_llm=body.real_llm,
        use_brain=not body.ab,
        config={"suites": suites, "sample_count": cfg.harness.sample_count,
                "ab": body.ab})
    bus = RunBus(kind="ab" if body.ab else "run")
    harness_runner.BUSES[run_id] = bus

    def _worker():
        try:
            if body.ab:
                harness_runner.run_ab(cfg, suites, real_llm=body.real_llm,
                                      bus=bus, base_run_id=run_id)
            else:
                harness_runner.run_all(cfg, suites, real_llm=body.real_llm,
                                        bus=bus, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            db.update_eval_run(run_id, status="error", error=str(e)[:400])
            bus.emit({"type": "error", "run_id": run_id,
                      "message": str(e)[:300]})

    threading.Thread(target=_worker, daemon=True,
                     name=f"harness-{run_id}").start()
    return {"ok": True, "run_id": run_id}


@app.get("/api/harness/run/{run_id}/events")
async def harness_run_events(run_id: int):
    """SSE: replay of run events + live tail until the run closes."""
    bus = harness_runner.BUSES.get(run_id)
    if bus is None:
        stored = db.get_eval_run(run_id)
        if stored is None:
            raise HTTPException(404, f"no such run {run_id}")
        if stored["status"] == "running":
            bus = harness_runner._bus(run_id)
        else:  # finished before anyone subscribed — replay from the DB
            def _finished():
                yield {"type": "start", "run_id": run_id, "suites": [
                    r["suite"] for r in stored["results"]], "label": stored["label"],
                    "real_llm": stored["real_llm"], "use_brain": stored["use_brain"]}
                for r in stored["results"]:
                    yield {"type": "suite_done", "run_id": run_id,
                           "suite": r["suite"], "score": r["score"],
                           "delta": (stored.get("deltas") or {}).get(r["suite"])}
                yield {"type": "done", "run_id": run_id,
                       "total": stored["total"],
                       "deltas": stored.get("deltas") or {}}
            return _sse_response(_finished())

    def _tail():
        q = bus.subscribe()
        while True:
            evt = q.get()
            if evt is None:
                return
            yield evt

    return _sse_response(_tail())


def _sse_response(gen):
    import queue as _queue

    def _frames():
        try:
            for evt in gen:
                yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
        except (_queue.Empty, GeneratorExit):
            return

    return StreamingResponse(iterate_in_threadpool(_frames()),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/harness/runs")
async def harness_runs(limit: int = 50):
    return {"runs": db.list_eval_runs(limit)}


@app.get("/api/harness/runs/{run_id}")
async def harness_run_detail(run_id: int):
    run = db.get_eval_run(run_id)
    if run is None:
        raise HTTPException(404, f"no such run {run_id}")
    return run


@app.post("/api/harness/compare")
async def harness_compare_ep(body: HarnessCompareBody):
    a, b = db.get_eval_run(body.a), db.get_eval_run(body.b)
    if not a or not b:
        raise HTTPException(404, "run not found")
    sa = {r["suite"]: r["score"] for r in a["results"]}
    sb = {r["suite"]: r["score"] for r in b["results"]}
    suites = {}
    for suite in sorted(set(sa) | set(sb)):
        x, y = sa.get(suite), sb.get(suite)
        suites[suite] = {"a": x, "b": y,
                         "delta": None if x is None or y is None
                         else round(y - x, 1)}
    return {
        "a": {"run_id": a["id"], "ts": a["ts"], "label": a["label"],
              "total": a["total"]},
        "b": {"run_id": b["id"], "ts": b["ts"], "label": b["label"],
              "total": b["total"]},
        "suites": suites,
        "total_delta": None if a["total"] is None or b["total"] is None
        else round(b["total"] - a["total"], 1),
    }


# ---------------- harness (eval + quality measurement) ----------------

from ..harness import runner as harness_runner  # noqa: E402 — section import


class HarnessRunBody(BaseModel):
    suites: list[str] | None = None
    real_llm: bool | None = None
    ab: bool = False


class HarnessCompareBody(BaseModel):
    a: int
    b: int


_harness_lock = threading.Lock()
_harness_active = {"run_id": None}


def _harness_worker(run_id: int, body: HarnessRunBody) -> None:
    try:
        if body.ab:
            harness_runner.run_ab(cfg, body.suites, real_llm=bool(body.real_llm),
                                  base_run_id=run_id)
        else:
            harness_runner.run_all(cfg, body.suites,
                                   use_brain=True,
                                   real_llm=bool(body.real_llm),
                                   label="manual", run_id=run_id)
    except Exception as e:  # noqa: BLE001 — worker: surface on the bus + db
        db.log("harness", f"run #{run_id} crashed: {e}", level="error")
        db.update_eval_run(run_id, status="error", error=str(e)[:500])
        harness_runner._bus(run_id).emit({"type": "error", "run_id": run_id,
                                          "message": str(e)[:500]})
    finally:
        with _harness_lock:
            _harness_active["run_id"] = None


@app.post("/api/harness/run")
async def harness_run_ep(body: HarnessRunBody):
    """Start an eval run (or A/B pair) in a worker thread → {run_id}.
    Progress streams from /api/harness/run/{run_id}/events."""
    try:
        harness_runner.resolve_suites(body.suites, cfg)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    with _harness_lock:
        if _harness_active["run_id"] is not None:
            raise HTTPException(409, f"a harness run is already active "
                                     f"(#{_harness_active['run_id']})")
        real = body.real_llm if body.real_llm is not None else False
        run_id = db.add_eval_run(
            label="ab:pair" if body.ab else "manual",
            real_llm=real, use_brain=True,
            config={"suites": body.suites or cfg.harness.suites,
                    "ab": bool(body.ab)})
        _harness_active["run_id"] = run_id
    threading.Thread(target=_harness_worker, args=(run_id, body),
                     daemon=True, name=f"harness-run-{run_id}").start()
    return {"ok": True, "run_id": run_id, "ab": bool(body.ab)}


def _replay_bus(run_id: int):
    """Synthesize a bus from stored results when the process restarted."""
    run = db.get_eval_run(run_id)
    if run is None:
        return None
    is_ab = str(run.get("label", "")).startswith("ab")
    bus = harness_runner.RunBus(kind="ab" if is_ab else "run")
    if is_ab:
        deltas = run.get("deltas") or {}
        arms = deltas.get("ab_arms") or []
        bus.emit({"type": "ab_start", "run_id": run_id,
                  "suites": (run.get("config") or {}).get("suites", [])})
        for arm_id in arms:
            arm = db.get_eval_run(arm_id)
            if not arm:
                continue
            for r in arm.get("results", []):
                bus.emit({"type": "suite_done", "run_id": arm_id,
                          "suite": r["suite"], "score": r["score"],
                          "delta": None})
        bus.emit({"type": "ab_done", "run_id": run_id,
                  "no_brain_run_id": arms[0] if arms else None,
                  "with_brain_run_id": arms[-1] if arms else None,
                  "lift": deltas.get("lift") or {}})
        return bus
    bus.emit({"type": "start", "run_id": run_id, "suites": cfg.harness.suites,
              "label": run["label"], "real_llm": run["real_llm"],
              "use_brain": run["use_brain"]})
    deltas = run.get("deltas") or {}
    for r in run.get("results", []):
        bus.emit({"type": "suite_done", "run_id": run_id, "suite": r["suite"],
                  "score": r["score"], "delta": deltas.get(r["suite"])})
    bus.emit({"type": "done", "run_id": run_id, "total": run.get("total"),
              "deltas": deltas, "report_path": None, "regression_notes": []})
    return bus


@app.get("/api/harness/run/{run_id}/events")
async def harness_events_ep(run_id: int):
    """SSE: start → per-suite progress → done (A/B pairs: → ab_done)."""
    bus = harness_runner.BUSES.get(run_id) or _replay_bus(run_id)
    if bus is None:
        raise HTTPException(404, f"no harness run {run_id}")

    def gen():
        is_ab = False
        try:
            q = bus.subscribe()
            while True:
                try:
                    evt = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if evt is None:
                    break
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt.get("type") == "ab_start":
                    is_ab = True  # arm 'done' events are not the stream's end
                if evt.get("type") == "error":
                    break
                if evt.get("type") == "done" and not is_ab:
                    break
                if evt.get("type") == "ab_done" and is_ab:
                    break
        except BaseException as e:  # noqa: BLE001 — surface swallowed SSE deaths
            db.log("harness", f"SSE generator died: {type(e).__name__}: {e}",
                   level="error")

    return StreamingResponse(iterate_in_threadpool(gen()),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/harness/runs")
async def harness_runs_ep(limit: int = 50):
    return {"runs": db.list_eval_runs(limit)}


@app.get("/api/harness/runs/{run_id}")
async def harness_run_detail_ep(run_id: int):
    run = db.get_eval_run(run_id)
    if run is None:
        raise HTTPException(404, f"no harness run {run_id}")
    return run


@app.post("/api/harness/compare")
async def harness_compare_ep(body: HarnessCompareBody):
    """Per-suite diff of two runs (a = older baseline, b = newer)."""
    a, b = db.get_eval_run(body.a), db.get_eval_run(body.b)
    if a is None or b is None:
        raise HTTPException(404, "run a or b not found")
    b_scores = {r["suite"]: r["score"] for r in b.get("results", [])}
    a_scores = {r["suite"]: r["score"] for r in a.get("results", [])}
    suites = sorted(set(a_scores) | set(b_scores))
    per_suite = {s: {"a": a_scores.get(s), "b": b_scores.get(s),
                     "delta": (round(b_scores[s] - a_scores[s], 1)
                               if s in a_scores and s in b_scores else None)}
                 for s in suites}
    total_delta = (round(b["total"] - a["total"], 1)
                   if a.get("total") is not None and b.get("total") is not None
                   else None)
    return {"a": {"id": a["id"], "ts": a["ts"], "label": a["label"],
                  "total": a.get("total")},
            "b": {"id": b["id"], "ts": b["ts"], "label": b["label"],
                  "total": b.get("total")},
            "suites": per_suite, "total_delta": total_delta}


@app.get("/api/strategy")
async def get_strategy():
    return db.get_acct_setting("strategy") or {"text": None, "exists": False}


@app.post("/api/strategy")
async def gen_strategy(force: bool = False):
    from ..gen import strategy as strategy_mod
    try:
        result = await asyncio.to_thread(strategy_mod.build_strategy, cfg, force)
        return {"ok": True, **result}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


@app.get("/api/calendar")
async def calendar_ep():
    """Scheduled/pending/published items by date, plus empty cadence slots."""
    import collections
    by_date = collections.defaultdict(list)
    for d in db.drafts_by_status("approved", 200):
        if d.get("scheduled_at"):
            by_date[d["scheduled_at"][:10]].append(_cal_item(d, "scheduled"))
    for d in db.drafts_by_status("draft", 200):  # scheduled replies await approval
        if d.get("scheduled_at"):
            by_date[d["scheduled_at"][:10]].append(_cal_item(d, "pending"))
    for d in db.drafts_by_status("published", 200):
        if d.get("published_at"):
            by_date[d["published_at"][:10]].append(_cal_item(d, "published"))
    return {"days": dict(by_date), "empty_slots": _empty_slots(by_date),
            "smart": _smart_slots_calendar()}


def _smart_slots_calendar(horizon_days: int = 14) -> dict:
    """Per-day scored slot chips for the Calendar page — same math the
    approve path uses, so what you see is what the scheduler would pick."""
    smart = {"enabled": bool(cfg.agent.smart_slots),
             "source": slots_mod.source(cfg), "slots": {}}
    if not cfg.agent.smart_slots:
        return smart
    today = datetime.now().date()
    for offset in range(horizon_days):
        day = today + timedelta(days=offset)
        chips = slots_mod.day_slots(cfg, day)
        if chips:
            smart["slots"][day.isoformat()] = chips
    return smart


def _cal_item(d: dict, state: str) -> dict:
    meta = d.get("meta") or {}
    alg = meta.get("alg") or {}
    return {"id": d["id"], "kind": d.get("kind") or "post", "state": state,
            "text": d["text"], "scheduled_at": d.get("scheduled_at"),
            "published_at": d.get("published_at"),
            "time": (d.get("scheduled_at") or d.get("published_at") or "T")[11:16],
            "image": d.get("image"), "score": alg.get("score"),
            "language": meta.get("language") or "en",
            "scheduled_reason": meta.get("scheduled_reason")}


def _empty_slots(by_date) -> dict:
    """Cadence gaps for the next 14 days: post_times not yet filled."""
    slots: dict[str, list[str]] = {}
    today = datetime.now().date()
    for offset in range(14):
        day = (today + timedelta(days=offset)).isoformat()
        taken = {item["time"] for item in by_date.get(day, [])
                 if item["kind"] == "post"}
        missing = [t for t in cfg.agent.post_times if t[:5] not in taken]
        if missing and offset > 0:  # today partially fillable — skip noise
            slots[day] = missing
    return slots


# ---------------- insights (Recharts data) ----------------

@app.get("/api/insights")
async def insights_ep():
    from ..gen.lang import detect as lang_detect
    posts = db.own_posts(limit=300)
    if not posts:
        return {"engagement_over_time": [], "best_hours": [], "hours_heatmap": [],
                "format_performance": [], "language_mix": [], "summary": {}}

    by_date: dict[str, dict] = {}
    hour_eng: dict[int, float] = {}
    heatmap: dict[tuple[int, int], list[float]] = {}
    lang_counts: dict[str, int] = {}
    for p in posts:
        ca = p.get("created_at") or ""
        eng = (p.get("likes") or 0) + 3 * (p.get("reposts") or 0) + 8 * (p.get("replies") or 0)
        if ca and "T" in ca:
            date, hh = ca[:10], ca[11:13]
            try:
                h = int(hh)
            except ValueError:
                continue
            slot = by_date.setdefault(date, {"date": date, "impressions": 0,
                                             "engagement": 0, "posts": 0})
            slot["impressions"] += p.get("impressions") or 0
            slot["engagement"] += eng
            slot["posts"] += 1
            hour_eng[h] = hour_eng.get(h, 0.0) + (p.get("engagement") or 0)
            wd = (datetime.fromisoformat(ca).weekday())  # 0=Mon
            heatmap.setdefault((wd, h), []).append(p.get("engagement") or 0)
        if p.get("text"):
            lang_counts[lang_detect(p["text"])] = lang_counts.get(lang_detect(p["text"]), 0) + 1

    timeline = sorted(by_date.values(), key=lambda s: s["date"])[-45:]
    best_hours = [{"hour": h, "avg_engagement": round(hour_eng.get(h, 0) / 30, 3)}
                  for h in range(24)]
    grid = [{"day": wd, "hour": h, "value": round(sum(v) / len(v), 3)}
            for (wd, h), v in sorted(heatmap.items())]

    # format performance: published drafts joined to real post engagement
    fmt_stats: dict[str, list[float]] = {}
    posts_by_xid = {p.get("x_id"): p for p in posts}
    for d in db.drafts_by_status("published", 200):
        meta = d.get("meta") or {}
        fmt = meta.get("format") or "one-liner"
        p = posts_by_xid.get(d.get("x_id"))
        if not p:
            continue
        eng = (p.get("likes") or 0) + 3 * (p.get("reposts") or 0) + 8 * (p.get("replies") or 0)
        fmt_stats.setdefault(fmt, []).append(float(eng))
    fmt_perf = [{"format": f, "count": len(v),
                 "avg_engagement": round(sum(v) / len(v), 1)}
                for f, v in sorted(fmt_stats.items())]

    total_imp = sum(p.get("impressions") or 0 for p in posts)
    total_eng = sum((p.get("likes") or 0) + 3 * (p.get("reposts") or 0)
                    + 8 * (p.get("replies") or 0) for p in posts)
    best = max(posts, key=lambda p: p.get("engagement") or 0)
    return {
        "engagement_over_time": timeline,
        "best_hours": best_hours,
        "hours_heatmap": grid,
        "format_performance": fmt_perf,
        "language_mix": [{"language": k, "count": v}
                         for k, v in sorted(lang_counts.items(), key=lambda x: -x[1])],
        "summary": {
            "total_impressions": total_imp,
            "total_engagement": total_eng,
            "avg_engagement_rate": round(total_eng / max(total_imp, 1), 5),
            "best_post": {"text": (best.get("text") or "")[:140],
                          "likes": best.get("likes"), "replies": best.get("replies")},
        },
    }


# ---------------- style profile ----------------

@app.get("/api/style-profile")
async def style_profile_ep():
    from ..gen import style_scan
    p = style_scan.load_profile()
    if not p:
        return {"exists": False, "stats": None, "human_summary": None,
                "updated_at": None}
    return {"exists": True, "stats": p.get("stats"),
            "human_summary": p.get("human_summary"),
            "updated_at": p.get("updated_at")}


# ---------------- accounts (v0.5.0 multi-account) ----------------

def _x_caps() -> dict:
    return {
        "max_posts_per_day": cfg.x.max_posts_per_day,
        "max_replies_per_day": cfg.x.max_replies_per_day,
        "min_delay_s": cfg.x.min_delay_s,
        "max_delay_s": cfg.x.max_delay_s,
    }


async def _verify_cookies_no_heal(canonical: str, account_id: int = 1,
                                  surface: str = "cookie connect") -> dict:
    """Validate cookies against real X with auto-heal DISABLED, then return
    the verified identity. Raises 400 on any failure.

    Every endpoint that persists cookies calls this FIRST and stores the same
    ``canonical`` string it validated. me()'s default heal-on-auth-failure must
    never run here: a healed browser session would make a dead token look
    valid and the caller would persist input cookies that never worked
    (FIX_BRIEF_BOOTSTRAP_VALIDATION)."""
    from ..x.client import XCookie
    probe = XCookie(canonical, caps=_x_caps(), account_id=account_id)
    try:
        return await probe.me(heal=False)  # identity check — whose cookies?
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        db.log("system", f"{surface} failed: {str(e)[:200]}", level="error")
        msg = str(e)
        if "353" in msg or "csrf" in msg.lower():
            # X answered the CSRF challenge, i.e. auth_token reached a live
            # endpoint — the paste is missing ct0 (or holds a pre-relogin
            # stale one). Same cure either way: copy BOTH cookies fresh from
            # the same browser session and paste them together.
            raise HTTPException(
                400, f"Cookies rejected by X ({msg[:200]}): the auth_token "
                     "looks fine, but ct0 is missing or stale — copy BOTH "
                     "values from the same browser (F12 → Cookies → x.com) "
                     "and paste them together, e.g. auth_token=…; ct0=…") from e
        raise HTTPException(
            400, f"Cookies rejected by X: {msg[:300]} — the token is "
                 "invalid or expired; re-copy it from your browser") from e

@app.get("/api/accounts")
async def accounts_ep():
    """Registry view — handles, follower snapshot, post count. No secrets:
    cookies are write-only (masked hint, values never returned)."""
    return {"active_account_id": db.active_account(), "accounts": db.list_accounts()}


@app.post("/api/accounts")
async def create_account_ep(body: AccountCreate):
    handle = (body.handle or "").strip().lstrip("@")
    if not handle:
        raise HTTPException(400, "handle required")
    cookies = ""
    if body.cookies_json:
        from ..x.client import normalize_cookies_input
        cookies = normalize_cookies_input(body.cookies_json) or ""
        if not cookies:
            raise HTTPException(400, COOKIES_PASTE_HINT)
        # validate no-heal BEFORE the account row exists — a bad paste must
        # not leave an orphan account or unvalidated cookies in the DB
        await _verify_cookies_no_heal(cookies, surface="account create")
    acct_id = db.create_account(handle, cookies)
    from ..gen import brain as brain_mod
    brain_mod.ensure(acct_id)  # fresh EMPTY brain — seeds only, no other account's memory
    db.log("accounts", f"created account #{acct_id} (@{handle}) — brain seeded fresh")
    return {"ok": True, "account_id": acct_id, "handle": handle}


@app.post("/api/accounts/{account_id}/activate")
async def activate_account_ep(account_id: int):
    if not db.set_active_account(account_id):
        raise HTTPException(404, f"no active account {account_id}")
    _rebuild_agent()  # cookie client picks up the new account's cookies
    db.log("accounts", f"switched active account → #{account_id} "
                       f"(@{db.get_account(account_id)['handle'] or 'no handle yet'})")
    return {"ok": True, "active_account_id": db.active_account()}


@app.post("/api/accounts/{account_id}/cookies")
async def set_account_cookies_ep(account_id: int, body: AccountCookies):
    """Write-only cookie storage for one account (masked in GETs, never logged)."""
    from ..x.client import normalize_cookies_input
    canonical = normalize_cookies_input(body.cookies_json)
    if not canonical:
        raise HTTPException(400, COOKIES_PASTE_HINT)
    if not db.get_account(account_id):
        raise HTTPException(404, f"no account {account_id}")
    # no-heal validation BEFORE storing — only cookies X actually accepted
    # are ever persisted; on failure the stored ones stay untouched
    await _verify_cookies_no_heal(canonical, account_id=account_id,
                                  surface="account cookies update")
    if not db.set_account_cookies(account_id, canonical):
        raise HTTPException(404, f"no account {account_id}")
    db.log("accounts", f"cookies updated for account #{account_id} (values not logged)")
    return {"ok": True, "cookies_set": True,
            "cookies_masked": db.mask_cookies(canonical)}


@app.delete("/api/accounts/{account_id}")
async def delete_account_ep(account_id: int):
    """Archive one account to data/accounts/archive-<handle>-<date>/ then
    remove its rows. The last remaining account cannot be deleted."""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, f"no account {account_id}")
    remaining = [a for a in db.list_accounts() if a["id"] != account_id]
    if not remaining:
        raise HTTPException(409, "cannot delete the only account")
    from ..x.client import resolve_cookies  # cookies never enter the archive
    from ..gen import brain as brain_mod
    handle = account["handle"] or f"account-{account_id}"
    archive_dir = brain_mod.ACCOUNTS_ROOT / \
        f"archive-{handle}-{datetime.now().strftime('%Y%m%d')}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dump = db.dump_account(account_id)
    (archive_dir / "dump.json").write_text(
        json.dumps({"account": {k: v for k, v in account.items()
                                if k not in ("cookies_json", "cookies_set")},
                    **dump}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    # the account's whole world moves with it (brain, digests) when present
    from ..gen import brain as brain_mod
    src_dir = brain_mod.account_dir(account_id)
    if src_dir.exists() and src_dir != archive_dir:
        import shutil
        shutil.move(str(src_dir), str(archive_dir / "account"))
    db.delete_account_rows(account_id)
    if db.active_account() == account_id:
        db.set_active_account(remaining[0]["id"])
        _rebuild_agent()
    db.log("accounts", f"archived + removed account #{account_id} (@{handle}) "
                       f"→ {archive_dir.name}")
    return {"ok": True, "archived_to": str(archive_dir),
            "active_account_id": db.active_account()}


class AccountBootstrap(BaseModel):
    cookies_json: str


@app.post("/api/accounts/bootstrap")
async def account_bootstrap_ep(body: AccountBootstrap):
    """Paste cookies → creates or selects THAT account (v0.5.0 connect flow).

    Validates the cookies against real X via me(); the returned handle then
    either re-selects an existing account (reconnect) or creates a fresh one
    with a seeded empty brain. The account becomes active."""
    from ..x.client import normalize_cookies_input
    canonical = normalize_cookies_input(body.cookies_json)
    if not canonical:
        raise HTTPException(400, COOKIES_PASTE_HINT)
    # no-heal identity check — a fake token must FAIL here (400, DB
    # untouched), never be masked by a healed browser session and persisted
    me = await _verify_cookies_no_heal(canonical, surface="account bootstrap")
    handle = me["username"]
    single_line = canonical
    existing = next((a for a in db.list_accounts()
                     if a["handle"].lower() == handle.lower()), None)
    if existing:
        acct = existing["id"]
        action = "reconnected"
    else:
        from ..gen import brain as brain_mod
        acct = db.create_account(handle, single_line)
        brain_mod.ensure(acct)  # fresh EMPTY brain — nothing from other accounts
        action = "created"
    db.set_account_cookies(acct, single_line)
    db.set_account_handle(acct, handle)
    db.set_me(me, acct)
    db.set_active_account(acct)
    _set_config_mode("cookie")
    _rebuild_agent()
    db.log("accounts", f"{action} account #{acct} (@{handle}, "
                       f"followers={me.get('followers')}) via cookie bootstrap")
    return {"ok": True, "account_id": acct, "handle": handle, "action": action,
            "followers": me.get("followers"), "mode": "cookie",
            "active_account_id": db.active_account()}


# ---------------- X connect (cookie wizard) ----------------

@app.post("/api/x/cookie-connect")
async def cookie_connect(body: CookieConnect):
    """Validate cookies JSON against real X, and if OK switch mode to cookie.

    v0.5.0: cookies persist into the ACTIVE account's row (DB is the source
    of truth; .env stays a bootstrap fallback for account 1)."""
    from ..x.client import normalize_cookies_input
    canonical = normalize_cookies_input(body.cookies_json)
    if not canonical:
        raise HTTPException(400, COOKIES_PASTE_HINT)
    acct = db.active_account()
    # no-heal validation — only cookies X accepted get stored (same rule as
    # bootstrap; a healed success must never persist the unvalidated input)
    me = await _verify_cookies_no_heal(canonical, account_id=acct,
                                       surface="cookie connect")
    # success → persist into the account row and switch mode
    db.set_account_cookies(acct, canonical)
    db.set_account_handle(acct, me["username"])
    _set_config_mode("cookie")
    db.set_me(me)
    db.log("system", f"connected @{me['username']} via cookies (followers={me.get('followers')})")
    # rebuild agent client so all loops use cookie mode now
    _rebuild_agent()
    return {"ok": True, "account_id": acct, "username": me["username"],
            "followers": me.get("followers"), "mode": "cookie"}


@app.get("/api/x/status")
async def x_status():
    from ..core.safety import usage
    from ..x import cookie_heal
    from ..x.client import resolve_cookies
    caps = {
        "max_posts_per_day": cfg.x.max_posts_per_day,
        "max_replies_per_day": cfg.x.max_replies_per_day,
        "min_delay_s": cfg.x.min_delay_s,
        "max_delay_s": cfg.x.max_delay_s,
    }
    acct = db.active_account()
    me = db.get_me(acct)
    account = db.get_account(acct) or {}
    heal = cookie_heal.status()
    return {
        "account_id": acct,
        "mode": cfg.x.mode,
        "username": me.get("username") or account.get("handle") or cfg.x.username,
        "followers": me.get("followers"),
        "cookies_set": bool(resolve_cookies(cfg, acct)),
        "cookies_masked": db.mask_cookies(db.account_cookies(acct)),
        "cookies_stale": heal["stale"],
        "last_heal": heal["last_heal"],
        "heal_ok": heal["heal_ok"],
        "safety": {"caps": caps, "usage": usage(acct)},
    }


@app.post("/api/x/safety")
async def update_safety(body: CapSettings):
    for k, v in body.dict().items():
        if v is None:
            continue
        setattr(cfg.x, k, max(1, int(v)))
        _set_config_value("x", k, max(1, int(v)))
    if hasattr(agent.x, "_caps"):
        agent.x._caps.update({
            "max_posts_per_day": cfg.x.max_posts_per_day,
            "max_replies_per_day": cfg.x.max_replies_per_day,
            "min_delay_s": cfg.x.min_delay_s,
            "max_delay_s": cfg.x.max_delay_s,
        })
    return await x_status()


def _rebuild_agent():
    global agent
    agent = Agent(load_config())




def _write_env(path: Path, key: str, value: str) -> None:
    """Set key=value in .env, creating or replacing as needed. No markup."""
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    out, seen = [], False
    for ln in lines:
        if ln.startswith(key + "="):
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(ln)
    if not seen:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _set_config_mode(mode: str) -> None:
    _set_config_value("x", "mode", mode)


def _set_config_value(section: str, key: str, value) -> None:
    import re as _re
    p = ROOT / "data" / "config.toml"
    text = p.read_text(encoding="utf-8")
    if isinstance(value, str):
        val = f'"{value}"'
    elif isinstance(value, bool):
        val = "true" if value else "false"
    else:
        val = str(value)
    pattern = _re.compile(rf"^(\s*{key}\s*=\s*).*$", _re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(rf"\g<1>{val}", text, count=1)
    else:
        # append under section
        sec = _re.compile(rf"^(\[{section}\])", _re.MULTILINE)
        if sec.search(text):
            text = sec.sub(rf"\g<1>\n{key} = {val}", text, count=1)
    p.write_text(text, encoding="utf-8")


# ---------------- system smoke (v0.3.7 live self-check) ----------------

SMOKE_RATE_LIMIT_S = 300  # one fresh run per 5 minutes


async def _run_smoke_and_store() -> dict:
    """Run the live self-check once, persist the report, log a system line."""
    from ..system import smoke as smoke_mod
    report = await smoke_mod.run_smoke(cfg, x_client=agent.x)
    data = report.to_dict()
    db.set_setting("smoke_last", data)
    db.set_setting("smoke_last_run_epoch", time.time())
    ident = next((p for p in data["probes"] if p["name"] == "identity"), None)
    if ident and not ident["ok"]:
        db.log("system", "live X wiring broken — check Connect tab "
                         f"(identity: {ident['detail'][:140]})", level="warn")
    else:
        ok_n = sum(1 for p in data["probes"] if p["ok"])
        db.log("system", f"smoke self-check: {data['status']} "
                         f"({ok_n}/{len(data['probes'])} probes ok, "
                         f"{data['ms']}ms, {data['x_reads']} X-reads)")
    return data


@app.get("/api/system/smoke")
async def smoke_get():
    last = db.get_setting("smoke_last")
    if not last:
        return {"ok": None, "status": "never", "ms": None, "x_reads": None,
                "ran_at": None, "probes": []}
    return last


@app.post("/api/system/smoke")
async def smoke_post():
    """Run a fresh self-check now (rate-limited: 1 run / 5 min)."""
    waited = time.time() - float(db.get_setting("smoke_last_run_epoch") or 0)
    if waited < SMOKE_RATE_LIMIT_S:
        raise HTTPException(429, "self-check rate-limited — 1 run / 5 min "
                                 f"(retry in {int(SMOKE_RATE_LIMIT_S - waited)}s)")
    return await _run_smoke_and_store()


async def _smoke_boot_task():
    """Startup smoke — background task; failures never block or kill boot."""
    try:
        await asyncio.sleep(1)  # let the server finish booting first
        await _run_smoke_and_store()
    except Exception as e:  # noqa: BLE001
        db.log("system", f"startup smoke failed: {e}", level="error")


_bg_tasks: set[asyncio.Task] = set()  # strong refs — bare tasks can be GC'd mid-flight


# ---------------- scheduler ----------------

def start_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    sched = AsyncIOScheduler(timezone=cfg.agent.timezone)
    # NOTE: pass the coroutine FUNCTION — AsyncIOScheduler runs it on the event loop.
    # (Wrapping in ensure_future runs in APScheduler's worker thread → no event loop.)
    sched.add_job(agent.study,
                  CronTrigger(hour=cfg.agent.study_hour, minute=0), id="study")

    async def _metrics_job():
        """04:17 nightly — small refresh keeps Insights numbers honest
        without waiting for the weekly learn loop's deeper pull."""
        from ..gen import metrics as metrics_mod
        try:
            res = await metrics_mod.refresh_metrics(agent.x, cfg, limit=20)
            db.log("metrics", f"nightly refresh: {res.get('refreshed')} posts captured")
        except Exception as e:  # noqa: BLE001
            db.log("metrics", f"nightly refresh failed: {e}", level="error")
        # DB diet: vacuum the shared test DB nightly (50MB+ was slowing
        # every suite run)
        try:
            from ..system.resilience import vacuum_db
            kb = vacuum_db()
            db.log("system", f"nightly vacuum: DB now {kb} KB")
        except Exception as e:  # noqa: BLE001
            db.log("system", f"vacuum skipped: {e}", level="warn")
        # rejection learning FIRST: reflect on the owner's real reject taps
        # while they're still distinguishable from what the sweeper kills
        try:
            from ..gen import rejection_learn
            res = rejection_learn.run_reflection(cfg)
            if res.get("learned_from"):
                db.log("brain", f"nightly rejection reflection: learned from "
                                f"{res['learned_from']} rejected drafts")
        except Exception as e:  # noqa: BLE001
            db.log("brain", f"nightly rejection reflection failed: {e}", level="warn")
        # expire unapproved drafts older than 3 days — a stale queue is noise,
        # not opportunity; production follows the human's approval pace.
        # Stamped reason=expired: queue hygiene, never learned as taste
        try:
            from datetime import timedelta
            from ..gen import rejection_learn
            cutoff = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
            with db.connect() as c:
                rows = c.execute(
                    "SELECT id FROM drafts WHERE status='draft' "
                    "AND created_at < ?", (cutoff,)).fetchall()
            for r in rows:
                db.update_draft(int(r["id"]), status="rejected")
                rejection_learn.record_rejection(int(r["id"]), reason="expired",
                                                 via="sweep")
            if rows:
                db.log("create", f"expired {len(rows)} unapproved drafts "
                                 f"older than 3 days")
        except Exception as e:  # noqa: BLE001
            db.log("metrics", f"draft expiry sweep failed: {e}", level="warn")

    sched.add_job(_metrics_job, CronTrigger(hour=4, minute=17),
                  id="metrics_refresh")
    sched.add_job(agent.create,
                  CronTrigger(hour=7, minute=0), id="create")
    sched.add_job(agent.engage,
                  CronTrigger(minute=30), id="engage")
    # mention inbox: conversation replies are worth most within the window —
    # every 30 min while autopilot is off (its rotation covers mentions when on)
    if cfg.agent.mentions_cron and not ap_mod.get_state()["enabled"]:
        sched.add_job(agent.mentions, CronTrigger(minute="*/30"), id="mentions")
    sched.add_job(agent.publish,
                  CronTrigger(minute="*/10"), id="publish")
    sched.add_job(agent.learn,
                  CronTrigger(day_of_week=cfg.agent.digest_weekday, hour=5, minute=0), id="learn")
    # daily digest — the agent reports to its owner (webhook when configured)
    sched.add_job(_digest_job,
                  CronTrigger(hour=digest_mod.digest_hour(cfg), minute=0),
                  id="digest")
    # hourly re-smoke at :11 — a transient boot blip must not pin a false
    # red until the next restart (rate limit is 5m, hourly fits inside it)
    sched.add_job(_run_smoke_and_store, CronTrigger(minute=11),
                  id="smoke_refresh")
    # morning briefing: the agent messages the OWNER first (09:00 daily) —
    # overnight publishes, drafts needing a decision (with one-tap cards),
    # today's schedule. You approve from bed.
    async def _briefing_job():
        from ..gen import briefing as briefing_mod
        try:
            briefing_mod.push_briefing(cfg)
        except Exception as e:  # noqa: BLE001
            db.log("briefing", f"morning briefing failed: {e}", level="warn")
    sched.add_job(_briefing_job, CronTrigger(hour=9, minute=3),
                  id="morning_briefing")
    # perf A/B loop: check shipped posts at +2h/+24h vs baseline
    async def _perf_job():
        from ..gen import perf_track
        try:
            perf_track.check_due_posts(cfg)
        except Exception as e:  # noqa: BLE001
            db.log("perf", f"perf check failed: {e}", level="warn")
    sched.add_job(_perf_job, IntervalTrigger(hours=2),
                  id="perf_track")
    # trend watches: hourly check, alert when a watched topic surges
    async def _watch_job():
        from ..gen import watch as watch_mod
        try:
            await watch_mod.check_watches(cfg)
        except Exception as e:  # noqa: BLE001
            db.log("watch", f"watch check failed: {e}", level="warn")
    sched.add_job(_watch_job, IntervalTrigger(hours=1),
                  id="trend_watches")
    # 05:00 daily session reset (like Hermes): both the web chat and
    # Telegram start FRESH each morning — yesterday's context stays in
    # the DB but leaves the live window
    async def _session_reset_job():
        from ..integrations import telegram as _tg
        try:
            _tg.reset_sessions()
        except Exception as e:  # noqa: BLE001
            db.log("system", f"session reset failed: {e}", level="warn")
    sched.add_job(_session_reset_job, CronTrigger(hour=5, minute=0),
                  id="session_reset")
    if ap_mod.get_state()["enabled"]:
        sched.add_job(_autopilot_job,
                      IntervalTrigger(minutes=ap_mod.interval_minutes(cfg),
                                      jitter=ap_mod.JITTER_MAX_S),
                      id="autopilot")
    sched.start()
    db.log("system", "scheduler started (study 03:00, create 07:00, engage :30, "
                     "mentions */30 (when autopilot off), publish */10, learn Sun 05:00, "
                     f"digest {digest_mod.digest_hour(cfg):02d}:00)")
    return sched


scheduler = None


@app.on_event("startup")
async def on_startup():
    global scheduler
    # re-apply persisted agent settings over config-file defaults
    for k in ("daily_draft_target", "post_times", "niche_accounts",
              "evergreen_themes", "auto_approve_replies", "smart_slots",
              "autopilot_interval_min", "digest_hour"):
        v = db.get_setting(f"agent_{k}")
        if v is not None:
            setattr(cfg.agent, k, v)
    import os
    if os.environ.get("OPENSTANLEY_NO_SCHEDULER") == "1":
        db.log("system", "scheduler disabled (OPENSTANLEY_NO_SCHEDULER=1)")
    else:
        scheduler = start_scheduler()
    # pre-warm the voice model in the background — the FIRST voice note
    # otherwise downloads ~75MB inside the chat handler and the bot goes
    # silent for minutes (user-facing latency, not correctness)
    import os as _os
    if _os.environ.get("OPENSTANLEY_NO_TELEGRAM") != "1":
        import threading as _th

        def _warm():
            try:
                from ..gen.voice_notes import _get_model
                _get_model()
                db.log("voice", "whisper model warm")
            except Exception as e:  # noqa: BLE001 — warm is best-effort
                db.log("voice", f"model warm skipped: {e}", level="warn")

        _th.Thread(target=_warm, daemon=True, name="whisper-warm").start()

    # v0.4.4 — Telegram poller (no-op unless enabled + token; tests set
    # OPENSTANLEY_NO_TELEGRAM=1 so no TestClient boot ever touches the network)
    await telegram_mod.start(cfg)
    if os.environ.get("OPENSTANLEY_NO_SMOKE") == "1":
        db.log("system", "startup smoke disabled (OPENSTANLEY_NO_SMOKE=1)")
    else:  # async, non-blocking — runs after scheduler init, never gates boot
        task = asyncio.get_running_loop().create_task(_smoke_boot_task())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


def main():
    import uvicorn
    uvicorn.run("openstanley.server.__main__:app", host=cfg.server_host, port=cfg.server_port, reload=False)


if __name__ == "__main__":
    main()
