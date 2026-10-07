"""Shared vivarium crypto core: signing + self-maintaining keys.

One-time deployment: NOTHING here needs manual rotation.
- VG cookie: auto-refreshed with a real (UC) browser when upstream starts
  403ing, plus every 12h. Works without a browser too: set VIVARIUM_VG env
  or POST /api/admin/vg and it hot-swaps (persisted to disk).
- U_HEX / X_CV: re-derived from vivarium's own JS by the same decode chain
  a browser runs (homepage loader -> /api/k -> wasm-sign -> decrypt layers
  -> find the table holding 'x-nonce'). Runs once in background at import;
  live values hot-swap in when validated. Compiled-in fallbacks keep
  serving meanwhile.
- WASM bytes: always fetched fresh from the (parsed) static URL at startup.

Same speed: wasmtime signing is ~ms, nonces are pooled (6 per /api/n),
HTTP uses one keep-alive session.
"""
import base64
import ctypes
import email.utils
import gzip
import json
import os
import re
import threading
import time

import requests
import wasmtime
from requests.adapters import HTTPAdapter

VIV = os.environ.get("VIVARIUM_BASE_URL", "https://vivarium.su")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Referer": f"{VIV}/", "Origin": VIV}

# ---- secrets: env first, then disk state, then empty (no keys in code) ----
# Local dev: copy .env.example to .env and fill it in. Render: set the same
# names in the dashboard. The background bootstrap re-derives U/XCV from the
# site's own JS and refreshes VG on its own, so these are only starting values.
def _load_dotenv():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        for line in open(os.path.join(here, ".env"), encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except OSError:
        pass


_load_dotenv()
_FB_VG = os.environ.get("VIVARIUM_VG", "")
_FB_U = os.environ.get("VIVARIUM_U_HEX", "")
_FB_XCV = os.environ.get("VIVARIUM_XCV", "")
_FB_WASM_URL = os.environ.get("VIVARIUM_WASM_URL", f"{VIV}/_next/static/media/hkf9.bin")

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE = os.path.join(_HERE, ".vivcrypto.json")

# ---- live values (hot-swapped) ----
_L = threading.Lock()
VG = os.environ.get("VIVARIUM_VG", "")
U_HEX = ""
X_CV = ""
WASM_URL = os.environ.get("VIVARIUM_WASM_URL", _FB_WASM_URL)
KEY_SOURCE = {"vg": "fallback", "crypto": "fallback"}
_VG_EXPIRY = {"at": 0.0}

S = requests.Session()
S.headers.update(HEADERS)
try:
    S.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=2))
    S.mount("http://", HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=2))
except Exception:
    pass


def _load_state():
    global VG, U_HEX, X_CV
    try:
        d = json.load(open(_STATE, encoding="utf-8"))
        if d.get("vg"):
            VG = d["vg"]
            KEY_SOURCE["vg"] = "disk"
        if d.get("u") and d.get("xcv"):
            U_HEX, X_CV = d["u"], d["xcv"]
            KEY_SOURCE["crypto"] = "disk"
        _VG_EXPIRY["at"] = float(d.get("vg_expiry", 0) or 0)
    except Exception:
        pass
    if not VG:
        VG, KEY_SOURCE["vg"] = _FB_VG, "fallback"
    if not U_HEX:
        U_HEX, X_CV, KEY_SOURCE["crypto"] = _FB_U, _FB_XCV, "fallback"


def _save_state():
    try:
        json.dump({"vg": VG, "u": U_HEX, "xcv": X_CV,
                   "vg_expiry": _VG_EXPIRY["at"]}, open(_STATE, "w", encoding="utf-8"))
    except Exception:
        pass


def _vg_expiry(vg: str) -> float:
    try:
        return float(vg.split(".")[0])
    except Exception:
        return 0.0


_load_state()
S.cookies.set("vg", VG, domain="vivarium.su", path="/")
if not _VG_EXPIRY["at"]:
    _VG_EXPIRY["at"] = _vg_expiry(VG)

# ---------------- wasmtime -------------
_store = wasmtime.Store()
_wasm_bytes = b""
_wasm_url_used = ""


def _wasm_init(wasm_bytes: bytes):
    global _wasm_bytes
    mod = wasmtime.Module(_store.engine, wasm_bytes)
    inst = wasmtime.Instance(_store, mod, [])
    exp = inst.exports(_store)
    base = ctypes.addressof(exp["memory"].data_ptr(_store).contents)
    _wasm_bytes = wasm_bytes
    return exp, base


try:
    _wasm_bytes = S.get(WASM_URL, headers={"User-Agent": UA}, timeout=15).content
    _wasm_url_used = WASM_URL
