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


def x_search(cfg: Config, query: str, limit: int = 10) -> list[dict]:
    """Search X through the cookie client — no API key, same path study uses.
    Returns [{text, author, likes, impressions}]."""
    import asyncio
    from ..server import __main__ as srv

    async def _run():
        return await srv.agent.x.search(query, limit=limit)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # called from inside the server's loop (tool exec runs in a thread)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.run(asyncio.run, _run())
    return asyncio.run(_run())


def x_trends(cfg: Config, limit: int = 10) -> list[str]:
    """X trending topics via the cookie client (twikit get_trends)."""
    import asyncio
    from ..server import __main__ as srv

    async def _run():
        c = await srv.agent.x._ensure()
        res = await c.get_trends("trending")
        return [str(t.name) for t in res][:limit]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.run(asyncio.run, _run())
    return asyncio.run(_run())
