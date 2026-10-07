import os
import sys
import time, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vivcrypto as vc
from vivcrypto import VIV

HEADERS = {
    "Accept": "*/*",
    "Origin": "https://vivarium.su",
    "Referer": "https://vivarium.su/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
}

# Keys are maintained by vivcrypto.py (auto-refresh, dynamic re-derive).
# Nothing to rotate by hand.
wasm32_hex = vc.wasm32_hex


def validate(data, path):
    if isinstance(data, dict) and data.get("streams") is not None:
        return data
    print(f"\n{'-'*25} API ERROR {'-'*25}\n")
    print(f"Path: {path}")
    print(f"Data: {str(data)[:500]}")
    raise SystemExit


_SESS = vc.S


def _sign(path_qs):
    return vc.sign(path_qs)


def signed_get(path_qs, timeout=30):
    """Signed GET /api/e (full, all providers, ~2-7s cold, ~350ms warm)."""
    hd = _sign(path_qs)
    rr = _SESS.get(f"https://vivarium.su{path_qs}", headers=hd, timeout=timeout)
    return validate(rr.json(), path_qs)


def signed_sse_first(path_qs, timeout=30):
    """Signed GET /api/es, return first source event (~500-700ms)."""
    hd = _sign(path_qs)
    rr = _SESS.get(f"https://vivarium.su{path_qs}", headers=hd, timeout=timeout, stream=True)
    buf = ""
    for chunk in rr.iter_content(chunk_size=1024, decode_unicode=True):
        if not chunk:
            continue
        buf += chunk
        while "\n\n" in buf:
            evt, buf = buf.split("\n\n", 1)
            if "event: source" in evt:
                for line in evt.split("\n"):
                    if line.startswith("data:"):
                        d = json.loads(line[5:].strip())
                        if d.get("streams"):
                            rr.close()
                            return validate({"streams": d["streams"], "subtitles": []}, path_qs)
    rr.close()
    raise SystemExit(f"No streams for {path_qs}")

# Note that there are different providers, find them here: https://vivarium.su/api/health
# Movie format: </api/e?id={tmdb_id}&type=movie>
# Tv format: </api/e?id={tmdb_id}&type=tv&s={season_number}&e={episode_number}>

# --- Game of Thrones ---
title = "Game of Thrones"
type = "tv"
tmdb_id = "1399"
season = "1"
episode = "1"
fast = True  # True = SSE first-result (~700ms), False = full /api/e (~4s, all providers)

if type == "movie":
    qs_e = f"/api/e?id={tmdb_id}&type=movie"
    qs_es = f"/api/es?id={tmdb_id}&type=movie"
else:
    qs_e = f"/api/e?id={tmdb_id}&type={type}&s={season}&e={episode}"
    qs_es = f"/api/es?id={tmdb_id}&type={type}&s={season}&e={episode}"

t0 = time.time()
if fast:
    result = signed_sse_first(qs_es)
else:
    result = signed_get(qs_e)
ms = (time.time() - t0) * 1000

print(f"\n{'-'*25} Decrypted Data ({ms:.0f}ms) {'-'*25}\n")
print(f"Referer: {HEADERS['Referer']}\n")
print(f"Title: {title} ({type} {tmdb_id}" + (f" S{season}E{episode}" if type == "tv" else "") + ")")
for s in result.get("streams", []):
    print(f"\n[{s.get('provider')}] {s.get('quality')} ({s.get('type')}): {s.get('url')}")