except Exception:
    pass
_exp, _BASE = _wasm_init(_wasm_bytes) if _wasm_bytes else (None, None)


def wasm32_hex(msg: str) -> str:
    b = msg.encode()
    ctypes.memmove(_BASE + _exp["in_ptr"](_store), b, len(b))
    _exp["sign"](_store, len(b))
    op = _exp["out_ptr"](_store)
    out = (ctypes.c_ubyte * 32)()
    ctypes.memmove(out, _BASE + op, 32)
    return bytes(out).hex()


# ---------------- generic obfuscated-table decoder -------------
def _imul(a, b):
    return ((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF)) & 0xFFFFFFFF


def decode_table(js: str):
    """Decode one obfuscated string table. Returns list[str] or raises."""
    calls = [(m.group(1), m.group(2)) for m in re.finditer(r"\}\)\(\$(\w+),\$(\w+)\)", js)]
    if len(calls) < 2:
        raise ValueError("table calls not found")
    yvar, alphavar = None, None
    arrvar, offvar = None, None
    assigns = {}
    for m in re.finditer(r"\$(\w+)=\"([^\"]{32,})\"", js):
        assigns[m.group(1)] = m.group(2)
    for a, b in calls:
        if a in assigns and b in assigns and len(assigns[b]) == 64:
            yvar, alphavar = a, b
            break
    if not yvar:
        raise ValueError("y/alphabet not found")
    rest = [c for c in calls if c != (yvar, alphavar)]
    if not rest:
        raise ValueError("offset call not found")
    arrvar, offvar = rest[0]
    y, alphabet = assigns[yvar], assigns[alphavar]
    fm = re.search(r"function \$(\w+)\(i\)\{if\(i in \$(\w+)\)", js)
    if not fm:
        raise ValueError("decoder func not found")
    fbody = js[fm.start():fm.start() + 2500]
    sm = re.search(r"\(\$(\w+)\^Math\.imul\(i\+1,(\d+)\)", fbody)
    if not sm:
        raise ValueError("seed not found")
    seedvar, imul_c = sm.group(1), int(sm.group(2))
    sm2 = re.search(re.escape("$" + seedvar) + r"=(\d{9,10})", js)
    seed = int(sm2.group(1)) if sm2 else None
    if seed is None:
        raise ValueError("seed value not found")
    add = int(re.search(r"\(h\+(\d+)\)>>>0", fbody).group(1))
    shifts = re.findall(r"h>>>(\d+)", fbody)
    ors = re.findall(r"h\|(\d+)", fbody)
    if len(shifts) < 3 or len(ors) < 2:
        raise ValueError("keystream consts not found")
    s1, s2, s3 = int(shifts[0]), int(shifts[1]), int(shifts[2])
    o1, o2 = int(ors[0]), int(ors[1])
    mm = {c: i for i, c in enumerate(alphabet)}
    raw = []
    w = k = 0
    for ch in y:
        c = mm.get(ch)
        if c is None:
            continue
        w = (w << 6) | c
        k += 6
        if k >= 8:
            k -= 8
            raw.append((w >> k) & 255)
    p = raw[0] | raw[1] << 8
    off = []
    q = 2 + p * 2
    for i in range(p):
        off.append(q)
        q += raw[2 + i * 2] | raw[3 + i * 2] << 8

    def dec(i):
        t, n = off[i], raw[2 + i * 2] | raw[3 + i * 2] << 8
        h = (seed ^ _imul(i + 1, imul_c)) & 0xFFFFFFFF
        r = [0] * n
        for j in range(n):
            h = (h + add) & 0xFFFFFFFF
            h = _imul((h ^ (h >> s1)) & 0xFFFFFFFF, (h | o1) & 0xFFFFFFFF)
            a = (h ^ (h >> s2)) & 0xFFFFFFFF
            h = (h ^ ((h + _imul(a, (h | o2) & 0xFFFFFFFF)) & 0xFFFFFFFF)) & 0xFFFFFFFF
            r[j] = raw[t + j] ^ ((h ^ (h >> s3)) & 255)
        return bytes(r).decode("utf-8", errors="replace")

    return [dec(i) for i in range(p)]


def _b64_xor91(b64: str) -> str:
    raw = base64.b64decode(b64)
    return bytes(c ^ 91 for c in raw).decode("utf-8", errors="replace")


def _try_aes_gzip(b64: str, key_hex: str):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(b64)
    pt = AESGCM(bytes.fromhex(key_hex)).decrypt(raw[:12], raw[12:], None)
    try:
        return gzip.decompress(pt).decode("utf-8")
    except Exception:
        return pt.decode("utf-8")


