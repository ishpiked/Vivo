"""Vivarium enc-dec style API (mirrors https://enc-dec.app/api/ which only supports cinejoy).

Envelope matches enc-dec.app: {"status": 200, "result": {...}}.

One-time deployment: no keys to rotate by hand. Crypto (VG cookie, U_HEX,
X_CV) is maintained by vivcrypto.py: VG auto-refreshes through a real
browser on expiry, U_HEX/X_CV are re-derived from the site's own JS in the
background. See GET /api/admin/status.

Endpoints:
  GET  /api/health-vivarium            -> providers (like wing.st/servers)
  GET  /api/servers                    -> FOREGROUND servers: Aster (english dub
                                          anime) + Vexa (japanese audio anime).
                                          All real providers still run in the
                                          background; only these two are shown.
  GET  /api/enc-vivarium?id=&type=&s=&e=
       -> {"path","headers","cookies"} signed request kit (like enc-cinejoy data+state).
          Client then GETs https://vivarium.su<path> themselves.
  POST /api/dec-vivarium  {"response": <raw /api/e JSON>, "dub": bool, "provider": str|None, "server": "aster"|"vexa"|None}
       -> {"streams":[...],"subtitles":[...],"qualities":[...]} filtered (like dec-cinejoy).
  GET  /api/vivarium?id=&type=&s=&e=&dub=&provider=&server=&race=
       -> one-shot convenience: server signs+fetches the FULL provider list in
          the background, classifies into Aster/Vexa, returns the chosen
          foreground server's streams. race=true takes the first sub-server
          link that fits instead of waiting for all.
  GET  /api/admin/status               -> key sources, vg expiry, bootstrap state
  POST /api/admin/vg {"vg": "..."}     -> hot-swap VG cookie (no restart)

Run: uvicorn api:app --port 8000
Requires: pip install fastapi uvicorn wasmtime requests
Optional for VG auto-refresh: pip install seleniumbase (needs Chrome)
"""
import re
import time
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

import vivcrypto as vc
from vivcrypto import VIV, HEADERS

_CACHE = {}  # (id,type,s,e,server,dub,provider,race) -> (at, result); TTL 120s
_CACHE_TTL = 120


def sign_request(path_qs: str):
    return path_qs, vc.sign(path_qs), {"vg": vc.VG}


class AdminVgBody(BaseModel):
    vg: Optional[str] = None


def build_qs(id: str, type: str, s: Optional[str], e: Optional[str]) -> str:
    if type == "movie":
        return f"/api/e?id={id}&type=movie"
    if not s or not e:
        raise ValueError("tv requires s and e")
    return f"/api/e?id={id}&type={type}&s={s}&e={e}"


# FOREGROUND servers. Only these two are shown; every real provider
# (Febbox, Hermes, ...) is still fetched in the background and its streams
# are classified into one of these two.
SERVERS = [
    {"server": "Aster", "audio": "dub",
     "description": "English dub anime"},
    {"server": "Vexa", "audio": "japanese",
     "description": "Japanese audio anime with subtitles"},
]


def is_dub(s: dict) -> bool:
    return "dub" in (s.get("quality") or "").lower()


def classify_streams(data: dict):
    """Split background provider streams into the two foreground servers."""
    streams = data.get("streams", []) or []
    aster = sorted([x for x in streams if is_dub(x)],
                   key=lambda x: x.get("rank", 0), reverse=True)
    vexa = sorted([x for x in streams if not is_dub(x)],
                  key=lambda x: (bool(x.get("subs")), x.get("rank", 0)),
                  reverse=True)
    return {"aster": aster, "vexa": vexa}


def filter_streams(data: dict, dub: bool = False, provider: Optional[str] = None,
                   server: Optional[str] = None):
    streams = data.get("streams", []) or []
    if server:
        server = server.lower()
        if server == "aster":
            streams = [x for x in streams if is_dub(x)]
        elif server == "vexa":
            streams = [x for x in streams if not is_dub(x)]
    elif dub:
        streams = [x for x in streams if is_dub(x)]
    if provider:
        streams = [x for x in streams if (x.get("provider") or "").lower() == provider.lower()]
    if server and server.lower() == "vexa":
        # Subtitled streams first: a vexa pick without subs is a broken pick.
        streams = sorted(streams, key=lambda x: (bool(x.get("subs")), x.get("rank", 0)),
                         reverse=True)
    return {"streams": streams, "subtitles": data.get("subtitles", []),
            "qualities": quality_list(streams)}


def parse_quality(s: dict) -> dict:
    """Normalize one stream's quality label into comparable fields."""
    q = (s.get("quality") or "").strip() or "Auto"
    m = re.search(r"(\d{3,4})\s*p", q, re.I)
    height = int(m.group(1)) if m else 0
    return {"label": q, "height": height,
            "dub": is_dub(s), "hevc": bool(s.get("hevc")),
            "hdr": bool(s.get("hdr")), "provider": s.get("provider"),
            "type": s.get("type")}


def quality_list(streams) -> list:
    """Every available quality, de-duplicated, best first."""
    seen = {}
    for s in streams or []:
        p = parse_quality(s)
        key = (p["label"].lower(), (p["provider"] or "").lower())
        if key not in seen:
            seen[key] = p
    return sorted(seen.values(),
                  key=lambda p: (p["height"], p["label"]), reverse=True)


def wants_for(server_or_dub, s: dict) -> bool:
    if s.get("type") != "hls":
        return False
    if server_or_dub in ("aster", True):
        return is_dub(s)
    if server_or_dub in ("vexa",):
        return not is_dub(s) and bool(s.get("subs"))
    return True


