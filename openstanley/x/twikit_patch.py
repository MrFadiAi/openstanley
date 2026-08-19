"""Runtime compatibility patches for twikit 2.3.3 against X's rotated frontend.

Why this exists (verified against live x.com on 2026-08-19):
- twikit 2.3.3 raises ``Exception: Couldn't get KEY_BYTE indices`` for every
  request: ``ClientTransaction.init`` can no longer locate the
  ``ondemand.s.<hash>a.js`` webpack chunk in x.com's home-page HTML, so it
  cannot derive the indices that feed the ``X-Client-Transaction-Id`` header.
- Upstream ``d60/twikit`` master is stale (no code change since 2025-04).
  The community fix — PR d60/twikit#432 (adapted from #411, two-step chunk
  lookup) — is still unmerged, and by our own anonymous probe the logged-out
  page no longer contains the chunk manifest at all: X moved to the new
  "x-web" bundle (``entry-client-logged-out-*.js``) with zero ``ondemand``
  references. Cookie-authenticated pages may still serve the legacy
  responsive-web manifest, so both formats are handled below.

Three layers, all idempotent, all skipped when twikit already handles things:

1. ``get_indices`` replacement — PR #432's two-step lookup (chunk index →
   chunk hash) with the legacy single-step format as fallback.
2. Graceful degradation — if the manifest still can't be found, requests
   proceed WITHOUT ``X-Client-Transaction-Id`` instead of dying client-side
   (a vendored, marker-checked copy of twikit 2.3.3's ``Client.request``
   omits the header when it can't be computed). X treats the header as one
   bot-scoring signal, not a hard gate; a headerless read beats no read.
3. ``User.__init__`` guards (also PR #432) — X now omits
   ``entities.description.urls`` / ``pinned_tweet_ids_str`` /
   ``withheld_in_countries`` from some user payloads, which raised KeyError.
"""
from __future__ import annotations

import re
from typing import Any

# --- chunk-manifest patterns -------------------------------------------------
# New format (webpack object keyed by chunk id): `,1234:"ondemand.s"`
_NEW_ON_DEMAND_RE = re.compile(r',(\d+):["\']ondemand\.s["\']')
# Legacy format: `"ondemand.s":"<hash>"`
_LEGACY_ON_DEMAND_RE = re.compile(r"""['|"]ondemand\.s['|"]:\s*['|"]([\w]*)['|"]""")
_ON_DEMAND_HASH_TEMPLATE = r',{chunk}:["\']([0-9a-f]+)["\']'
# New bundle uses bare `[n],16` array accesses; legacy used `(e[n],16)`
_NEW_INDICES_RE = re.compile(r"\[(\d+)\],\s*16")
_LEGACY_INDICES_RE = re.compile(r"\(\w\[(\d{1,2})\],\s*16\)")

_ONDEMAND_URL = "https://abs.twimg.com/responsive-web/client-web/ondemand.s.{h}a.js"

# Placeholder indices used only in degraded mode — a header is never generated
# from them (see _request below), they just let init() finish cleanly.
_PLACEHOLDER = (2, [42, 45])

applied: list[str] = []


async def _patched_get_indices(self, home_page_response, session, headers):
    """Replacement for ClientTransaction.get_indices (twikit 2.3.3).

    Tries the current webpack chunk-map format (PR d60/twikit#432), then the
    legacy format, and finally marks the transaction generator degraded
    instead of raising "Couldn't get KEY_BYTE indices".
    """
    text = str(home_page_response)

    async def fetch(url: str) -> str:
        resp = await session.request(method="GET", url=url, headers=headers)
        return resp.text

    indices: list[str] = []
    try:
        chunk = _NEW_ON_DEMAND_RE.search(text)
        if chunk:
            h = re.search(_ON_DEMAND_HASH_TEMPLATE.format(chunk=chunk.group(1)), text)
            if h:
                js = await fetch(_ONDEMAND_URL.format(h=h.group(1)))
                indices = [m.group(1) for m in _NEW_INDICES_RE.finditer(js)]
    except Exception:  # noqa: BLE001 — degrade, never crash the read path
        indices = []

    if not indices:
        try:
            legacy = _LEGACY_ON_DEMAND_RE.search(text)
            if legacy:
                js = await fetch(_ONDEMAND_URL.format(h=legacy.group(1)))
                indices = [m.group(1) for m in _LEGACY_INDICES_RE.finditer(js)]
        except Exception:  # noqa: BLE001
            indices = []

    if not indices:
        self.degraded = True
        return _PLACEHOLDER
    return int(indices[0]), [int(i) for i in indices[1:]]