# ---------------- dynamic crypto bootstrap -------------
_BOOT = {"running": False, "last": "", "ok": False}


def bootstrap_crypto():
    """Re-derive U_HEX/X_CV (+wasm URL) from the site's own JS. True on swap."""
    global U_HEX, X_CV, WASM_URL, _exp, _BASE, _wasm_bytes
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": f"{VIV}/"})
    home = sess.get(f"{VIV}/", timeout=15).text
    m = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', home)
    if not m:
        raise ValueError("loader stub not found")
    loader = _b64_xor91(m.group(1))
    if not loader.startswith("/"):
        raise ValueError("bad loader path")
    ljs = sess.get(VIV + loader, headers={"Referer": f"{VIV}/"}, timeout=15).text
    table = decode_table(ljs)
    wasm_cands = [s for s in table if s.endswith(".bin")]
    enc_cands = [s for s in table if re.fullmatch(r"/_next/static/chunks/[0-9a-f]{16}\.js", s or "")]
    if not wasm_cands or not enc_cands:
        raise ValueError("wasm/enc chunk not found")
    WASM_URL = VIV + wasm_cands[0]
    wbytes = sess.get(WASM_URL, timeout=15).content
    # /api/k needs a working VG
    k = sess.get(f"{VIV}/api/k", cookies={"vg": VG},
                 headers={"Referer": f"{VIV}/"}, timeout=15).text.strip()
    if len(k) != 32 or any(c not in "0123456789abcdef" for c in k):
        raise ValueError("bad /api/k")
    exp_, base_ = _temp_wasm(wbytes)

    def ksign(msg: str) -> str:
        b = msg.encode()
        ctypes.memmove(base_ + exp_["in_ptr"](_store), b, len(b))
        exp_["sign"](_store, len(b))
        op = exp_["out_ptr"](_store)
        out = (ctypes.c_ubyte * 16)()
        ctypes.memmove(out, base_ + op, 16)
        return bytes(out).hex()

    signed = ksign(k)
    enc_b64 = sess.get(VIV + enc_cands[0], headers={"Referer": f"{VIV}/"}, timeout=20).text.strip()
    layer2 = _try_aes_gzip(enc_b64, signed)
    t2 = decode_table(layer2)
    chunk_paths = [s for s in t2 if s.startswith("/_next/static/") and s.endswith(".js")]
    if not chunk_paths:
        raise ValueError("no chunks in map")
    found_u = found_xcv = None
    for cp in chunk_paths:
        try:
            cjs = sess.get(VIV + cp, headers={"Referer": f"{VIV}/"}, timeout=15).text.strip()
            pt = _try_aes_gzip(cjs, signed)
            t = decode_table(pt)
        except Exception:
            continue
        if "x-nonce" not in t:
            continue
        u_c = [s for s in t if re.fullmatch(r"[0-9a-f]{64}", s or "")]
        try:
            xi = t.index("x-cv")
        except ValueError:
            continue
        xcv_c = t[xi + 1] if xi + 1 < len(t) and re.fullmatch(r"[0-9a-f]+", t[xi + 1] or "") else None
        if u_c and xcv_c:
            found_u, found_xcv = u_c[0], xcv_c
            break
    if not (found_u and found_xcv):
        raise ValueError("signing table not found")
    # validate end-to-end before trusting
    if not _validate_crypto(wbytes, found_u, found_xcv):
        raise ValueError("validation failed")
    with _L:
        U_HEX, X_CV = found_u, found_xcv
        KEY_SOURCE["crypto"] = "live"
        _exp, _BASE = _temp_wasm_keep(wbytes)
        _save_state()
    return True


def _temp_wasm(wbytes: bytes):
    mod = wasmtime.Module(_store.engine, wbytes)
    inst = wasmtime.Instance(_store, mod, [])
    exp = inst.exports(_store)
    base = ctypes.addressof(exp["memory"].data_ptr(_store).contents)
    return exp, base


def _temp_wasm_keep(wbytes: bytes):
    global _wasm_bytes, _wasm_url_used
    exp, base = _temp_wasm(wbytes)
    _wasm_bytes, _wasm_url_used = wbytes, WASM_URL
    return exp, base


