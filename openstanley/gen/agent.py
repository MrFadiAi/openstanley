"""Agent loops — the heart of OpenStanley (OpenStanley's overnight strategy etc.).

All loops share one XClient + Config. APScheduler drives them inside the server.
Each loop is also callable manually via the dashboard/API.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime

from ..core import db
from ..core.text import err_str
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
                db.log("import", f"niche search failed for '{theme}': {err_str(e)}", level="warn")
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
                db.log("study", f"search failed for '{q}': {err_str(e)}", level="warn")
                continue
            for p in res:
                db.upsert_post(p, acct)
                niche_new += 1
        # deep pull: any account we barely know (own or niche) gets its real
        # history paged in, not the 30-post search sample (user report
        # 2026-08-21: new account "has a lot of posts", learned only 30).
        # ONE call with the full limit — the cookie client pages via cursor
        # WITHIN a call; chunked calls restart from page 1 every time (the
        # 2026-08-28 finding: days of "+400 posts" logs that stored nothing)
        DEEP_MIN = 150
        DEEP_PULL_LIMIT = 800
        try:
            me_now = db.get_me(acct) or {}
            handles = [me_now.get("username")] + list(self.cfg.agent.niche_accounts or [])
            for h in handles:
                if not h:
                    continue
                with db.connect() as c:
                    (n,) = c.execute(
                        "SELECT COUNT(*) FROM posts WHERE account_id=? "
                        "AND lower(author_handle)=lower(?)",
                        (acct, h)).fetchone()
                if n >= DEEP_MIN:
                    continue
                posts = await self.x.user_tweets(str(h), limit=DEEP_PULL_LIMIT)
                for ppost in posts:
                    db.upsert_post(ppost, acct)
                with db.connect() as c:
                    (after,) = c.execute(
                        "SELECT COUNT(*) FROM posts WHERE account_id=? "
                        "AND lower(author_handle)=lower(?)",
                        (acct, h)).fetchone()
                net = int(after) - int(n)
                if net > 0:
                    db.log("study", f"deep pull @{h}: +{net} NEW stored posts "
                                    f"(had {n}, fetched {len(posts)})")
                    niche_new += net
        except Exception as e:  # noqa: BLE001 — deep pull never blocks study
            db.log("study", f"deep pull skipped: {err_str(e)}", level="warn")
        # bank check right after the reads — usually pure DB mining (path a)
        rep = await ideas_mod.replenish(self.cfg, min_bank=16, x=self.x, acct=acct)
        # steal-this-hook: winners are in the room — distill their patterns
        hooks_added = 0
        try:
            from . import hooks as hooks_mod
            res = await asyncio.to_thread(hooks_mod.extract, self.cfg, acct)
            hooks_added = res.get("added", 0)
        except Exception as e:  # noqa: BLE001 — patterns never block study
            db.log("hooks", f"pattern extract skipped: {e}", level="warn")
        db.log("study", f"[account {acct}] study loop done: +{niche_new} niche posts, "
                        f"bank={db.idea_count(acct)}, hooks+{hooks_added}")
        return {"niche_new": niche_new, "bank": db.idea_count(acct),
                "replenished": rep["added"], "account": acct}

    async def create(self) -> dict:
        """Daily: scout LIVE niche trends first, then bank drafts (replenished
        when low — the create loop must never silently starve)."""
        acct = db.active_account()
        # trend scout: today's niche conversation → one grounded draft, so the
        # queue always has something born from what's happening RIGHT NOW
        trend_ids: list[int] = []
        try:
            from . import trend_scout as ts
            found = await ts.scout(self.cfg, self.x, acct)
            did = await asyncio.to_thread(ts.draft_from_findings,
                                          self.cfg, found["found"], acct)
            if did:
                trend_ids = [did]
                _tg_draft_cards(trend_ids)
        except Exception as e:  # noqa: BLE001 — scouting never blocks create
            db.log("trend-scout", f"scout skipped: {e}", level="warn")
        rep = await ideas_mod.replenish(self.cfg, x=self.x, acct=acct)  # no-op unless low
        if rep["added"]:
            db.log("create", f"[account {acct}] bank low ({rep['bank_before']}) — replenished "
                             f"+{rep['added']} from {','.join(rep['sources'])}")
        ids = await asyncio.to_thread(drafts_mod.generate_drafts, self.cfg, acct=acct)
        _tg_draft_cards(ids)
        out = {"drafts": len(trend_ids + ids), "account": acct,
               "trend_drafted": len(trend_ids)}
        if rep["ran"]:
            out["bank_replenished"] = rep["added"]
        return out

    async def engage(self) -> dict:
        """Hourly: pull mentions, draft replies + SCHEDULED niche replies."""
        acct = db.active_account()
        new = await replies_mod.pull_engagements(self.cfg, self.x, acct=acct)
        # live targets first: the gate can only pass what exists RIGHT NOW —
        # stored-only targets were 37-45h stale every single pass
        try:
            await replies_mod.refresh_niche_targets(self.cfg, self.x, acct)
        except Exception as e:  # noqa: BLE001 — refresh never blocks engage
            db.log("engage", f"live refresh skipped: {e}", level="warn")
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

    async def deep_train(self) -> dict:
        """DEEP TRAINING of the brain on the ACTIVE account — the full
        immersion chain, deeper than /study: max history import (posts AND
        replies, paged to 800), metrics ground truth, style+voice rebuild,
        niche study with deep pull + hooks, and a dedicated long reflection.
        Returns the brain report card. Read-only on X (no writes)."""
        import time as _time
        t0 = _time.time()
        acct = db.active_account()
        try:
            me = await self.x.me()          # live identity wins — training
        except Exception:                    # noqa: BLE001 — stored is a fine
            me = db.get_me(acct) or {}       # fallback when X is unreachable
        db.set_me(me, acct)

        # 1. full history: own posts + own replies, paged deep
        own = await self.x.user_tweets(me["username"], limit=800)
        replies = []
        try:
            replies = await self.x.user_replies(me["username"], limit=400)
        except Exception as e:  # noqa: BLE001 — replies are bonus, not a gate
            db.log("train", f"replies pull skipped: {e}", level="warn")
        for p in own + replies:
            db.upsert_post(p, acct)
        db.log("train", f"[account {acct}] history: {len(own)} posts + "
                        f"{len(replies)} replies ingested")

        # X rate-limits long read bursts: breathe between phases so a 429
        # never kills a training run that already ingested the history
        import asyncio as _aio

        async def _breathe(seconds: float) -> None:
            await _aio.sleep(seconds)

        # 2. metrics ground truth (time series + identity)
        from . import metrics as metrics_mod
        await _breathe(20)
        try:
            await metrics_mod.refresh_metrics(self.x, self.cfg, limit=60,
                                              acct=acct)
        except Exception as e:  # noqa: BLE001
            db.log("train", f"metrics refresh skipped: {e}", level="warn")

        # 3. style + voice rebuild from the full corpus (retry once on 429)
        await _breathe(30)
        scan_res: dict = {}
        for attempt in (1, 2):
            try:
                scan_res = await self.scan()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1 and "429" in str(e):
                    db.log("train", "X rate limit on scan — cooling 90s, "
                                    "retrying once", level="warn")
                    await _breathe(90)
                else:
                    db.log("train", f"scan phase skipped: {e}", level="warn")
                    break

        # 4. niche study + deep pull + hooks (retry once on 429)
        await _breathe(30)
        for attempt in (1, 2):
            try:
                await self.study()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1 and "429" in str(e):
                    db.log("train", "X rate limit on study — cooling 90s, "
                                    "retrying once", level="warn")
                    await _breathe(90)
                else:
                    db.log("train", f"study phase skipped: {e}", level="warn")
                    break

        # 5. dedicated reflection over everything
        brain_res = await _reflect("scan", self.cfg, acct)
        try:
            await _reflect("learn", self.cfg, acct)
        except Exception:  # noqa: BLE001
            pass

        # 6. the report card
        from . import brain as brain_mod
        from . import hooks as hooks_mod
        with db.connect() as c:
            (posts,) = c.execute(
                "SELECT COUNT(*) FROM posts WHERE account_id=? AND is_own=1",
                (acct,)).fetchone()
            (niche,) = c.execute(
                "SELECT COUNT(*) FROM posts WHERE account_id=? AND is_own=0",
                (acct,)).fetchone()
        report = {
            "account": acct, "handle": me.get("username"),
            "posts_ingested": len(own), "replies_ingested": len(replies),
            "own_total": posts, "niche_total": niche,
            "hooks": len(hooks_mod.list_hooks(acct)),
            "brain_rules": len(brain_mod.parse_rules(
                brain_mod.read("rules", acct))),
            "journal_entries": brain_mod.read("journal", acct).count("## "),
            "voice": scan_res.get("voice"), "brain": brain_res,
            "seconds": round(_time.time() - t0),
        }
        db.log("train", f"deep train complete: {report}")
        return report

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
                # link-reply (v0.6.x): a clean post can carry its link in the
                # FIRST REPLY instead of the body — keeps the main tweet
                # readable while the link still ships under it. Charged
                # against the reply cap; a cap bounce just skips the link,
                # the post itself is already out.
                link = (nxt.get("meta") or {}).get("link_reply")
                if link and x_id and nxt.get("kind") == "post":
                    try:
                        await self.x.post_tweet(str(link), reply_to=str(x_id))
                        db.log("publish", f"[account {acct}] link reply under "
                                          f"{x_id}: {str(link)[:60]}")
                    except SafetyCapExceeded:
                        db.log("publish", f"[account {acct}] link reply skipped "
                                          f"(reply cap) — post {x_id} is out",
                               level="warn")
                    except Exception as e:  # noqa: BLE001 — never lose the post
                        db.log("publish", f"[account {acct}] link reply failed: "
                                          f"{e}", level="warn")
            except SafetyCapExceeded as e:
                # reschedule to the next FREE slot — cap-bounced drafts must
                # spread across days/times, not pile onto tomorrow 09:00
                from . import slots as slots_mod
                tomorrow = (datetime.now() + timedelta(days=1)).date()
                if self.cfg.agent.smart_slots:
                    best = slots_mod.day_slots(self.cfg, tomorrow)
                    base_at = best[0]["at"] if best else \
                        datetime.combine(tomorrow, datetime.min.time()) + timedelta(hours=9)
                else:
                    t0 = (self.cfg.agent.post_times or ["09:00"])[0]
                    hh, mm = (int(x) for x in str(t0).split(":")[:2])
                    base_at = datetime.combine(tomorrow, datetime.min.time()) + timedelta(hours=hh, minutes=mm)
                at, why = slots_mod.nudge_free(base_at, self.cfg,
                                               slots_mod.taken_slots(acct))
                tmr = at.isoformat(timespec="seconds")
                db.update_draft(nxt["id"], acct=acct, scheduled_at=tmr)
                db.log("publish", f"[account {acct}] daily cap reached — draft {nxt['id']} rescheduled to {tmr}"
                       + (f" ({why})" if why else ""), level="warn")
                continue  # a post hitting the 4/day cap must not block a
                          # reply that still has its own 10/day budget
            except Exception as e:  # noqa: BLE001
                db.log("publish", f"[account {acct}] draft {nxt['id']} failed: {e}", level="error")
                db.update_draft(nxt["id"], acct=acct, status="failed")
        self._alert_stranded(acct)
        return {"published": published, "account": acct}

    def _alert_stranded(self, acct: int) -> None:
        """DUE approved drafts on OTHER accounts can never ship from this
        loop (the X client is bound to the ACTIVE account). Live incident
        2026-08-28: the owner approved 11 week-old account-1 cards from
        Telegram while account 2 was active — accepted, scheduled, silently
        unpublishable. Surface them loudly instead: log + one TG alert per
        draft (never auto-posting cross-account, never re-alerting)."""
        from datetime import datetime as _dt
        try:
            with db.connect() as c:
                rows = c.execute(
                    "SELECT id, account_id, scheduled_at FROM drafts "
                    "WHERE status='approved' AND account_id != ? "
                    "AND scheduled_at <= ? ORDER BY scheduled_at LIMIT 10",
                    (acct, _dt.now().isoformat(timespec="seconds"))).fetchall()
            stranded = {int(r["id"]): dict(r) for r in rows}
            if not stranded:
                db.set_setting("stranded_alerted", [])
                return
            already = set(db.get_setting("stranded_alerted") or [])
            fresh = [d for i, d in stranded.items() if i not in already]
            if not fresh:
                return
            db.log("publish", f"{len(fresh)} DUE approved draft(s) stranded on "
                            f"other accounts (active is {acct}): "
                            + ", ".join(f"#{d['id']}(acct {d['account_id']}, "
                                        f"{(d['scheduled_at'] or '')[:16]})"
                                        for d in fresh), level="warn")
            try:
                from ..integrations import telegram as tg_mod
                if tg_mod.is_enabled():
                    body = ["⚠️ Approved drafts the publish loop CANNOT ship —",
                            "they belong to another account:"]
                    for d in fresh:
                        body.append(f"• #{d['id']} — account {d['account_id']}, "
                                    f"was due {(d['scheduled_at'] or '')[:16]}")
                    body.append(f"Switch with /account <id> to publish them "
                                f"(active is {acct}), or reject them.")
                    tg_mod.notify_bg(chr(10).join(body))
            except Exception as e:  # noqa: BLE001 — alert delivery is best-effort
                db.log("publish", f"stranded alert delivery failed: {e}",
                       level="warn")
            db.set_setting("stranded_alerted",
                           sorted(already | set(stranded)))
        except Exception as e:  # noqa: BLE001 — never break the publish loop
            db.log("publish", f"stranded check failed: {e}", level="warn")

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
