"""Web + X search for the agent — no paid APIs.

Two sources:
- DuckDuckGo HTML endpoint for the open web (no key, no SDK, just httpx)
- X search through the EXISTING cookie client (twikit) — the same search
  the study loop uses, now exposed on demand

Both are read-only. Chat turns them into drafts via the tool registry.

TinyFish (2026-08-28): when a free tinyfish.ai API key is set (Settings or
OPENSTANLEY_TINYFISH_KEY), web_search/web_read route through it FIRST —
ranked results + browser-rendered markdown at $0 — and fall back to the
DDG/reader path on any error, so the agent never loses the capability.
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

TINYFISH_SEARCH_URL = "https://api.search.tinyfish.ai/search"
TINYFISH_FETCH_URL = "https://api.fetch.tinyfish.ai/"  # the ROOT — verified live
# 2026-08-28: /fetch et al. 404 (site page); POST the root with {urls}

_RES_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _tinyfish_key() -> str:
    """Free key from tinyfish.ai — Settings value wins, env is the fallback."""
    import os
    try:
        from ..core import db as _db
        k = _db.get_setting("tinyfish_api_key")
        if k:
            return str(k)
    except Exception:  # noqa: BLE001 — settings must never break search
        pass
    return os.environ.get("OPENSTANLEY_TINYFISH_KEY", "")


def _tinyfish_search(query: str, limit: int) -> list[dict]:
    """TinyFish /search -> the same [{title, snippet, url}] shape. Empty on
    any failure (the caller falls back to DDG — never lose the capability)."""
    key = _tinyfish_key()
    if not key:
        return []
    try:
        r = httpx.get(TINYFISH_SEARCH_URL,
                      headers={"X-API-Key": key},
                      params={"query": query,
                              "purpose": "source research for on-voice X "
                                         "content the owner will approve"},
                      timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        out = []
        for hit in (r.json().get("results") or [])[:limit]:
            title = str(hit.get("title") or "").strip()
            url = str(hit.get("url") or "").strip()
            snippet = str(hit.get("snippet") or "").strip()
            if title and url:
                out.append({"title": title[:120], "snippet": snippet[:280],
                            "url": url[:300],
                            "site": str(hit.get("site_name") or "")[:60]})
        return out
    except Exception:  # noqa: BLE001 — provider errors fall back silently
        return []


def _tinyfish_fetch(url: str, max_chars: int) -> Optional[dict]:
    """TinyFish /fetch (browser-rendered markdown) -> web_read's dict shape,
    or None when unkeyed/failed (caller falls back to the plain reader)."""
    key = _tinyfish_key()
    if not key:
        return None
    try:
        r = httpx.post(TINYFISH_FETCH_URL,
                       headers={"X-API-Key": key},
                       json={"urls": [url], "format": "markdown"},
                       timeout=45)
        if r.status_code != 200:
            return None
        results = r.json().get("results") or []
        if not results:
            return None
        page = results[0] or {}
        text = str(page.get("text") or "").strip()
        if not text:
            return None
        return {"ok": True, "title": str(page.get("title") or "")[:120],
                "url": url, "length": len(text), "text": text[:max_chars],
                "via": "tinyfish"}
    except Exception:  # noqa: BLE001
        return None


def web_search(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """[{title, snippet, url}] — TinyFish first when keyed ($0, ranked),
    DuckDuckGo HTML as the always-there fallback. Empty on failure —
    the caller tells the user honestly instead of inventing results."""
    if not (query or "").strip():
        return []
    tf = _tinyfish_search(query, limit)
    if tf:
        return tf[:limit]
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
    """X trending topics via a fresh cookie client (loop-safe anywhere).
    The trends endpoint 404s transiently (code 34) — one retry fixes it."""
    import asyncio
    import time as _time

    async def _run():
        x = _own_client()
        c = await x._ensure()
        last = None
        for attempt in (1, 2):
            try:
                res = await c.get_trends("trending")
                return [str(t.name) for t in res][:limit]
            except Exception as e:  # noqa: BLE001 — transient 404s clear on retry
                last = e
                if attempt == 1:
                    _time.sleep(2)
        raise last

    return asyncio.run(_run())


def web_read(url: str, max_chars: int = 6000) -> dict:
    """Fetch a URL and return readable text — the agent's way IN to any page
    (articles, docs, dashboards), like a browser's reader mode. No JS, no
    auth; honest error when the page refuses."""
    import re as _re
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    tf = _tinyfish_fetch(url, max_chars)
    if tf:
        return tf
    try:
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"fetch failed: {type(e).__name__}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    html = r.text
    # strip the noise: scripts, styles, tags, entities — reader-mode style
    html = _re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ",
                   html, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"<[^>]+>", " ", html)
    text = _html.unescape(text)
    text = _re.sub(r"\s+", " ", text).strip()
    title = ""
    m = _re.search(r"<title[^>]*>(.*?)</title>", html,
                    _re.IGNORECASE | _re.DOTALL)
    if m:
        title = _html.unescape(m.group(1)).strip()[:120]
    if not text:
        return {"ok": False, "error": "no readable text (JS-only page?)"}
    return {"ok": True, "title": title, "url": str(r.url), "length": len(text),
            "text": text[:max_chars]}