def _validate_crypto(wbytes: bytes, u_hex: str, xcv: str) -> bool:
    """One cheap signed call must return real streams."""
    try:
        exp_, base_ = _temp_wasm(wbytes)

        def sig32(msg: str) -> str:
            b = msg.encode()
            ctypes.memmove(base_ + exp_["in_ptr"](_store), b, len(b))
            exp_["sign"](_store, len(b))
            op = exp_["out_ptr"](_store)
            out = (ctypes.c_ubyte * 32)()
            ctypes.memmove(out, base_ + op, 32)
            return bytes(out).hex()

        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": f"{VIV}/", "Origin": VIV})
        s.cookies.set("vg", VG, domain="vivarium.su", path="/")
        n = s.get(f"{VIV}/api/n", timeout=10).json()[0]
        ts = int(time.time())
        qs = "/api/e?id=550&type=movie"
        sig = sig32(f"GET\n{qs}\n{ts}\n{n}\n{u_hex}")
        hd = dict(HEADERS)
        hd.update({"x-ts": str(ts), "x-nonce": n, "x-cv": xcv, "x-sig": sig})
        d = s.get(f"{VIV}{qs}", headers=hd, timeout=25).json()
        return bool(isinstance(d, dict) and d.get("streams"))
    except Exception:
        return False


def bootstrap_background():
    if _BOOT["running"]:
        return
    _BOOT["running"] = True

    def run():
        try:
            bootstrap_crypto()
            _BOOT["ok"] = True
            _BOOT["last"] = "ok"
        except Exception as e:
            _BOOT["last"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            _BOOT["running"] = False

    threading.Thread(target=run, daemon=True).start()


# ---------------- VG auto-refresh -------------
_VG_LOCK = threading.Lock()


def refresh_vg_browser(timeout=120) -> str:
    """Pass Turnstile in a real UC browser, return fresh vg cookie."""
    from seleniumbase import SB
    with SB(uc=True, test=False, headless=True) as sb:
        sb.open(f"{VIV}/")
        sb.sleep(15)
        for c in sb.get_cookies():
            if c.get("name") == "vg" and c.get("value"):
                return c["value"]
    raise RuntimeError("vg cookie not issued")


def _vg_ok() -> bool:
    try:
        r = S.get(f"{VIV}/api/n", timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def ensure_vg() -> bool:
    """Make sure VG works; refresh via browser on 403. True if usable."""
    if _vg_ok():
        return True
    with _VG_LOCK:
        if _vg_ok():
            return True
        try:
            set_vg(refresh_vg_browser(), "browser")
            return _vg_ok()
        except Exception as e:
            _BOOT["last"] = f"vg-refresh: {type(e).__name__}: {e}"[:200]
            return False


def set_vg(vg: str, source="manual"):
    global VG
    with _L:
        VG = vg.strip()
        KEY_SOURCE["vg"] = source
        _VG_EXPIRY["at"] = _vg_expiry(VG)
        S.cookies.set("vg", VG, domain="vivarium.su", path="/")
        _save_state()


def _vg_maintainer():
    while True:
        time.sleep(12 * 3600)
        try:
            if time.time() > _VG_EXPIRY["at"] - 24 * 3600:
                ensure_vg()
        except Exception:
            pass


# ---------------- shared signing -------------
_NONCES = []
_NONCES_DATE = {"date": None}
_NONCES_LOCK = threading.Lock()


def _load_nonce_batch():
    global _NONCES_DATE
    r = S.get(f"{VIV}/api/n", timeout=10)
    if r.status_code == 403:
        if not ensure_vg():
            raise RuntimeError("vg expired and browser refresh unavailable; "
                               "set VIVARIUM_VG in .env (local) or Render dashboard, "
                               "or POST /api/admin/vg with a fresh vg cookie")
        r = S.get(f"{VIV}/api/n", timeout=10)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("empty nonce payload from /api/n")
    with _NONCES_LOCK:
        _NONCES.extend(payload)
        _NONCES_DATE["date"] = r.headers.get("Date")


def _warm_nonce_pool(target=12):
    try:
        while len(_NONCES) < target:
            _load_nonce_batch()
    except Exception:
        pass


def sign(path_qs: str):
    while True:
        with _NONCES_LOCK:
            if _NONCES:
                nonce = _NONCES.pop(0)
                break
        _load_nonce_batch()
    l = 0
    try:
        ds = _NONCES_DATE["date"]
        if ds:
            l = email.utils.parsedate_to_datetime(ds).timestamp() * 1000 - time.time() * 1000
    except Exception:
        pass
    ts = int((time.time() * 1000 + l) // 1000)
    sig = wasm32_hex(f"GET\n{path_qs}\n{ts}\n{nonce}\n{U_HEX}")
    hd = dict(HEADERS)
    hd.update({"x-ts": str(ts), "x-nonce": nonce, "x-cv": X_CV, "x-sig": sig})
    return hd


def is_dub(s: dict) -> bool:
    return "dub" in (s.get("quality") or "").lower()


bootstrap_background()
threading.Thread(target=lambda: (_warm_nonce_pool(), None), daemon=True).start()
threading.Thread(target=_vg_maintainer, daemon=True).start()
