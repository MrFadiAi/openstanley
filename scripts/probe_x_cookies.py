"""Probe Chromium-family cookie stores for x.com auth cookies (read-only).
Reports which encryption version (v10/v11/v20) without printing values."""
import base64, ctypes, ctypes.wintypes as wt, json, os, shutil, sqlite3, tempfile

CRYPTPROTECT_UI_FORBIDDEN = 0x01

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def dpapi_decrypt(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)

BROWSERS = {
    "Brave": r"C:\Users\Fadinl\AppData\Local\BraveSoftware\Brave-User-Data",
    "Chrome": r"C:\Users\Fadinl\AppData\Local\Google\Chrome\User Data",
    "Edge": r"C:\Users\Fadinl\AppData\Local\Microsoft\Edge\User Data",
}

def probe(name, root):
    print(f"\n=== {name} ===")
    ls = os.path.join(root, "Local State")
    if not os.path.exists(ls):
        print("  no Local State"); return
    # enumerate profiles
    profiles = [p for p in os.listdir(root) if (p == "Default" or p.startswith("Profile ")) and os.path.isdir(os.path.join(root, p))]
    for prof in profiles:
        ck = os.path.join(root, prof, "Network", "Cookies")
        if not os.path.exists(ck):
            ck2 = os.path.join(root, prof, "Cookies")
            if not os.path.exists(ck2):
                continue
            ck = ck2
        tmp = os.path.join(tempfile.gettempdir(), f"ck_{name}_{prof}.db")
        try:
            shutil.copy2(ck, tmp)
        except PermissionError:
            print(f"  {prof}: cookie db locked (copy failed)"); continue
        try:
            con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
            rows = con.execute(
                "SELECT name, length(encrypted_value), substr(encrypted_value,1,3) FROM cookies WHERE host_key LIKE '%.x.com' AND name IN ('auth_token','ct0')"
            ).fetchall()
            con.close()
        except Exception as e:
            print(f"  {prof}: read error {e}"); continue
        if rows:
            for n, ln, pref in rows:
                print(f"  {prof}: {n}  len={ln}  version={pref!r}")
        else:
            print(f"  {prof}: no x.com auth cookies")
    # also check key container
    try:
        st = json.load(open(ls, encoding="utf-8"))
        oc = st.get("os_crypt", {})
        abk = oc.get("app_bound_encrypted_key")
        print(f"  app_bound key present: {bool(abk)}")
    except Exception as e:
        print("  Local State parse error", e)

for n, r in BROWSERS.items():
    if os.path.exists(r):
        probe(n, r)
    else:
        print(f"\n=== {n} === not installed")
