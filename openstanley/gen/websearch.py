"""Web + X search for the agent — no paid APIs.

Two sources:
- DuckDuckGo HTML endpoint for the open web (no key, no SDK, just httpx)
- X search through the EXISTING cookie client (twikit) — the same search
  the study loop uses, now exposed on demand

Both are read-only. Chat turns them into drafts via the tool registry.
"""
from __future__ import annotations

import html as _html
import re
from typing import Optional

import httpx

from ..core.config import Config

DDG_URL = "https://html.duckduckgo.com/html/?q={q}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"}
TIMEOUT = 15.0
MAX_RESULTS = 6

_RES_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def web_search(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """DuckDuckGo HTML search → [{title, snippet, url}]. Empty on failure —
    the caller tells the user honestly instead of inventing results."""
    if not (query or "").strip():
        return []
    try:
        r = httpx.get(DDG_URL.format(q=httpx.QueryParams({"q": query}).get("q", query).replace(" ", "+")),
                      headers=UA, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return []
        out = []
        for m in _RES_RE.finditer(r.text):
            url, title, snippet = m.group(1), _strip_tags(m.group(2)), _strip_tags(m.group(3))
            # DDG wraps urls in a redirect — unwrap the uddg param
            um = re.search(r"uddg=([^&]+)", url)
            if um:
                from urllib.parse import unquote
                url = unquote(um.group(1))
            if title and snippet:
                out.append({"title": title[:120], "snippet": snippet[:280],
                            "url": url[:300]})
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001 — search must never take the chat down
        return []

def _own_client():
    """A FRESH XCookie bound to THIS caller's loop. Reusing the server's
    agent client from the TG poller thread broke with 'attached to a
    different loop' (user report 2026-08-24: x_trends returned ok=False)."""
    from ..x.client import XCookie
    cookies = _stored_cookies()
    if not cookies:
        raise RuntimeError("no X cookies stored, connect the account first")
    return XCookie(cookies)


def _stored_cookies() -> Optional[str]:
    import json as _json
    from ..core import db as _db
    _db.init_db()
    raw = _db.account_cookies(_db.active_account())
    if raw:
        try:
            _json.loads(raw)
            return raw
        except (TypeError, ValueError):
            pass
    import os
    return os.environ.get("OPENSTANLEY_X_COOKIES") or None


def x_search(cfg: Config, query: str, limit: int = 10) -> list[dict]:
    """Search X through a fresh cookie client, no API key, loop-safe from
    any thread (TG poller, chat workers, cron)."""
    import asyncio

    async def _run():
        x = _own_client()
        return await x.search(query, limit=limit)

    return asyncio.run(_run())


def x_trends(cfg: Config, limit: int = 10) -> list[str]:
    """X trending topics via a fresh cookie client (loop-safe anywhere)."""
    import asyncio

    async def _run():
        x = _own_client()
        c = await x._ensure()
        res = await c.get_trends("trending")
        return [str(t.name) for t in res][:limit]

    return asyncio.run(_run())
