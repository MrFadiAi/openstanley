"""Read x.com auth cookies from live Brave via CDP (port 9222).
Read-only: uses Network.getAllCookies on an existing/blank target."""
import json
import urllib.request

def cdp_targets():
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        return json.load(r)

def ws_call(ws_url, method, params=None, msg_id=1, timeout=15):
    import socket, base64, os
    # minimal websocket client (no deps)
    parsed = urllib.parse.urlparse(ws_url)
    host, port = parsed.hostname, parsed.port or 80
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {parsed.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    # read headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    def send_text(payload):
        data = payload.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(data)
        if n < 126: header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126); header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127); header += n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        sock.sendall(bytes(header) + mask + masked)
    def recv_text():
        def recv_exact(n):
            out = b""
            while len(out) < n:
                chunk = sock.recv(n - len(out))
                if not chunk: raise ConnectionError("closed")
                out += chunk
            return out
        while True:
            b1, b2 = recv_exact(2)
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            masked = b2 & 0x80
            if ln == 126: ln = int.from_bytes(recv_exact(2), "big")
            elif ln == 127: ln = int.from_bytes(recv_exact(8), "big")
            if masked: mask = recv_exact(4)
            data = recv_exact(ln) if ln else b""
            if masked: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 1: return data.decode(errors="replace")
            if opcode == 8: raise ConnectionError("ws close")
            # ping/pong/continuation: ignore
    send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(recv_text())
        if msg.get("id") == msg_id:
            return msg

targets = cdp_targets()
page = next((t for t in targets if t.get("type") == "page"), None)
if not page:
    raise SystemExit("no page target")
res = ws_call(page["webSocketDebuggerUrl"], "Network.getAllCookies")
cookies = res.get("result", {}).get("cookies", [])
xc = [c for c in cookies if c["domain"].endswith(".x.com") and c["name"] in ("auth_token", "ct0")]
if not xc:
    print("NO x.com cookies in browser session — likely logged out")
else:
    out = {c["name"]: c["value"] for c in xc}
    # write to openstanley .env format + report lengths only
    import pathlib
    env = pathlib.Path(r"D:\ai\openstanley\.env")
    compact = json.dumps(out, separators=(",", ":"))
    lines = env.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if not l.startswith("XOPENSTANLEY_X_COOKIES=")]
    lines.append(f"XOPENSTANLEY_X_COOKIES={compact}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"cookies pulled from live Brave: {sorted(out)} (auth_token len={len(out.get('auth_token',''))}, ct0 len={len(out.get('ct0',''))}) — persisted to .env")