async def _request(self, method: str, url: str,
                   auto_unlock: bool = True, raise_exception: bool = True,
                   **kwargs) -> tuple[dict | Any, object]:
    """Vendored copy of twikit 2.3.3 Client.request with one change:

    ``X-Client-Transaction-Id`` is only sent when it could actually be
    computed (the stock version raises inside init()/generate_transaction_id
    when X's home page has no ondemand manifest).
    """
    import json as _json
    from urllib.parse import urlparse as _urlparse

    from twikit.constants import DOMAIN
    from twikit.errors import (AccountLocked, AccountSuspended, BadRequest,
                               Forbidden, NotFound, RequestTimeout,
                               ServerError, TooManyRequests,
                               TwitterException, Unauthorized)

    headers = kwargs.pop("headers", {})

    if not self.client_transaction.home_page_response:
        cookies_backup = self.get_cookies().copy()
        ct_headers = {
            "Accept-Language": f"{self.language},{self.language.split('-')[0]};q=0.9",
            "Cache-Control": "no-cache",
            "Referer": f"https://{DOMAIN}",
            "User-Agent": self._user_agent,
        }
        try:
            await self.client_transaction.init(self.http, ct_headers)
        except Exception:  # noqa: BLE001 — degrade instead of hard-failing
            self.client_transaction.degraded = True
            self.client_transaction.home_page_response = True  # block re-init
        self.set_cookies(cookies_backup, clear_cookies=True)

    if not getattr(self.client_transaction, "degraded", False):
        try:
            tid = self.client_transaction.generate_transaction_id(
                method=method, path=_urlparse(url).path)
            if tid:
                headers["X-Client-Transaction-Id"] = tid
        except Exception:  # noqa: BLE001
            pass

    cookies_backup = self.get_cookies().copy()
    response = await self.http.request(method, url, headers=headers, **kwargs)
    self._remove_duplicate_ct0_cookie()

    try:
        response_data = response.json()
    except _json.decoder.JSONDecodeError:
        response_data = response.text

    if isinstance(response_data, dict) and "errors" in response_data:
        error_code = response_data["errors"][0]["code"]
        error_message = response_data["errors"][0].get("message")
        if error_code in (37, 64):
            raise AccountSuspended(error_message)

        if error_code == 326:
            if self.captcha_solver is None:
                raise AccountLocked(
                    "Your account is locked. Visit "
                    f"https://{DOMAIN}/account/access to unlock it.")
            if auto_unlock:
                await self.unlock()
                self.set_cookies(cookies_backup, clear_cookies=True)
                response = await self.http.request(method, url, **kwargs)
                self._remove_duplicate_ct0_cookie()
                try:
                    response_data = response.json()
                except _json.decoder.JSONDecodeError:
                    response_data = response.text

    status_code = response.status_code

    if status_code >= 400 and raise_exception:
        message = f'status: {status_code}, message: "{response.text}"'
        if status_code == 400:
            raise BadRequest(message, headers=response.headers)
        elif status_code == 401:
            raise Unauthorized(message, headers=response.headers)
        elif status_code == 403:
            raise Forbidden(message, headers=response.headers)
        elif status_code == 404:
            raise NotFound(message, headers=response.headers)
        elif status_code == 408:
            raise RequestTimeout(message, headers=response.headers)
        elif status_code == 429:
            if await self._get_user_state() == "suspended":
                raise AccountSuspended(message, headers=response.headers)
            raise TooManyRequests(message, headers=response.headers)
        elif 500 <= status_code < 600:
            raise ServerError(message, headers=response.headers)
        else:
            raise TwitterException(message, headers=response.headers)

    return response_data, response


def _guarded_user_init(orig_init):
    """Wrap a User.__init__ so missing legacy fields default instead of KeyError."""
    def wrapper(self, client, data):
        legacy = (data or {}).get("legacy")
        if isinstance(legacy, dict):
            entities = legacy.setdefault("entities", {})
            if isinstance(entities, dict):
                desc = entities.setdefault("description", {})
                if isinstance(desc, dict):
                    desc.setdefault("urls", [])
            legacy.setdefault("pinned_tweet_ids_str", [])
            legacy.setdefault("withheld_in_countries", [])
        orig_init(self, client, data)
    return wrapper


def apply() -> list[str]:
    """Apply all patches (idempotent). Returns the list of patch names applied
    on THIS call — empty when everything was already in place."""
    fresh: list[str] = []
    try:
        from twikit.x_client_transaction.transaction import ClientTransaction
    except ImportError:
        return fresh  # twikit restructured — nothing we can do here

    if not getattr(ClientTransaction.get_indices, "_openstanley_patched", False):
        _patched_get_indices._openstanley_patched = True  # type: ignore[attr-defined]
        ClientTransaction.get_indices = _patched_get_indices
        fresh.append("get_indices")

    from twikit.client.client import Client as TwikitClient
    if not getattr(TwikitClient.request, "_openstanley_patched", False):
        _request._openstanley_patched = True  # type: ignore[attr-defined]
        TwikitClient.request = _request
        fresh.append("request-degrades-without-transaction-id")

    for mod_name, label in (("twikit.user", "user-guards"),
                            ("twikit.guest.user", "guest-user-guards")):
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, "User", None)
            if cls and not getattr(cls.__init__, "_openstanley_patched", False):
                cls.__init__ = _guarded_user_init(cls.__init__)
                cls.__init__._openstanley_patched = True  # type: ignore[attr-defined]
                fresh.append(label)
        except ImportError:
            continue

    for name in fresh:
        if name not in applied:
            applied.append(name)
    return fresh
