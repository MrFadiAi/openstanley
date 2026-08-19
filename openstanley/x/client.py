"""X access layer — one interface, three implementations.

- XApi  : official X API v2 (paid tier recommended)
- XCookie: twikit cookie-based client (free, unofficial)
- XDry  : dry-run simulator (no network) — safe default & demo mode

All return/accept plain dicts so the rest of the system never touches X libs.
"""
from __future__ import annotations

import asyncio
import functools
import json
import random
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

from ..core import db


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _auto_heal(retry: bool = True):
    """Cookie auto-heal guard for the XCookie request path.

    On a twikit auth failure (401/32, 403/353, KEY_BYTE family) it marks the
    cookies stale once and fires one cooldown-gated heal via Brave CDP (see
    openstanley/x/cookie_heal.py). Reads retry a single time after a successful
    heal — the retry re-enters _ensure() with the rebuilt client. Writes
    re-raise: an unauthorized POST never reached X, but side-effectful calls
    are never auto-retried. Never loops (cooldown + single retry).
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            try:
                return await fn(self, *args, **kwargs)
            except Exception as e:
                from . import cookie_heal
                if not cookie_heal.is_auth_failure(e):
                    raise
                if not await cookie_heal.handle_failure(self, e):
                    raise
                if not retry:
                    raise
                return await fn(self, *args, **kwargs)
        return wrapper
    return deco


class XClient(ABC):
    mode = "base"

    @abstractmethod
    async def me(self) -> dict: ...

    @abstractmethod
    async def user_tweets(self, username: str, limit: int = 100) -> list[dict]: ...

    async def user_replies(self, username: str, limit: int = 100) -> list[dict]:
        """Replies posted by the user. Default: unsupported (empty)."""
        return []

    async def get_tweet(self, x_id: str) -> dict:
        """Fetch one tweet (for quote-post previews). Default: unsupported."""
        return {"x_id": x_id, "text": "", "author": ""}

    @abstractmethod
    async def search(self, query: str, limit: int = 50) -> list[dict]: ...

    @abstractmethod
    async def mentions(self, limit: int = 50) -> list[dict]: ...

    @abstractmethod
    async def post_tweet(self, text: str, reply_to: Optional[str] = None,
                         media_path: Optional[str] = None,
                         quote_of: Optional[str] = None) -> dict: ...

    async def post_thread(self, tweets: list[str]) -> list[dict]:
        results, reply_to = [], None
        for t in tweets:
            r = await self.post_tweet(t, reply_to=reply_to)
            results.append(r)
            reply_to = r.get("x_id")
            if not reply_to:
                break
        return results


class XDry(XClient):
    """Simulated X for dry-run mode. Generates plausible history + trends."""
    mode = "dryrun"

    SAMPLE_OWN = [
        "shipped a feature today that took 3 weeks of prototyping. the lesson: build the ugly version first, it teaches you things slides never will.",
        "hot take: most 'AI agents' are just if-statements with a good resume.",
        "the best productivity system is a deadline you actually respect.",
        "reminder: you don't need permission to build. the internet is the permission.",
        "spent the morning debugging my own README. documentation is a love letter to your future self.",
        "unpopular: junior devs who ask 'stupid' questions daily outperform seniors who ask none yearly.",
        "my workflow now: think in systems, write in plain text, ship in public.",
        "3 years of side projects taught me more than 3 years of meetings ever did.",
    ]
    SAMPLE_NICHE = [
        "the real moat in 2026 isn't data. it's taste. everyone has the same models.",
        "agents that can't say 'no' aren't agents, they're interns with anxiety.",
        "your first 100 followers don't come from content. they come from comments.",
        "stop optimizing your funnel. start optimizing your replying speed.",
        "I posted daily for 90 days. here's what actually moved the needle: nothing until day 70.",
        "distribution > product. I'll die on this hill and I'll have visitors there.",
        "the algorithm doesn't hate you. your hooks are just boring. (mine were too.)",
        "write like you talk. edit like you're being read.",
    ]
    SAMPLE_MENTIONS = [
        {"handle": "@builder_bytes", "text": "what stack are you using for the agent?"},
        {"handle": "@devsara", "text": "this thread changed how I think about shipping, thank you"},
        {"handle": "@nightcoder", "text": "disagree — meetings aren't all waste, alignment matters"},
    ]
    SAMPLE_REPLIES = [
        "this is the way. most people quit before the compounding starts.",
        "counterpoint: the ugly version IS the design doc.",
        "been there. the second rewrite always ships faster than the first fix.",
        "exactly — taste is the only moat left when models are commodity.",
        "add to this: write the error message first, the code becomes obvious.",
    ]

    def __init__(self, username: str = "local_user"):
        self.username = username

    async def me(self) -> dict:
        return {"username": self.username, "name": "Local User (dry-run)", "followers": 421}

    async def user_tweets(self, username: str, limit: int = 100) -> list[dict]:
        out = []
        base = datetime.now() - timedelta(days=1)
        for i in range(min(limit, 120)):
            txt = self.SAMPLE_OWN[i % len(self.SAMPLE_OWN)]
            likes = random.randint(3, 90)
            out.append({
                "x_id": f"dry-own-{i}", "author_handle": username, "is_own": 1,
                "created_at": (base - timedelta(hours=7 * i)).isoformat(timespec="seconds"),
                "text": txt, "impressions": likes * random.randint(18, 40),
                "likes": likes, "reposts": int(likes * 0.2), "replies": int(likes * 0.15),
                "bookmarks": int(likes * 0.3),
            })
        return out

    async def user_replies(self, username: str, limit: int = 100) -> list[dict]:
        base = datetime.now() - timedelta(days=1)
        out = []
        for i in range(min(limit, 60)):
            txt = self.SAMPLE_REPLIES[i % len(self.SAMPLE_REPLIES)]
            likes = random.randint(0, 24)
            out.append({
                "x_id": f"dry-reply-{i}", "author_handle": username, "is_own": 1,
                "created_at": (base - timedelta(hours=5 * i)).isoformat(timespec="seconds"),
                "text": txt, "impressions": likes * 30,
                "likes": likes, "reposts": int(likes * 0.1), "replies": int(likes * 0.2),
                "bookmarks": int(likes * 0.1),
            })
        return out

    async def get_tweet(self, x_id: str) -> dict:
        return {"x_id": x_id, "author": "dryrun_user",
                "text": "(dry-run preview) this is a simulated tweet for "
                        "quote-post drafting — connect your account to see "
                        "the real text."}

    async def search(self, query: str, limit: int = 50) -> list[dict]:
        base = datetime.now() - timedelta(hours=2)
        out = []
        for i in range(min(limit, 60)):
            txt = random.choice(self.SAMPLE_NICHE)
            likes = random.randint(40, 900)
            out.append({
                "x_id": f"dry-niche-{query[:12]}-{i}", "author_handle": f"niche_user_{i}",
                "is_own": 0, "created_at": (base - timedelta(minutes=11 * i)).isoformat(timespec="seconds"),
                "text": txt, "impressions": likes * 22,
                "likes": likes, "reposts": int(likes * 0.25), "replies": int(likes * 0.12),
                "bookmarks": int(likes * 0.18),
            })
        return out

    async def mentions(self, limit: int = 50) -> list[dict]:
        return [
            {"x_id": f"dry-mention-{i}", "author_handle": m["handle"],
             "author_name": m["handle"].lstrip("@"), "text": m["text"],
             "created_at": _now_iso()}
            for i, m in enumerate(self.SAMPLE_MENTIONS[:limit])
        ]

    async def post_tweet(self, text: str, reply_to: Optional[str] = None,
                         media_path: Optional[str] = None,
                         quote_of: Optional[str] = None) -> dict:
        x_id = f"dry-post-{random.randint(10000, 99999)}"
        extra = []
        if reply_to:
            extra.append(f"reply_to={reply_to}")
        if media_path:
            extra.append(f"media={media_path}")
        if quote_of:
            extra.append(f"quote_of={quote_of}")
        db.log("publish", f"[dry-run] would post: {text[:80]}{'…' if len(text) > 80 else ''}"
                          + (f" ({', '.join(extra)})" if extra else ""))
        return {"x_id": x_id, "dry_run": True}


class XCookie(XClient):
    """twikit-based free client. Cookies come from the ACCOUNT row
    (accounts.cookies_json) with the .env bootstrap as a fallback for
    account 1 only (v0.5.0) — the DB always wins when present.

    All writes go through safety: daily caps + jittered human-like delays.
    """
    mode = "cookie"

    def __init__(self, cookies_json: str, username: str = "", caps: dict | None = None,
                 account_id: int = 1):
        from ..core.safety import human_delay, check_and_record, usage
        self._human_delay = human_delay
        self._check_and_record = check_and_record
        self._safety_usage = usage
        self._caps = caps or {"max_posts_per_day": 4, "max_replies_per_day": 10}
        self._cookies = cookies_json
        self.username = username
        self.account_id = account_id
        self._client = None
        self._last_request = 0.0

    def safety_status(self) -> dict:
        return {"caps": self._caps, "usage": self._safety_usage(self.account_id)}

    async def _throttle_reads(self) -> None:
        """Space out read requests too — no rapid-fire scraping bursts."""
        import time
        gap = 2.0 + (id(self) % 7) / 10  # per-instance 2.0-2.7s min gap
        now = time.monotonic()
        wait = self._last_request + gap - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _ensure(self):
        if self._client is not None:
            return self._client
        try:
            from twikit import Client
        except ImportError as e:
            raise RuntimeError("twikit not installed. Run: pip install twikit") from e
        from . import twikit_patch
        patched = twikit_patch.apply()
        if patched:
            db.log("system", f"twikit compat patches applied: {', '.join(patched)}")
        cookies = json.loads(self._cookies)
        if not isinstance(cookies, dict) or not cookies:
            raise RuntimeError("Cookies JSON invalid — expected an object like {\"auth_token\": \"...\", \"ct0\": \"...\"}")
        if "auth_token" not in cookies:
            raise RuntimeError("Cookies missing 'auth_token' — see dashboard → X Connect for how to grab cookies")
        c = Client("en-US")
        c.set_cookies(cookies)  # SYNC in twikit 2.3.3 — do NOT await
        me = await c.user()
        self.username = me.screen_name
        self._client = c
        return c

    @_auto_heal()
    async def me(self) -> dict:
        c = await self._ensure()
        u = await c.user()
        return {"username": u.screen_name, "name": u.name, "followers": u.followers_count}

    @_auto_heal()
    async def user_tweets(self, username: str, limit: int = 100) -> list[dict]:
        c = await self._ensure()
        await self._throttle_reads()
        u = await c.get_user_by_screen_name(username)
        tl = await c.get_user_tweets(u.id, tweet_type="Tweets", count=min(limit, 100))
        out = []
        for t in tl:
            out.append(self._tw(t, own=(username == self.username)))
        return out

    @_auto_heal()
    async def user_replies(self, username: str, limit: int = 100) -> list[dict]:
        c = await self._ensure()
        await self._throttle_reads()
        u = await c.get_user_by_screen_name(username)
        tl = await c.get_user_tweets(u.id, tweet_type="Replies", count=min(limit, 100))
        out = []
        for t in tl:
            out.append(self._tw(t, own=(username == self.username)))
        return out

    @_auto_heal()
    async def get_tweet(self, x_id: str) -> dict:
        c = await self._ensure()
        await self._throttle_reads()
        t = await c.get_tweet(x_id)
        author = getattr(getattr(t, "user", None), "screen_name", "")
        return {"x_id": t.id, "text": t.text, "author": author}

    @_auto_heal()
    async def search(self, query: str, limit: int = 50) -> list[dict]:
        c = await self._ensure()
        await self._throttle_reads()
        res = await c.search_tweet(query, "Top", count=min(limit, 50))
        return [self._tw(t) for t in res]

    @_auto_heal()
    async def mentions(self, limit: int = 5) -> list[dict]:
        c = await self._ensure()
        await self._throttle_reads()
        notif = await c.get_notifications(type="Mentions", count=min(limit, 40))
        out = []
        for n in notif[:limit]:
            out.append({
                "x_id": getattr(n, "id", None) or f"tw-{len(out)}",
                "author_handle": getattr(getattr(n, "user", None), "screen_name", ""),
                "author_name": getattr(getattr(n, "user", None), "name", ""),
                "text": getattr(n, "text", ""),
                "created_at": _now_iso(),
            })
        return out

    @_auto_heal(retry=False)
    async def post_tweet(self, text: str, reply_to: Optional[str] = None,
                         media_path: Optional[str] = None,
                         quote_of: Optional[str] = None) -> dict:
        c = await self._ensure()
        kind = "replies" if reply_to else "posts"
        self._check_and_record(kind, self._caps, acct=self.account_id)  # raises SafetyCapExceeded if over
        await self._human_delay((self._caps.get("min_delay_s", 5), self._caps.get("max_delay_s", 20)))
        kwargs: dict = {"text": text}
        if reply_to:
            kwargs["reply_to"] = reply_to
        if quote_of:
            kwargs["quote_tweet_id"] = quote_of
        if media_path:
            await self._throttle_reads()
            media = await c.upload_media(media_path)
            kwargs["media_id"] = media
        t = await c.create_tweet(**kwargs)
        return {"x_id": t.id}

    @_auto_heal(retry=False)
    async def post_thread(self, tweets: list[str]) -> list[dict]:
        # threads count as ONE post toward the cap, but still human-delay each tweet
        c = await self._ensure()
        self._check_and_record("posts", self._caps, acct=self.account_id)
        results = []
        reply_to = None
        for t in tweets:
            await self._human_delay((self._caps.get("min_delay_s", 5), self._caps.get("max_delay_s", 20)))
            r = await c.create_tweet(text=t, reply_to=reply_to) if reply_to else await c.create_tweet(text=t)
            results.append({"x_id": r.id})
            reply_to = r.id
        return results

    @staticmethod
    def _tw(t, own: bool = False) -> dict:
        def gi(x):
            try:
                return int(x or 0)
            except (TypeError, ValueError):
                return 0
        return {
            "x_id": t.id, "author_handle": getattr(getattr(t, "user", None), "screen_name", ""),
            "is_own": int(own),
            "created_at": getattr(t, "created_at", None) or _now_iso(),
            "text": t.text,
            "impressions": gi(getattr(t, "view_count", None) or getattr(t, "views", None)),
            "likes": gi(getattr(t, "favorite_count", None)),
            "reposts": gi(getattr(t, "retweet_count", None)),
            "replies": gi(getattr(t, "reply_count", None)),
            "bookmarks": gi(getattr(t, "bookmark_count", None)),
        }


class XApi(XClient):
    """Official X API v2 via OAuth 1.0a user context (tweepy)."""
    mode = "api"

    def __init__(self, bearer: str, api_key: str, api_secret: str,
                 access_token: str, access_secret: str):
        self._bearer, self._ak, self._as = bearer, api_key, api_secret
        self._at, self._asx = access_token, access_secret
        self._client = None
        self._v1 = None  # tweepy v1.1 API for media upload
        self.username = ""

    async def _ensure(self):
        if self._client is not None:
            return self._client
        try:
            import tweepy
        except ImportError as e:
            raise RuntimeError("tweepy not installed. Run: pip install tweepy") from e
        client = tweepy.Client(
            bearer_token=self._bearer or None,
            consumer_key=self._ak, consumer_secret=self._as,
            access_token=self._at, access_token_secret=self._asx,
            wait_on_rate_limit=True,
        )
        self._client = client
        return client

    async def me(self) -> dict:
        import asyncio
        c = await self._ensure()
        me = await asyncio.to_thread(c.get_me, user_fields=["public_metrics"])
        d = me.data
        self.username = d.username
        return {"username": d.username, "name": d.name,
                "followers": d.public_metrics.get("followers_count", 0)}

    async def user_tweets(self, username: str, limit: int = 100,
                          include_replies: bool = False) -> list[dict]:
        import asyncio
        c = await self._ensure()
        uid = (await asyncio.to_thread(c.get_user, username=username)).data.id
        resp = await asyncio.to_thread(
            c.get_users_tweets, uid, max_results=min(limit, 100),
            exclude_replies=not include_replies,
            tweet_fields=["public_metrics", "created_at"],
        )
        out = []
        for t in (resp.data or []):
            m = t.public_metrics or {}
            own = username == self.username
            out.append({
                "x_id": t.id, "author_handle": username, "is_own": int(own),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "text": t.text,
                "impressions": m.get("impression_count", 0),
                "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0),
                "replies": m.get("reply_count", 0), "bookmarks": m.get("bookmark_count", 0),
            })
        return out

    async def user_replies(self, username: str, limit: int = 100) -> list[dict]:
        return await self.user_tweets(username, limit, include_replies=True)

    async def get_tweet(self, x_id: str) -> dict:
        import asyncio
        c = await self._ensure()
        resp = await asyncio.to_thread(
            c.get_tweet, x_id, expansions=["author_id"],
            user_fields=["username"], tweet_fields=["created_at"],
        )
        t = resp.data
        users = resp.includes.get("users") if hasattr(resp, "includes") else []
        author = users[0].username if users else ""
        return {"x_id": t.id, "text": t.text, "author": author}

    async def search(self, query: str, limit: int = 50) -> list[dict]:
        import asyncio
        c = await self._ensure()
        resp = await asyncio.to_thread(
            c.search_recent_tweets, query, max_results=min(limit, 100),
            tweet_fields=["public_metrics", "created_at"],
            expansions=["author_id"], user_fields=["username"],
        )
        users = {u.id: u.username for u in (resp.includes.get("users") or [])} \
            if hasattr(resp, "includes") else {}
        out = []
        for t in (resp.data or []):
            m = t.public_metrics or {}
            out.append({
                "x_id": t.id, "author_handle": users.get(t.author_id, ""),
                "is_own": 0, "created_at": t.created_at.isoformat() if t.created_at else None,
                "text": t.text,
                "impressions": m.get("impression_count", 0),
                "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0),
                "replies": m.get("reply_count", 0), "bookmarks": m.get("bookmark_count", 0),
            })
        return out

    async def mentions(self, limit: int = 50) -> list[dict]:
        import asyncio
        c = await self._ensure()
        me_id = await self._me_id()
        resp = await asyncio.to_thread(
            c.get_users_mentions, me_id,
            max_results=min(limit, 100),
            tweet_fields=["created_at"],
            expansions=["author_id"], user_fields=["username", "name"],
        )
        users = {}
        if hasattr(resp, "includes"):
            for u in (resp.includes.get("users") or []):
                users[u.id] = (u.username, u.name)
        out = []
        for t in (resp.data or []):
            un, nm = users.get(t.author_id, ("", ""))
            out.append({
                "x_id": t.id, "author_handle": un, "author_name": nm,
                "text": t.text, "created_at": t.created_at.isoformat() if t.created_at else None,
            })
        return out

    async def post_tweet(self, text: str, reply_to: Optional[str] = None,
                         media_path: Optional[str] = None,
                         quote_of: Optional[str] = None) -> dict:
        import asyncio
        c = await self._ensure()
        kwargs: dict = {"text": text}
        if reply_to:
            kwargs["reply_in_reply_to_tweet_id"] = reply_to
        if quote_of:
            kwargs["quote_tweet_id"] = quote_of
        if media_path:
            media_id = await self._upload_media_v11(media_path)
            kwargs["media_ids"] = [media_id]
        resp = await asyncio.to_thread(c.create_tweet, **kwargs)
        return {"x_id": resp.data["id"]}

    async def _upload_media_v11(self, path: str) -> str:
        """Media upload needs the v1.1 API (v2 has no upload endpoint)."""
        import asyncio
        try:
            import tweepy
        except ImportError as e:
            raise RuntimeError("tweepy not installed. Run: pip install tweepy") from e
        if self._v1 is None:
            self._v1 = tweepy.API(tweepy.OAuth1UserHandler(
                self._ak, self._as, self._at, self._asx))
        media = await asyncio.to_thread(self._v1.media_upload, path)
        return media.media_id_string

    async def _me_id(self) -> str:
        c = await self._ensure()
        import asyncio
        return (await asyncio.to_thread(c.get_me)).data.id


def resolve_cookies(cfg, account_id: int) -> str:
    """Cookies for one account: the accounts row wins; the .env bootstrap
    value is a fallback for account 1 only (v0.5.0)."""
    import json as _json
    stored = db.account_cookies(account_id)
    if stored:
        return stored
    if account_id == 1:
        env_cookies = cfg.x.cookies
        if env_cookies:
            try:  # accept a JSON object or a raw auth_token string
                if isinstance(_json.loads(env_cookies), dict):
                    return env_cookies
            except (ValueError, TypeError):
                pass
    return ""


def build_client(cfg, account_id: int | None = None) -> XClient:
    """Factory from Config (x.mode). account_id defaults to the ACTIVE account."""
    import os
    acct = db.active_account() if account_id is None else account_id
    mode = cfg.x.mode
    if mode == "cookie":
        cookies = resolve_cookies(cfg, acct) or "{}"
        caps = {
            "max_posts_per_day": cfg.x.max_posts_per_day,
            "max_replies_per_day": cfg.x.max_replies_per_day,
            "min_delay_s": cfg.x.min_delay_s,
            "max_delay_s": cfg.x.max_delay_s,
        }
        username = cfg.x.username if acct == 1 else ""
        return XCookie(cookies, username, caps=caps, account_id=acct)
    if mode == "api":
        return XApi(
            bearer=os.environ.get(cfg.x.bearer_token_env, ""),
            api_key=os.environ.get(cfg.x.api_key_env, ""),
            api_secret=os.environ.get(cfg.x.api_secret_env, ""),
            access_token=os.environ.get(cfg.x.access_token_env, ""),
            access_secret=os.environ.get(cfg.x.access_secret_env, ""),
        )
    return XDry(cfg.x.username or "local_user")
