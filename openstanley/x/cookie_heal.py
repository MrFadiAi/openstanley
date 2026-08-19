"""Cookie auto-heal — durable real-account connection.

X rotates cookies (ct0 ~daily, auth_token on any browser re-login). In cookie
mode every read/write dies once they expire — until now a human had to re-pull
cookies by hand. This module closes the loop:

  detector  wraps the XCookie request path; on twikit auth failures it marks
            the cookies stale once and fires a heal attempt.
  healer    pulls fresh x.com cookies from a live Brave over CDP
            (``--remote-debugging-port=9222``, read-only
            ``Network.getAllCookies``), persists them to ``.env`` atomically,
            updates the in-memory env value and rebuilds the twikit client.

No browser is ever launched: healing only works when Brave is ALREADY
listening on 127.0.0.1:9222. The manual path (restart Brave with the debug
flag) is surfaced by the Connect tab instead. Heals are cooldown-gated
(default 10 min) so an expired session can never cause a request loop.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..core import db

ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT / ".env"
ENV_VAR = "XOPENSTANLEY_X_COOKIES"

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
HEAL_COOLDOWN_S = 600  # 10 min between heal attempts — never loop

# --- auth-failure fingerprints ------------------------------------------------
# X error codes: 32 = "could not authenticate you", 353 = forbidden/logged-out.
# KEY_BYTE: twikit's client-transaction init crash (manifest rotation family).
_STATUS_AUTH_RE = re.compile(r"status:\s*40[13]", re.IGNORECASE)
_CODE_AUTH_RE = re.compile(r"\bcode[:= ]\s*(?:32|353)\b", re.IGNORECASE)
_KEYBYTE_RE = re.compile(r"KEY_BYTE", re.IGNORECASE)

# module-level indirection so tests can fast-forward the cooldown clock
_monotonic = time.monotonic


class HealState:
    """Process-wide heal bookkeeping (read by /api/x/status)."""

    def __init__(self) -> None:
        self.stale: bool = False
        self.last_attempt: float | None = None   # monotonic
        self.last_heal: str | None = None        # wall-clock ISO or None
        self.heal_ok: bool | None = None         # last attempt's outcome

    def as_dict(self) -> dict:
        return {"stale": self.stale, "last_heal": self.last_heal,
                "heal_ok": self.heal_ok}


STATE = HealState()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- detector ------------------------------------------------------------------

def is_auth_failure(exc: BaseException) -> bool:
    """True when an exception means 'cookies are stale' (vs any other error).

    Matches twikit's Unauthorized/Forbidden classes when importable, and
    falls back to message fingerprints (401/403 status, X codes 32/353,
    KEY_BYTE-family) so the classifier also works with fakes in tests.
    """
    try:
        from twikit.errors import Forbidden, Unauthorized
        if isinstance(exc, (Unauthorized, Forbidden)):
            return True
    except ImportError:
        pass
    for klass in type(exc).__mro__:
        if klass.__name__ in ("Unauthorized", "Forbidden"):
            return True
    msg = str(exc)
    return bool(_STATUS_AUTH_RE.search(msg) or _CODE_AUTH_RE.search(msg)
                or _KEYBYTE_RE.search(msg))


def mark_stale(reason: BaseException | str) -> None:
    """Flag cookies stale — logs only on the fresh→stale transition (once)."""
    if not STATE.stale:
        db.log("system", f"cookies look stale ({str(reason)[:120]}) — "
                         "attempting auto-heal via Brave CDP")
    STATE.stale = True


# --- .env persistence ----------------------------------------------------------

def persist_cookies_env(env_path: Path, env_var: str, cookies_json: str) -> None:
    """Set ``env_var=cookies_json`` in .env, preserving every other line.

    Atomic: written to a temp file in the same directory, then os.replace'd
    over the target so a crash mid-write can never truncate the .env.
    """
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    out, seen = [], False
    for ln in lines:
        if ln.startswith(env_var + "="):
            out.append(f"{env_var}={cookies_json}")
            seen = True
        else:
            out.append(ln)
    if not seen:
        out.append(f"{env_var}={cookies_json}")
    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    import os
    os.replace(tmp, env_path)


# --- CDP cookie pull (read-only, no browser ever launched) ---------------------

def _cdp_targets() -> list[dict]:
    with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=5) as r:
        return json.load(r)


def _ws_call(ws_url: str, method: str, params: dict | None = None,
             msg_id: int = 1, timeout: int = 15) -> dict:
    """Minimal websocket client for one CDP call (no deps)."""
    import base64
    import os
    import socket
    parsed = urllib.parse.urlparse(ws_url)
    host, port = parsed.hostname, parsed.port or 80
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {parsed.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)

    def send_text(payload: str) -> None:
        data = payload.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        sock.sendall(bytes(header) + mask + masked)

    def recv_text() -> str:
        def recv_exact(n: int) -> bytes:
            out = b""
            while len(out) < n:
                chunk = sock.recv(n - len(out))
                if not chunk:
                    raise ConnectionError("closed")
                out += chunk
            return out
        while True:
            b1, b2 = recv_exact(2)
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            masked = b2 & 0x80
            if ln == 126:
                ln = int.from_bytes(recv_exact(2), "big")
            elif ln == 127:
                ln = int.from_bytes(recv_exact(8), "big")
            if masked:
                mask = recv_exact(4)
            data = recv_exact(ln) if ln else b""
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 1:
                return data.decode(errors="replace")
            if opcode == 8:
                raise ConnectionError("ws close")
            # ping/pong/continuation: ignore

    send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(recv_text())
        if msg.get("id") == msg_id:
            return msg


def pull_cookies_from_browser() -> dict | None:
    """x.com auth_token+ct0 from live Brave over CDP, or None.

    Never raises for the expected "can't heal" cases (no browser listening,
    logged out): those are reported as None so the caller can degrade.
    """
    try:
        targets = _cdp_targets()
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return None
        res = _ws_call(page["webSocketDebuggerUrl"], "Network.getAllCookies")
    except (OSError, ValueError, KeyError, ConnectionError):
        return None  # nothing listening on 9222 (or malformed) → not healable now
    cookies = res.get("result", {}).get("cookies", [])
    out = {c["name"]: c["value"] for c in cookies
           if str(c.get("domain", "")).endswith(".x.com")
           and c.get("name") in ("auth_token", "ct0")}
    return out or None


# --- healer --------------------------------------------------------------------

def _cooldown_active() -> bool:
    if STATE.last_attempt is None:
        return False
    return (_monotonic() - STATE.last_attempt) < HEAL_COOLDOWN_S


async def heal_cookies(xc: Any = None,
                       env_var: str | None = None,
                       env_path: Path | None = None) -> bool:
    """Pull fresh cookies from Brave and rebuild the client. True on success.

    ``xc`` is the live XCookie instance to heal in place (the server's agent
    holds the reference, so mutation beats replacement). Cooldown-gated:
    at most one attempt per HEAL_COOLDOWN_S regardless of who calls.
    env_var/env_path default to the module-level ENV_VAR/ENV_PATH at call
    time (monkeypatchable in tests).
    """
    env_var = env_var or ENV_VAR
    env_path = env_path if env_path is not None else ENV_PATH
    if _cooldown_active():
        return False
    STATE.last_attempt = _monotonic()

    fresh = pull_cookies_from_browser()
    if not fresh or "auth_token" not in fresh:
        STATE.heal_ok = False
        db.log("system", "cookie heal failed: no Brave on "
                         f"--remote-debugging-port={CDP_PORT} with an x.com "
                         "session (manual: restart Brave with the flag)", level="error")
        return False

    import os
    compact = json.dumps(fresh, separators=(",", ":"))
    try:
        current = json.loads(getattr(xc, "_cookies", "") or
                             os.environ.get(env_var, "") or "{}")
    except json.JSONDecodeError:
        current = {}
    token_rotated = current.get("auth_token") != fresh["auth_token"]
    ct0_rotated = current.get("ct0") != fresh.get("ct0")

    # persist + live process value (a restart or _rebuild_agent picks it up)
    persist_cookies_env(env_path, env_var, compact)
    os.environ[env_var] = compact

    # rebuild the twikit client: next _ensure() re-creates it with the new
    # cookies and re-resolves the user. The db "me" cache is dropped too —
    # a rotated auth_token may mean a different account logged in the browser,
    # and the next me() call re-populates it with verified data.
    if xc is not None:
        xc._cookies = compact
        xc._client = None
    if token_rotated:
        db.set_setting("me", None)

    STATE.stale = False
    STATE.heal_ok = True
    STATE.last_heal = _now_iso()
    kind = "auth_token rotated" if token_rotated else "ct0 refreshed"
    db.log("system", f"cookies self-healed from Brave CDP ({kind}; "
                     f"auth_token len={len(fresh['auth_token'])}, "
                     f"ct0 len={len(fresh.get('ct0', ''))})")
    return True


async def handle_failure(xc: Any, exc: BaseException) -> bool:
    """Detector entry point for the XCookie request path: mark + try to heal."""
    mark_stale(exc)
    return await heal_cookies(xc)


def status() -> dict:
    """Heal fields for /api/x/status."""
    return STATE.as_dict()
