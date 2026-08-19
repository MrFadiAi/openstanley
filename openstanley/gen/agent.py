"""Agent loops — the heart of OpenStanley (OpenStanley's overnight strategy etc.).

All loops share one XClient + Config. APScheduler drives them inside the server.
Each loop is also callable manually via the dashboard/API.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime

from ..core import db
from ..core.config import Config, load_config
from ..x.client import build_client
from . import brain as brain_mod
from . import ideas as ideas_mod
from . import drafts as drafts_mod
from . import mentions as mentions_mod
from . import replies as replies_mod
from . import voice as voice_mod


def _tg_draft_cards(ids: list[int]) -> None:
    """v0.4.4 — TG 'needs approval' card for drafts a loop just created.
    Fire-and-forget daemon thread: slow/broken Telegram can never slow or
    break a loop, and nothing auto-approves anywhere."""
    if not ids:
        return
    try:
        from ..integrations import telegram as tg_mod
        if tg_mod.is_enabled():
            threading.Thread(target=tg_mod.notify_new_drafts, args=(ids,),
                             daemon=True, name="tg-draft-card").start()
    except Exception as e:  # noqa: BLE001 — optional bridge, never a loop risk
        db.log("telegram", f"draft card skipped: {e}", level="warn")


async def _reflect(trigger: str, cfg: Config, acct: int | None = None) -> str:
    """Best-effort brain reflection off the event loop. Returns status."""
    try:
        res = await asyncio.to_thread(brain_mod.reflect, cfg, trigger, acct=acct)
        a = res["applied"]
        return (f"brain: +{len(a['added_rules'])} rules, "
                f"{len(a['strategy_updates'])} strategies")
    except Exception as e:  # noqa: BLE001 — reflection must never break a loop
        db.log("brain", f"reflect({trigger}) failed: {e}", level="warn")
        return f"brain reflect skipped: {e}"


class Agent:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.x = build_client(self.cfg)

    # ---------- onboarding / import ----------

    async def import_history(self) -> dict:
        acct = db.active_account()  # pinned for the whole loop — a mid-run
        me = await self.x.me()      # account switch never mixes data
        db.set_me(me, acct)
        db.log("import", f"[account {acct}] importing history for @{me['username']}")
        own = await self.x.user_tweets(me["username"], limit=self.cfg.x.import_count)
        for p in own:
            db.upsert_post(p, acct)
        niche = 0
        for theme in self.cfg.agent.evergreen_themes[:3]:
            try:
                res = await self.x.search(theme, limit=30)
            except Exception as e:  # noqa: BLE001
                db.log("import", f"niche search failed for '{theme}': {e}", level="warn")
                continue
            for p in res:
                db.upsert_post(p, acct)
                niche += 1
        db.log("import", f"[account {acct}] imported {len(own)} own posts + {niche} niche posts")
        return {"me": me, "own": len(own), "niche": niche, "account": acct}

    async def study(self) -> dict:
        """Nightly: refresh niche data + fill the story bank (ACTIVE account)."""
        acct = db.active_account()
        me = db.get_me(acct) or await self.x.me()
        niche_new = 0
        queries = self.cfg.agent.evergreen_themes + [f"from:{a}" for a in self.cfg.agent.niche_accounts]
        for q in queries[:5]:
            try:
                res = await self.x.search(q, limit=30)
            except Exception as e:  # noqa: BLE001
                db.log("study", f"search failed for '{q}': {e}", level="warn")
                continue
            for p in res:
                db.upsert_post(p, acct)
                niche_new += 1
        # bank check right after the reads — usually pure DB mining (path a)
        rep = await ideas_mod.replenish(self.cfg, min_bank=16, x=self.x, acct=acct)
        db.log("study", f"[account {acct}] study loop done: +{niche_new} niche posts, "
                        f"bank={db.idea_count(acct)}")
        return {"niche_new": niche_new, "bank": db.idea_count(acct),
                "replenished": rep["added"], "account": acct}

    async def create(self) -> dict:
        """Daily: generate drafts from the bank (replenishing it first when low
        — the create loop must never silently starve on an empty bank)."""
        acct = db.active_account()
        rep = await ideas_mod.replenish(self.cfg, x=self.x, acct=acct)  # no-op unless low
        if rep["added"]:
            db.log("create", f"[account {acct}] bank low ({rep['bank_before']}) — replenished "
                             f"+{rep['added']} from {','.join(rep['sources'])}")
        ids = await asyncio.to_thread(drafts_mod.generate_drafts, self.cfg, acct=acct)
        _tg_draft_cards(ids)
        out = {"drafts": len(ids), "account": acct}
        if rep["ran"]:
            out["bank_replenished"] = rep["added"]
        return out

    async def engage(self) -> dict:
        """Hourly: pull mentions, draft replies + SCHEDULED niche replies."""
        acct = db.active_account()
        new = await replies_mod.pull_engagements(self.cfg, self.x, acct=acct)
        ids = await asyncio.to_thread(replies_mod.draft_replies, self.cfg, acct=acct)
        niche_ids = await asyncio.to_thread(replies_mod.draft_niche_replies, self.cfg,
                                            acct=acct)
        _tg_draft_cards(ids + niche_ids)
        return {"new_mentions": new, "replies_drafted": len(ids),
                "niche_replies_scheduled": len(niche_ids), "account": acct}

    async def mentions(self) -> dict:
        """Mention inbox: fetch new mentions, draft replies for the newest
        unhandled ones (conversation beats cadence — replies within the
        window matter). Drafts only; approval gate as usual."""
        acct = db.active_account()
        fetched = await mentions_mod.fetch_mentions(self.x, acct=acct)
        rich = {m["x_id"]: m for m in fetched}  # rows w/ parent-text context
        budget = max(0, int(self.cfg.agent.mention_drafts_per_run))
        drafted = 0
        new_ids: list[int] = []
        for row in mentions_mod.pending_mentions(acct=acct):
            if drafted >= budget:
                break
            m = {**row, **{k: v for k, v in rich.get(row["x_id"], {}).items()
                           if v is not None}}
            did = await asyncio.to_thread(mentions_mod.draft_mention_reply,
                                          self.cfg, m, acct)
            if did:
                drafted += 1
                new_ids.append(did)
        _tg_draft_cards(new_ids)
        return {"mentions_new": len(fetched), "replies_drafted": drafted,
                "account": acct}

    async def scan(self) -> dict:
        """Deep style scan — up to 800 posts+replies → style_profile."""
        from . import style_scan as scan_mod
        acct = db.active_account()
        profile = await scan_mod.scan_account(self.cfg, self.x,
                                              max_posts=self.cfg.x.scan_count,
                                              acct=acct)
        try:
            voice_mod.build_voice(self.cfg, force=True, acct=acct)
            voice_status = "rebuilt"
        except Exception as e:  # noqa: BLE001
            voice_status = f"skipped: {e}"
        brain_status = await _reflect("scan", self.cfg, acct)
        return {"posts_scanned": profile["stats"]["posts_scanned"],
                "languages": profile["stats"]["language_mix"],
                "voice": voice_status, "brain": brain_status, "account": acct}

    async def publish(self) -> dict:
        """Publish due approved items (posts/replies/quotes, with media)."""
        from ..core.safety import SafetyCapExceeded
        from datetime import timedelta
        acct = db.active_account()
        published = []
        while True:
            nxt = db.next_scheduled(acct)
            if not nxt:
                break
            try:
                from ..core.config import ROOT
                media_path = str(ROOT / "data" / "media" / nxt["image"]) \
                    if nxt.get("image") else None
                reply_to = (nxt.get("meta") or {}).get("reply_to_x_id") \
                    if nxt.get("kind") == "reply" else None
                quote_of = nxt.get("quote_of") if nxt.get("kind") == "quote" else None
                if nxt["thread"]:
                    res = await self.x.post_thread(nxt["thread"])
                    x_id = res[0].get("x_id") if res else None
                else:
                    res = await self.x.post_tweet(nxt["text"], reply_to=reply_to,
                                                  media_path=media_path,
                                                  quote_of=quote_of)
                    x_id = res.get("x_id")
                db.update_draft(nxt["id"], acct=acct, status="published", x_id=x_id,
                                published_at=datetime.now().isoformat(timespec="seconds"))
                db.log("publish", f"[account {acct}] published draft {nxt['id']} → x_id={x_id}")
                published.append({"draft_id": nxt["id"], "x_id": x_id})
            except SafetyCapExceeded as e:
                # reschedule to tomorrow's best slot — never lose approved content
                if self.cfg.agent.smart_slots:
                    from . import slots as slots_mod
                    tomorrow = (datetime.now() + timedelta(days=1)).date()
                    best = slots_mod.day_slots(self.cfg, tomorrow)
                    tmr = best[0]["at"].isoformat(timespec="seconds") if best else \
                        (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
                else:
                    tmr = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + "T" + \
                          (self.cfg.agent.post_times[0] if self.cfg.agent.post_times else "09:00") + ":00"
                db.update_draft(nxt["id"], acct=acct, scheduled_at=tmr)
                db.log("publish", f"[account {acct}] daily cap reached — draft {nxt['id']} rescheduled to {tmr}", level="warn")
                break
            except Exception as e:  # noqa: BLE001
                db.log("publish", f"[account {acct}] draft {nxt['id']} failed: {e}", level="error")
                db.update_draft(nxt["id"], acct=acct, status="failed")
        return {"published": published, "account": acct}

    async def learn(self) -> dict:
        """Weekly: refresh metrics (time series + brain) + voice profile."""
        from . import metrics as metrics_mod
        acct = db.active_account()
        try:
            res = await metrics_mod.refresh_metrics(self.x, self.cfg, limit=60,
                                                    acct=acct)
            metrics_status = (f"{res['refreshed']} posts captured, "
                              f"followers={res['followers']}, "
                              f"avg_rate={res['avg_engagement_rate']}")
        except Exception as e:  # noqa: BLE001 — metrics failure must not block voice
            res = {"refreshed": 0, "followers": 0, "avg_engagement_rate": 0.0,
                   "reflected": False}
            metrics_status = f"failed: {e}"
            db.log("learn", f"metrics refresh failed: {e}", level="warn")
        try:
            voice_mod.build_voice(self.cfg, force=True, acct=acct)
            voice_status = "rebuilt"
        except Exception as e:  # noqa: BLE001
            voice_status = f"failed: {e}"
        brain_status = await _reflect("learn", self.cfg, acct)
        db.log("learn", f"[account {acct}] learn loop: {metrics_status}, voice {voice_status}")
        return {"refreshed": res["refreshed"], "metrics": metrics_status,
                "voice": voice_status, "brain": brain_status, "account": acct}
