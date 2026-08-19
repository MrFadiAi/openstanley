"""Live smoke self-check — REAL wiring probes, read-only by construction.

v0.3.7 problem this solves: tests are hermetic (fake X) and the dashboard
only shows stale `last_scan` info, so when cookies silently expire at 2am the
autopilot keeps ticking into a dead connection and nobody finds out for days.

`run_smoke(cfg)` fires cheap, read-only probes against the REAL account and
reports honestly:

  identity           x.me() → username/followers (confirms cookies + auth)
  timeline_read      x.user_tweets(me, limit=5) → count
  search_read        x.search(first evergreen theme, limit=5) → count
  notifications_read x.mentions(limit=5) — the notifications API is flaky,
                     so a failure here is a WARN (amber), never a FAIL
  llm                tiny gen.llm round-trip ("ping" → non-empty reply, ≤30s)
  brain              brain_context() non-empty + journal readable
  db                 write/read/delete on a `_smoke` settings key

Budget: ≤ 8 X-reads (probe design issues 4; cookie-mode user resolution has
headroom) and ≤ 60s wall clock (identity runs first to warm the client, the
rest run concurrently — worst case 15 + 30s of timeouts, well under budget).

NEVER posts. NEVER writes to X. Every probe is independently timed and
exception-guarded; one hanging or broken probe degrades only itself.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from ..core import db
from ..gen import brain as brain_mod
from ..gen.llm import chat as llm_chat

# per-probe wall-clock caps (s) — monkeypatchable in tests
PROBE_TIMEOUTS: dict[str, float] = {
    "identity": 15,
    "timeline_read": 15,
    "search_read": 15,
    "notifications_read": 15,
    "llm": 30,
    "brain": 5,
    "db": 5,
}

# a probe whose failure downgrades to WARN instead of FAIL
WARN_ONLY = {"notifications_read"}

LLM_PING_SYSTEM = "You are a connectivity probe. Reply with exactly: pong"


@dataclass
class ProbeResult:
    name: str
    ok: bool
    ms: int
    detail: str
    warn: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "ms": self.ms,
                "detail": self.detail, "warn": self.warn}


@dataclass
class SmokeReport:
    ok: bool                     # False only when a required probe failed
    status: str                  # green | amber | red
    ms: int
    x_reads: int
    ran_at: str
    probes: list[ProbeResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "status": self.status, "ms": self.ms,
                "x_reads": self.x_reads, "ran_at": self.ran_at,
                "probes": [p.to_dict() for p in self.probes]}


# ---------- X read accounting ----------

READ_METHODS = ("me", "user_tweets", "user_replies", "get_tweet",
                "search", "mentions")


class _CountingX:
    """Delegate wrapper that counts read ops against the ≤8 budget.
    Write methods are deliberately NOT forwarded — smoke cannot write."""

    def __init__(self, inner):
        self._inner = inner
        self.reads = 0

    def __getattr__(self, item):
        if item in READ_METHODS:
            self.reads += 1
        return getattr(self._inner, item)


# ---------- probes ----------

def _identity(x, shared: dict) -> Awaitable[str]:
    async def run() -> str:
        me = await x.me()
        shared["username"] = me.get("username", "")
        if not shared["username"]:
            raise RuntimeError("me() returned no username")
        return (f"@{shared['username']} "
                f"({me.get('followers', '?')} followers)")
    return run()


def _timeline(x, shared: dict) -> Awaitable[str]:
    async def run() -> str:
        username = shared.get("username") or ""
        if not username:
            raise RuntimeError("no username — identity probe failed")
        tweets = await x.user_tweets(username, limit=5)
        return f"{len(tweets)} tweets"
    return run()


def _search(cfg, x) -> Awaitable[str]:
    async def run() -> str:
        themes = cfg.agent.evergreen_themes or []
        query = (themes[0] if themes else "x")[:40]
        res = await x.search(query, limit=5)
        return f"{len(res)} results for {query!r}"
    return run()


def _notifications(x) -> Awaitable[str]:
    async def run() -> str:
        notes = await x.mentions(limit=5)
        return f"{len(notes)} notifications"
    return run()


def _llm(cfg, llm) -> Awaitable[str]:
    async def run() -> str:
        # sync httpx under the hood — keep a slow provider OFF the event loop
        # (startup smoke shares the server loop; a stall blocks every request)
        reply = await asyncio.to_thread(llm, cfg.llm,
                                        system=LLM_PING_SYSTEM, user="ping")
        reply = (reply or "").strip()
        if not reply:
            raise RuntimeError("empty reply")
        return f"reply: {reply[:40]!r}"
    return run()


def _brain() -> Awaitable[str]:
    async def run() -> str:
        ctx = brain_mod.brain_context()
        if not ctx.strip():
            raise RuntimeError("brain_context() is empty")
        entries = brain_mod.parse_journal(brain_mod.read("journal"))
        return f"ctx {len(ctx)} chars, {len(entries)} journal entries"
    return run()


def _db() -> Awaitable[str]:
    async def run() -> str:
        token = f"smoke-{int(time.time())}"
        db.set_setting("_smoke", token)
        got = db.get_setting("_smoke")
        with db.connect() as c:
            c.execute("DELETE FROM settings WHERE key='_smoke'")
        if got != token:
            raise RuntimeError(f"roundtrip mismatch: {got!r}")
        if db.get_setting("_smoke") is not None:
            raise RuntimeError("_smoke key not deleted")
        return "settings write/read/delete ok"
    return run()


def _default_llm(llm_cfg, system: str = "", user: str = "", **kw) -> str:
    """Tiny real round-trip: 16 max tokens, temperature 0 — pennies-free."""
    tiny = dataclasses.replace(llm_cfg, max_tokens=16, temperature=0.0)
    return llm_chat(tiny, system=system, user=user)


# ---------- runner ----------

async def _probe(name: str, make: Callable[[], Awaitable[str]]) -> ProbeResult:
    t0 = time.monotonic()
    try:
        detail = await asyncio.wait_for(make(), PROBE_TIMEOUTS[name])
        return ProbeResult(name, True, int((time.monotonic() - t0) * 1000), detail)
    except asyncio.TimeoutError:
        return ProbeResult(name, False,
                           int((time.monotonic() - t0) * 1000),
                           f"timeout after {PROBE_TIMEOUTS[name]}s",
                           warn=name in WARN_ONLY)
    except Exception as e:  # noqa: BLE001 — one probe failing must not kill the rest
        return ProbeResult(name, False,
                           int((time.monotonic() - t0) * 1000),
                           f"{type(e).__name__}: {str(e)[:160]}",
                           warn=name in WARN_ONLY)


async def run_smoke(cfg, x_client=None, llm: Callable | None = None) -> SmokeReport:
    """Run all probes against the real wiring and report honestly.

    `x_client` defaults to a fresh client from cfg (the server passes its
    live agent client so the probe reuses the warmed connection); `llm`
    defaults to a tiny real gen.llm round-trip. Both are injectable so
    tests stay hermetic.
    """
    if x_client is None:
        from ..x.client import build_client
        x_client = build_client(cfg)
    x = _CountingX(x_client)
    llm = llm or _default_llm
    t0 = time.monotonic()
    shared: dict = {}

    # identity first (warms the client connection), then the rest concurrently
    probes = [await _probe("identity", lambda: _identity(x, shared))]
    probes += await asyncio.gather(
        _probe("timeline_read", lambda: _timeline(x, shared)),
        _probe("search_read", lambda: _search(cfg, x)),
        _probe("notifications_read", lambda: _notifications(x)),
        _probe("llm", lambda: _llm(cfg, llm)),
        _probe("brain", _brain),
        _probe("db", _db),
    )

    failed_required = [p for p in probes if not p.ok and not p.warn]
    failed_warn = [p for p in probes if not p.ok and p.warn]
    status = "red" if failed_required else ("amber" if failed_warn else "green")
    return SmokeReport(
        ok=status != "red", status=status,
        ms=int((time.monotonic() - t0) * 1000),
        x_reads=x.reads, ran_at=datetime.now().isoformat(timespec="seconds"),
        probes=probes,
    )