def race_streams(path_qs: str, server_or_dub, timeout: int = 25):
    """Parallel sub-server race: /api/es fans out to every provider at once,
    first `source` event fitting the foreground server wins the playback."""
    import json
    es_qs = path_qs.replace("/api/e", "/api/es", 1)
    _, headers, _ = sign_request(es_qs)
    r = vc.S.get(f"{VIV}{es_qs}", headers=headers,
                  timeout=timeout, stream=True)
    buf = ""
    try:
        for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
            if not chunk:
                continue
            buf += chunk
            while "\n\n" in buf:
                evt, buf = buf.split("\n\n", 1)
                if "event: source" not in evt:
                    if "event: done" in evt:
                        return None
                    continue
                for line in evt.split("\n"):
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    for s in d.get("streams", []):
                        if wants_for(server_or_dub, s):
                            return s
    finally:
        r.close()
    return None


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class DecBody(BaseModel):
    response: Optional[dict] = None
    dub: bool = False
    provider: Optional[str] = None
    server: Optional[str] = None


@app.get("/")
def root():
    return {"status": "ok", "service": "vivarium-api"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/api/servers")
def servers():
    return {"status": 200, "result": {"servers": SERVERS}}


@app.get("/api/health-vivarium")
def health_vivarium():
    r = vc.S.get(f"{VIV}/api/health", timeout=15)
    if r.status_code != 200:
        return {"status": r.status_code, "result": "", "error": "Upstream health failed"}
    return {"status": 200, "result": r.json()}


@app.get("/api/enc-vivarium")
def enc_vivarium(
    id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    s: Optional[str] = Query(None),
    e: Optional[str] = Query(None),
):
    if not id or not type:
        return {"status": 400, "result": "", "error": "Expected query: id, type",
                "hint": "GET: /api/enc-vivarium?id=[tmdb_or_internal]&type=[movie|tv]&s=[season]&e=[episode]"}
    if type not in ("movie", "tv"):
        return {"status": 400, "result": "", "error": "Invalid type",
                "hint": "type must be movie|tv"}
    try:
        path = build_qs(id, type, s, e)
    except ValueError as ex:
        return {"status": 400, "result": "", "error": str(ex)}
    path, headers, cookies = sign_request(path)
    return {"status": 200, "result": {"path": path, "headers": headers, "cookies": cookies,
                                      "url": f"{VIV}{path}"}}


@app.post("/api/dec-vivarium")
def dec_vivarium(body: DecBody):
    if not isinstance(body.response, dict) or "streams" not in body.response:
        return {"status": 400, "result": "", "error": "Expected body: response, [dub, provider, server]",
                "hint": "POST: { 'response': <raw /api/e JSON>, 'dub': false }"}
    if body.server and body.server.lower() not in ("aster", "vexa"):
        return {"status": 400, "result": "", "error": "Invalid server",
                "hint": "server must be aster|vexa"}
    return {"status": 200, "result": filter_streams(body.response, body.dub, body.provider, body.server)}


@app.get("/api/vivarium")
def vivarium(
    id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    s: Optional[str] = Query(None),
    e: Optional[str] = Query(None),
    dub: bool = Query(False),
    provider: Optional[str] = Query(None),
    server: Optional[str] = Query(None),
    race: bool = Query(False),
):
    if not id or not type:
        return {"status": 400, "result": "", "error": "Expected query: id, type",
                "hint": "GET: /api/vivarium?id=[tmdb_or_internal]&type=[movie|tv]&s=[season]&e=[episode]&server=[aster|vexa]"}
    if server and server.lower() not in ("aster", "vexa"):
        return {"status": 400, "result": "", "error": "Invalid server",
                "hint": "server must be aster|vexa"}
    try:
        path = build_qs(id, type, s, e)
    except ValueError as ex:
        return {"status": 400, "result": "", "error": str(ex)}
    key = (id, type, s, e, (server or "").lower(), bool(dub),
           (provider or "").lower(), bool(race))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        res = dict(hit[1])
        res["cached"] = True
        return {"status": 200, "result": res}
    if race:
        # Parallel race: first sub-server link that fits wins (fastest play).
        won = race_streams(path, server.lower() if server else (True if dub else None))
        if won:
            res = filter_streams({"streams": [won], "subtitles": []}, dub, provider, server)
            _CACHE[key] = (time.time(), res)
            return {"status": 200, "result": res}
        # Race missed (slow round): fall back to the full background list.
    _, headers, _ = sign_request(path)
    r = vc.S.get(f"{VIV}{path}", headers=headers, timeout=30)
    if r.status_code != 200:
        return {"status": r.status_code, "result": "", "error": "Upstream fetch failed"}
    res = filter_streams(r.json(), dub, provider, server)
    _CACHE[key] = (time.time(), res)
    return {"status": 200, "result": res}


@app.get("/api/admin/status")
def admin_status():
    import datetime
    exp = vc._VG_EXPIRY["at"]
    return {"status": 200, "result": {
        "vg_source": vc.KEY_SOURCE["vg"],
        "vg_expiry_utc": (datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat()
                          if exp else None),
        "crypto_source": vc.KEY_SOURCE["crypto"],
        "crypto_bootstrap": vc._BOOT,
        "wasm_url": vc.WASM_URL,
    }}


@app.post("/api/admin/vg")
def admin_vg(body: AdminVgBody):
    if not body.vg or len(body.vg.strip()) < 20:
        return {"status": 400, "result": "", "error": "Expected body: vg",
                "hint": "POST: { 'vg': '<vg cookie value from browser>' }"}
    vc.set_vg(body.vg, "admin")
    ok = vc.ensure_vg()
    return {"status": 200 if ok else 502,
            "result": {"vg_source": vc.KEY_SOURCE["vg"], "usable": ok}}
