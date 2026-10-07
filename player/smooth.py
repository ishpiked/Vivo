"""Smooth vivarium player: background fetches ALL providers, foreground shows
one of two servers, mirrors HLS locally with parallel prefetch so seeks hit
local disk (ms) instead of the CDN (seconds).

Foreground servers:
  aster  English dub anime
  vexa   Japanese audio anime with subtitles

Usage:
  python smooth.py --id 30984 --type tv --s 2 --e 48 --server vexa --title "Bleach TYBW S4E8"
  python smooth.py --id 95479 --type tv --s 1 --e 51 --server aster --dry-run

Requires: pip install requests wasmtime (via vivcrypto.py)
Keys are maintained by vivcrypto.py (auto-refresh, dynamic re-derive).
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from requests.adapters import HTTPAdapter

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vivcrypto as vc
from vivcrypto import VIV, HEADERS as H, UA

SERVERS = ("aster", "vexa")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True, help="tmdb id (movie/tv) or internal id (anime)")
    p.add_argument("--type", default="tv", choices=("movie", "tv"))
    p.add_argument("--s", default="1")
    p.add_argument("--e", default="1")
    p.add_argument("--server", default="vexa", choices=SERVERS,
                   help="aster = english dub, vexa = japanese audio + subs")
    p.add_argument("--title", default="Vivarium")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--quality", default="best",
                   help="best|1080p|720p|480p|auto: rendition to mirror. "
                        "Lower = half the bytes, ~2x faster cache fill.")
    p.add_argument("--sub-delay", type=float, default=0.0,
                   help="subtitle delay in seconds (+ late, - early). "
                        "Also adjustable live in mpv with z/Z.")
    p.add_argument("--dry-run", action="store_true",
                   help="build the local playlist and exit (no mpv)")
    return p.parse_args()


ARGS = parse_args()
CACHE = os.path.join(tempfile.gettempdir(), "opencode",
                     f"viv_{ARGS.id}_{ARGS.type}_s{ARGS.s}_e{ARGS.e}_{ARGS.server}")
os.makedirs(CACHE, exist_ok=True)
for f in os.listdir(CACHE):
    if f.endswith((".part", ".log", ".json")):
        try:
            os.remove(os.path.join(CACHE, f))
        except OSError:
            pass
# seg*.m4s / init.mp4 / en.vtt persist: seeks stay on local disk (ms).

T0 = time.time()


def newsess(n=20):
    s = requests.Session()
    s.headers.update(H)
    s.cookies.update({"vg": vc.VG})
    s.mount("https://", HTTPAdapter(pool_connections=n, pool_maxsize=n, max_retries=2))
    s.mount("http://", HTTPAdapter(pool_connections=n, pool_maxsize=n, max_retries=2))
    return s


S = vc.S  # shared keep-alive session, live vg cookie, pooled nonces


def sign(path_qs):
    return vc.sign(path_qs)


sign.date = None


def is_dub(s):
    return vc.is_dub(s)


# ---- background: RACE all sub-servers in parallel, fastest link wins ----
# /api/es fans out to every provider concurrently and streams back one
# `source` event per sub-server as each answers. First event that fits the
# foreground server (aster=dub / vexa=subbed) wins the whole playback.
if ARGS.type == "movie":
    qs = f"/api/es?id={ARGS.id}&type=movie"
else:
    qs = f"/api/es?id={ARGS.id}&type={ARGS.type}&s={ARGS.s}&e={ARGS.e}"


def wants(s):
    q = s.get("quality") or ""
    if s.get("type") != "hls":
        return False
    if ARGS.server == "aster":
        return "dub" in q.lower()
    return "dub" not in q.lower() and bool(s.get("subs"))


def race_pick(timeout=25):
    rr = S.get(f"{VIV}{qs}", headers=sign(qs), timeout=timeout, stream=True)
    buf = ""
    try:
        for chunk in rr.iter_content(chunk_size=1024, decode_unicode=True):
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
                        if wants(s):
                            return s
    finally:
        rr.close()
    return None


pick = race_pick()
assert pick, f"no {ARGS.server} stream answered the race"
print(f"race won by {pick['provider']} {pick['quality']} {(time.time()-T0)*1000:.0f}ms", flush=True)


def cue_count(body: bytes) -> int:
    txt = body.decode("utf-8", errors="replace")
    return len(re.findall(r"\d{2}:\d{2}[.:]\d{2,3}\s*-->\s*\d{2}:\d{2}[.:]\d{2,3}", txt))


def fetch_en_sub(s):
    """Download + verify the english sub of one stream. Returns (cues, url, body)."""
    subs = s.get("subs", []) or []
    en = next((x for x in subs if x.get("lang") == "en"), None)
    if not en:
        return (0, None, None)
    for attempt in range(3):
        try:
            r = S.get(en["url"], headers=mh, timeout=20)
            r.raise_for_status()
            n = cue_count(r.content)
            if n > 0:
                return (n, en["url"], r.content)
        except Exception:
            time.sleep(1)
    return (0, None, None)


mh = {"User-Agent": UA, "Referer": f"{VIV}/", "Origin": VIV}
# Winner's subs ride on the same stream object (same provider, same
# offsets) — verify with one quick download instead of re-racing.
sub_body, sub_label = None, None
n, _, body = fetch_en_sub(pick)
if n > 0:
    sub_body, sub_label = body, "English"
elif ARGS.server == "vexa":
    # Winner's sub link died: fall back to the full background list and
    # take the richest verified english track.
    eqs = qs.replace("/api/es", "/api/e", 1)
    data = S.get(f"{VIV}{eqs}", headers=sign(eqs), timeout=40).json()
    scored = []
    for s in data.get("streams", []) or []:
        if wants(s):
            m, _, b = fetch_en_sub(s)
            scored.append((m, s.get("rank", 0), s, b))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    assert scored and scored[0][0] > 0, "vexa streams list subs but none downloadable"
    n, pick, sub_body = scored[0][0], scored[0][2], scored[0][3]
    sub_label = "English"
    print(f"fallback pick {pick['provider']} {pick['quality']} subs={n} cues", flush=True)
print(f"foreground server={ARGS.server} pick {pick['provider']} {pick['quality']} {(time.time()-T0)*1000:.0f}ms", flush=True)
master = S.get(pick["url"], headers=mh, timeout=20).text
renditions = []
for attrs, url in re.findall(r"#EXT-X-STREAM-INF:([^\n]*)\n(https?://[^\s\"']+)", master):
    m = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
    n = re.search(r'NAME="([^"]+)"', attrs)
    b = re.search(r"BANDWIDTH=(\d+)", attrs)
    renditions.append({"name": n.group(1) if n else "?",
                       "width": int(m.group(1)) if m else 0,
                       "height": int(m.group(2)) if m else 0,
                       "bandwidth": int(b.group(1)) if b else 0,
                       "url": url})
print("renditions:", [(r["name"], f"{r['width']}x{r['height']}", r["bandwidth"]) for r in renditions], flush=True)


def choose_rendition(rs, want):
    rs = [r for r in rs if r["height"] > 0] or rs
    if not rs:
        return None
    if want == "best":
        return max(rs, key=lambda r: (r["height"], r["bandwidth"]))
    m = re.search(r"(\d{3,4})", want or "")
    if m:
        h = int(m.group(1))
        under = [r for r in rs if r["height"] <= h]
        return max(under or rs, key=lambda r: (r["height"], r["bandwidth"]))
    named = [r for r in rs if r["name"].lower() == want.lower()]
    if named:
        return max(named, key=lambda r: r["bandwidth"])
    return max(rs, key=lambda r: (r["height"], r["bandwidth"]))


chosen = choose_rendition(renditions, ARGS.quality)
vurl = chosen["url"] if chosen else pick["url"]
print(f"rendition {chosen['name']} {chosen['width']}x{chosen['height']} {(time.time()-T0)*1000:.0f}ms" if chosen else "single stream", flush=True)
variant = S.get(vurl, headers=mh, timeout=20).text
mapm = re.search(r'#EXT-X-MAP:URI="([^"]+)"', variant)
map_url = mapm.group(1) if mapm else None
durs = re.findall(r"#EXTINF:([\d.]+)", variant)
urls = [ln.strip() for ln in variant.split("\n")
        if ln.strip().startswith("http") and ln.strip() != (map_url or "")]
segs = list(zip(durs, urls))
print(f"variant {len(segs)} segs map={bool(map_url)} {(time.time()-T0)*1000:.0f}ms", flush=True)

subpath = None
if sub_body:
    subpath = os.path.join(CACHE, "en.vtt")
    open(subpath, "wb").write(sub_body)
    print(f"sub {sub_label} {len(sub_body)}B verified", flush=True)
elif ARGS.server == "vexa":
    raise SystemExit("vexa requires subtitles but none verified")

# ---- parallel mirror with seek-swarm ----
DL = newsess(20)
lock = threading.Lock()
prio = deque()
seq_next = [0]
max_cached = [-1]


def path_of(i):
    return os.path.join(CACHE, f"seg{i:04d}.m4s")


def dl_to(url, dest):
    rr = DL.get(url, timeout=30)
    rr.raise_for_status()
    tmp = dest + ".part"
    open(tmp, "wb").write(rr.content)
    os.replace(tmp, dest)


def fetch_seg(i):
    p = path_of(i)
    if os.path.exists(p):
        return True
    try:
        dl_to(segs[i][1], p)
        with lock:
            if i > max_cached[0]:
                max_cached[0] = i
        return True
    except Exception:
        return False


def fetch_init():
    p = os.path.join(CACHE, "init.mp4")
    if os.path.exists(p) or not map_url:
        return True
    try:
        dl_to(map_url, p)
        return True
    except Exception:
        return False


def worker():
    while True:
        with lock:
            i = prio.popleft() if prio else None
            if i is None:
                i = seq_next[0]
                seq_next[0] += 1
        if i >= len(segs):
            return
        fetch_seg(i)


for _ in range(ARGS.workers):
    threading.Thread(target=worker, daemon=True).start()


class Hdl(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/local.m3u8":
            lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:8",
                     "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                     "#EXT-X-INDEPENDENT-SEGMENTS"]
            if map_url:
                lines.append('#EXT-X-MAP:URI="/init.mp4"')
            for i, (dur, url) in enumerate(segs):
                lines.append(f"#EXTINF:{dur},")
                lines.append(f"/seg{i:04d}.m4s")
            lines.append("#EXT-X-ENDLIST")
            body = "\n".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.match(r"/seg(\d+)\.m4s", self.path)
        if m:
            i = int(m.group(1))
            if not os.path.exists(path_of(i)):
                with lock:
                    if i > max_cached[0] + 15:
                        for j in range(min(i + 30, len(segs)), i - 1, -1):
                            if not os.path.exists(path_of(j)):
                                prio.appendleft(j)
                t = time.time()
                for _ in range(150):
                    if os.path.exists(path_of(i)):
                        break
                    time.sleep(0.1)
                print(f"seek seg{i} {'HIT' if (os.path.exists(path_of(i)) and time.time()-t < 0.15) else 'miss'} {(time.time()-t)*1000:.0f}ms", flush=True)
            try:
                with open(path_of(i), "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/init.mp4":
            if not os.path.exists(os.path.join(CACHE, "init.mp4")):
                fetch_init()
            try:
                with open(os.path.join(CACHE, "init.mp4"), "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/en.vtt":
            body = open(subpath, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), Hdl)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
t1 = time.time()
fetch_init()
fetch_seg(0)
fetch_seg(1)
print(f"startup {(time.time()-t1)*1000:.0f}ms total {(time.time()-T0)*1000:.0f}ms", flush=True)
json.dump({"port": port, "segs": len(segs), "provider": pick["provider"],
           "server": ARGS.server},
          open(os.path.join(CACHE, "srv.json"), "w"))
if ARGS.dry_run:
    print(f"dry-run ok: playlist=http://127.0.0.1:{port}/local.m3u8 segs={len(segs)}", flush=True)
    raise SystemExit(0)
mpv = r"C:\Program Files\MPV Player\mpv.exe"
args = [mpv, f"--title={ARGS.title} [{ARGS.server}] smooth",
        f"--sub-file=http://127.0.0.1:{port}/en.vtt" if subpath else "--no-sub",
        f"--sub-delay={ARGS.sub_delay}",
        "--cache-secs=300", "--demuxer-max-bytes=800MiB", "--demuxer-readahead-secs=120",
        "--demuxer-lavf-o=probesize=10M,analyzeduration=10M",
        f"http://127.0.0.1:{port}/local.m3u8"]
p = subprocess.Popen(args)
print(f"mpv pid={p.pid} port={port} total {(time.time()-T0)*1000:.0f}ms", flush=True)
p.wait()
