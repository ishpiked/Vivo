"""Vivarium enc-dec style API (mirrors https://enc-dec.app/api/ which only supports cinejoy).

Envelope matches enc-dec.app: {"status": 200, "result": {...}}.

One-time deployment: no keys to rotate by hand. Crypto (VG cookie, U_HEX,
X_CV) is maintained by vivcrypto.py: VG auto-refreshes through a real
browser on expiry, U_HEX/X_CV are re-derived from the site's own JS in the
background. See GET /api/admin/status.

Endpoints:
  GET  /api/health-vivarium            -> providers (like wing.st/servers)
  GET  /api/servers                    -> FOREGROUND servers: Aster (english dub
                                          anime) + Vexa (japanese audio anime
                                          with English subtitles).
                                          All real providers still run in the
                                          background; only these two are shown.
  GET  /api/enc-vivarium?id=&type=&s=&e=
       -> {"path","headers","cookies"} signed request kit (like enc-cinejoy data+state).
          Requires the admin bearer password because the kit includes the VG cookie.
          Client then GETs https://vivarium.su<path> themselves.
  POST /api/dec-vivarium  {"response": <raw /api/e JSON>, "dub": bool, "provider": str|None, "server": "aster"|"vexa"|None}
       -> {"streams":[...],"subtitles":[...],"qualities":[...]} filtered (like dec-cinejoy).
  GET  /api/vivarium?url=... or ?id=&type=&s=&e=&dub=&provider=&server=&race=
       -> one-shot convenience: race=true (default) returns the first matching
          HLS source from the parallel provider event stream. race=false fetches
          the complete list when all qualities/sources are required.
  GET  /api/admin/status               -> key sources, vg expiry, bootstrap state (protected)
  POST /api/admin/vg {"vg": "..."}     -> hot-swap VG cookie (protected; no restart)

Run: uvicorn api:app --port 8000
Requires: pip install fastapi uvicorn wasmtime requests
Optional for VG auto-refresh: pip install seleniumbase (needs Chrome)
"""
import asyncio
import datetime
import hmac
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

import vivcrypto as vc
from vivcrypto import VIV, HEADERS

# ============================================================================
# Legacy curated anime metadata. Live link resolution uses Vivarium's
# /api/cours mapping instead of assuming this table covers every title.
# ============================================================================

VIVARIUM_ID_MAP = {
    # Confirmed 1:1 mappings from research
    30984: {
        "tmdb_id": 30984,
        "title": "Bleach",
        "type": "tv",
        "title_romaji": "Bleach",
        "seasons_tmdb": 3,  # Season 1: 366 eps, Season 2: 50 eps (TYBW), Season 3:?
        "description": "English dub anime with long-running series",
    },
    635302: {
        "tmdb_id": 635302,
        "title": "Demon Slayer Movie",
        "type": "movie",
        "title_romaji": "Kimetsu no Yaiba",
        "description": "Demon Slayer movie (Mugen Train arc)",
    },
    37854: {
        "tmdb_id": 37854,
        "title": "One Piece",
        "type": "tv",
        "title_romaji": "One Piece",
        "seasons_tmdb": 23,  # 23+ seasons, highly fragmented
        "description": "Longest-running anime with 23+ TMDB seasons",
    },
    61663: {
        "tmdb_id": 61663,
        "title": "Your Lie in April",
        "type": "tv",
        "title_romaji": "Yahari",
        "seasons_tmdb": 1,  # Simple: 1 main season + 1 special (Season 0)
        "description": "Romance drama anime, 22 episodes",
    },
    46260: {
        "tmdb_id": 46260,
        "title": "Naruto",
        "type": "tv",
        "title_romaji": "Naruto",
        "seasons_tmdb": 4,  # TMDB has 4 seasons; TVDB has 5 with different splits
        "description": "Original Naruto series, 220 episodes + specials",
    },
}


def map_vivarium_to_tmdb(vivarium_id: int) -> dict:
    """Map a Vivarium internal ID to TMDB metadata.
    
    Args:
        vivarium_id: The Vivarium internal show ID
        
    Returns:
        dict with tmdb_id, title, type, seasons_tmdb, and other metadata
        None if ID not in mapping table
    """
    return VIVARIUM_ID_MAP.get(vivarium_id)


def get_api_params(vivarium_id: int, season: Optional[str] = None, 
                   episode: Optional[str] = None, dub: bool = False) -> dict:
    """Get API parameters for /api/vivarium endpoint.
    
    Args:
        vivarium_id: Vivarium internal show ID
        season: Season number (optional, defaults to "1")
        episode: Episode number (optional, defaults to "1")
        dub: Whether to request English dub audio
        
    Returns:
        dict with {id, type, s, e} parameters for API call
    """
    mapping = map_vivarium_to_tmdb(vivarium_id)
    if not mapping:
        return None
    
    content_type = mapping["type"]
    
    if content_type == "movie":
        return {"id": mapping["tmdb_id"], "type": "movie"}
    
    # For TV type - use provided season/episode or defaults
    s = season if season else "1"
    e = episode if episode else "1"
    
    return {
        "id": mapping["tmdb_id"],
        "type": "tv",
        "s": s,
        "e": e,
    }


def get_stream_server_filters(dub: bool = False, server: Optional[str] = None) -> dict:
    """Get stream filtering parameters based on dub/sub preference.
    
    Args:
        dub: Whether to filter for English dub
        server: Server filter ("aster" for dub, "vexa" for sub, None for both)
        
    Returns:
        dict suitable for passing to filter_streams() or API params
    """
    from api import classify_streams, filter_streams, is_dub, has_english_subtitles
    
    # The actual filtering is done in the API endpoints
    # This function provides the logic description
    if server == "aster":
        return {"server_key": "aster"}
    elif server == "vexa":
        return {"server_key": "vexa"}
    elif dub:
        return {"dub": True}
    else:
        return {"dub": False}


# Keep reference to these for endpoint use
__all__ = [
    "VIVARIUM_ID_MAP",
    "map_vivarium_to_tmdb", 
    "get_api_params",
    "get_stream_server_filters",
]

_LOG = logging.getLogger("vivarium.performance")
_HEALTH_LOG = logging.getLogger("vivarium.health")
_HEALTH_LOG.setLevel(logging.INFO)
_HEALTH_LOG.propagate = False
if not _HEALTH_LOG.handlers:
    _health_handler = logging.StreamHandler()
    _health_handler.setFormatter(logging.Formatter("%(message)s"))
    _HEALTH_LOG.addHandler(_health_handler)
_CACHE = OrderedDict()  # (id,type,s,e,server,dub,provider,race) -> (at, result)
_CACHE_TTL = 30
_UPSTREAM_CACHE = OrderedDict()  # path -> (at, payload)
_UPSTREAM_CACHE_TTL = 30
_CACHE_LIMIT = 512
_CACHE_LOCK = threading.Lock()
_METRICS_LOCK = threading.Lock()
_STARTED_AT = time.time()
_METRICS = {
    "requests_total": 0,
    "status_codes": {},
    "paths": {},
    "total_latency_ms": 0.0,
    "max_latency_ms": 0.0,
    "last_request_at": None,
    "lookups_total": 0,
    "empty_lookups": 0,
}


def sign_request(path_qs: str):
    return path_qs, vc.sign(path_qs), {"vg": vc.VG}


def require_admin(request: Request):
    password = os.environ.get("VIVARIUM_ADMIN_PASSWORD")
    if not password:
        raise HTTPException(
            status_code=503, detail="Admin access is not configured")

    scheme, separator, credential = request.headers.get(
        "authorization", "").partition(" ")
    if (not separator or scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credential.strip().encode("utf-8"),
                password.encode("utf-8"))):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing admin password",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _cache_get(cache, key, ttl):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = cache.get(key)
        if entry is None:
            return None
        if now - entry[0] >= ttl:
            del cache[key]
            return None
        cache.move_to_end(key)
        return entry[1]


def _cache_put(cache, key, value):
    with _CACHE_LOCK:
        cache[key] = (time.monotonic(), value)
        cache.move_to_end(key)
        while len(cache) > _CACHE_LIMIT:
            cache.popitem(last=False)


def _log_timing(stages):
    _LOG.info("scrape_timing_ms %s", " ".join(
        f"{name}={value:.2f}" for name, value in stages.items()))


def _record_lookup(empty: bool):
    with _METRICS_LOCK:
        _METRICS["lookups_total"] += 1
        if empty:
            _METRICS["empty_lookups"] += 1


def _usable_hls_stream(stream: dict) -> bool:
    url = stream.get("url")
    if str(stream.get("type") or "").lower() != "hls" or not isinstance(url, str):
        return False
    return url.strip().lower().startswith(("http://", "https://"))


def _no_sources_response():
    return JSONResponse(status_code=404, content={
        "status": 404, "result": "",
        "error": "No usable HLS sources found",
        "code": "no_sources",
        "hint": "Try again later or check /api/health-vivarium for provider status.",
    })


class AdminVgBody(BaseModel):
    vg: Optional[str] = None


def build_qs(id: str, type: str, s: Optional[str], e: Optional[str]) -> str:
    if type not in ("movie", "tv"):
        raise ValueError("type must be movie|tv")
    if type == "movie":
        return f"/api/e?{urlencode({'id': id, 'type': 'movie'})}"
    if not s or not e:
        raise ValueError("tv requires s and e")
    return f"/api/e?{urlencode({'id': id, 'type': type, 's': s, 'e': e})}"


def _link_query_value(query: dict, key: str) -> Optional[str]:
    values = query.get(key)
    return values[-1] if values else None


def parse_media_url(media_url: str) -> dict:
    """Parse a TMDB episode/movie URL or a Vivarium show URL."""
    try:
        parsed = urlsplit(media_url)
    except ValueError as ex:
        raise ValueError("Invalid media URL") from ex
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP(S) media link")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in media URLs")

    host = parsed.hostname.lower()
    segments = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if host in ("themoviedb.org", "www.themoviedb.org", "m.themoviedb.org"):
        route_index = next(
            (index for index, part in enumerate(segments)
             if part in ("movie", "tv")),
            None)
        if route_index is None or route_index + 1 >= len(segments):
            raise ValueError("TMDB URL must contain /movie/<id> or /tv/<id>")
        content_type = segments[route_index]
        id_match = re.match(r"^(\d+)(?:-|$)", segments[route_index + 1])
        if not id_match:
            raise ValueError("TMDB URL does not contain a numeric media id")
        season = _link_query_value(query, "s")
        episode = _link_query_value(query, "e")
        if content_type == "tv":
            for index, part in enumerate(segments[route_index + 2:],
                                         start=route_index + 2):
                if part == "season" and index + 1 < len(segments):
                    season = segments[index + 1]
                elif part == "episode" and index + 1 < len(segments):
                    episode = segments[index + 1]
        return {
            "id": id_match.group(1), "type": content_type,
            "s": season, "e": episode,
            "a": _link_query_value(query, "a"),
        }

    if host not in ("vivarium.su", "www.vivarium.su"):
        raise ValueError("URL host must be themoviedb.org or vivarium.su")
    if len(segments) < 2 or segments[0] not in ("s", "m"):
        raise ValueError("Vivarium URL must use /s/<slug-id> or /m/<slug-id>")
    id_match = re.search(r"-(\d+)$", segments[1])
    if not id_match:
        raise ValueError("Vivarium URL does not contain a numeric media id")
    return {
        "id": id_match.group(1),
        "type": "tv" if segments[0] == "s" else "movie",
        "s": _link_query_value(query, "s"),
        "e": _link_query_value(query, "e"),
        "a": _link_query_value(query, "a"),
    }


async def resolve_media_url(media_url: str, season: Optional[str],
                            episode: Optional[str], client) -> dict:
    """Resolve URL parameters to the TMDB coordinates Vivarium's API expects."""
    media = parse_media_url(media_url)
    media["s"] = season or media["s"]
    media["e"] = episode or media["e"]
    if media["type"] == "movie":
        return media

    if (media["s"] is not None
            and (not re.fullmatch(r"\d+", media["s"])
                 or int(media["s"]) < 0)):
        raise ValueError("Season number (s) must be a non-negative integer")
    if (media["e"] is not None
            and (not re.fullmatch(r"\d+", media["e"])
                 or int(media["e"]) < 1)):
        raise ValueError("Episode number (e) must be a positive integer")

    ani_id = media["a"]
    if ani_id and not media["s"]:
        if not re.fullmatch(r"\d+", ani_id):
            raise ValueError("AniList id (a) must be numeric")
        if not media["e"]:
            raise ValueError("TV links require an episode number (e)")

        try:
            response = await client.get(
                f"{VIV}/api/cours", params={"id": media["id"]},
                timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5))
        except httpx.HTTPError as ex:
            raise RuntimeError(f"Vivarium course lookup failed: {ex}") from ex
        if response.status_code != 200:
            raise RuntimeError(
                f"Vivarium course lookup failed: {response.status_code}")
        try:
            payload = response.json()
        except ValueError as ex:
            raise RuntimeError("Vivarium returned invalid course data") from ex
        courses = payload.get("cours") if isinstance(payload, dict) else None
        if not isinstance(courses, list):
            raise RuntimeError("Vivarium returned invalid course data")
        course = next(
            (item for item in courses
             if isinstance(item, dict) and str(item.get("al")) == ani_id),
            None)
        if course is None:
            raise ValueError(
                f"AniList id {ani_id} is not mapped to this Vivarium title")

        remaining = int(media["e"])
        ranges = course.get("r")
        if not isinstance(ranges, list):
            raise RuntimeError("Vivarium returned invalid episode mapping data")
        for episode_range in ranges:
            if (not isinstance(episode_range, list) or len(episode_range) != 3
                    or not all(str(value).isdigit()
                               for value in episode_range)):
                raise RuntimeError("Vivarium returned invalid episode mapping data")
            tmdb_season, first_episode, last_episode = map(int, episode_range)
            episode_count = last_episode - first_episode + 1
            if tmdb_season < 0 or first_episode < 1 or episode_count < 1:
                raise RuntimeError("Vivarium returned invalid episode mapping data")
            if remaining <= episode_count:
                media["s"] = str(tmdb_season)
                media["e"] = str(first_episode + remaining - 1)
                return media
            remaining -= episode_count
        raise ValueError(
            f"Episode {media['e']} is outside AniList id {ani_id}'s episode range")

    return media


# FOREGROUND servers. Only these two are shown; every real provider
# (Febbox, Hermes, ...) is still fetched in the background and its streams
# are classified into one of these two.
SERVERS = [
    {"server": "Aster", "audio": "dub",
     "description": "English dub anime"},
    {"server": "Vexa", "audio": "japanese",
     "description": "Japanese audio anime with English subtitles"},
]


def is_dub(s: dict) -> bool:
    return "dub" in (s.get("quality") or "").lower()


def has_english_subtitles(stream: dict) -> bool:
    subtitles = stream.get("subs")
    if not isinstance(subtitles, list):
        return False
    for subtitle in subtitles:
        if not isinstance(subtitle, dict):
            continue
        labels = " ".join(str(subtitle.get(key) or "")
                          for key in ("lang", "language", "label"))
        if re.search(r"\b(?:english|eng|en)\b", labels, re.I):
            return True
    return False


def classify_streams(data: dict):
    """Split background provider streams into the two foreground servers."""
    streams = data.get("streams", []) or []
    aster = sorted([x for x in streams if is_dub(x)],
                   key=lambda x: x.get("rank", 0), reverse=True)
    vexa = sorted([x for x in streams
                   if not is_dub(x) and has_english_subtitles(x)],
                  key=lambda x: (bool(x.get("subs")), x.get("rank", 0)),
                  reverse=True)
    return {"aster": aster, "vexa": vexa}


def filter_streams(data: dict, dub: bool = False, provider: Optional[str] = None,
                   server: Optional[str] = None):
    streams = data.get("streams", []) or []
    server_key = (server or "").lower()
    provider_key = (provider or "").lower()

    filtered = []
    for item in streams:
        if server_key == "aster" and not is_dub(item):
            continue
        if server_key == "vexa" and (
                is_dub(item) or not has_english_subtitles(item)):
            continue
        elif not server_key and dub and not is_dub(item):
            continue
        if provider_key and (item.get("provider") or "").lower() != provider_key:
            continue
        filtered.append(item)

    if server_key == "vexa":
        filtered.sort(key=lambda x: (bool(x.get("subs")), x.get("rank", 0)), reverse=True)
    return {"streams": filtered, "subtitles": data.get("subtitles", []),
            "qualities": quality_list(filtered)}


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
    if str(s.get("type") or "").lower() != "hls":
        return False
    if server_or_dub in ("aster", True):
        return is_dub(s)
    if server_or_dub in ("vexa",):
        return not is_dub(s) and has_english_subtitles(s)
    return True


async def _cached_upstream(client, path_qs: str, timings: dict):
    cache_started = time.perf_counter()
    payload = _cache_get(_UPSTREAM_CACHE, path_qs, _UPSTREAM_CACHE_TTL)
    timings["upstream_cache_lookup"] = (time.perf_counter() - cache_started) * 1000
    if payload is not None:
        return payload

    _, headers, cookies = await asyncio.to_thread(sign_request, path_qs)
    started = time.perf_counter()
    response = await client.get(
        f"{VIV}{path_qs}", headers=headers, cookies=cookies,
        timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5))
    timings["request"] = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        raise RuntimeError(f"Upstream fetch failed: {response.status_code}")

    started = time.perf_counter()
    payload = response.json()
    timings["parse"] = (time.perf_counter() - started) * 1000
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    if any(isinstance(stream, dict) and _usable_hls_stream(stream)
           for stream in streams or []):
        _cache_put(_UPSTREAM_CACHE, path_qs, payload)
    return payload


async def race_streams(client, path_qs: str, server_or_dub, provider, timings: dict):
    """Parallel sub-server race: /api/es fans out to every provider at once,
    first `source` event fitting the foreground server wins the playback."""
    import json
    es_qs = path_qs.replace("/api/e", "/api/es", 1)
    _, headers, cookies = await asyncio.to_thread(sign_request, es_qs)
    request_started = time.perf_counter()
    async with client.stream(
            "GET", f"{VIV}{es_qs}", headers=headers, cookies=cookies) as response:
        timings["request"] = (time.perf_counter() - request_started) * 1000
        if response.status_code != 200:
            raise RuntimeError(f"Upstream search failed: {response.status_code}")

        event_name = ""
        data_lines = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line:
                if event_name == "done":
                    timings["search"] = (
                        time.perf_counter() - request_started) * 1000
                    return None
                if event_name == "source" and data_lines:
                    started = time.perf_counter()
                    try:
                        data = json.loads("\n".join(data_lines))
                    except (json.JSONDecodeError, TypeError):
                        data = {}
                    timings["parse"] = timings.get("parse", 0.0) + (
                        time.perf_counter() - started) * 1000

                    started = time.perf_counter()
                    streams = data.get("streams") if isinstance(data, dict) else None
                    for stream in streams or []:
                        if (isinstance(stream, dict)
                                and wants_for(server_or_dub, stream)
                                and _usable_hls_stream(stream)
                                and (not provider or
                                     (stream.get("provider") or "").lower()
                                     == provider.lower())):
                            timings["extract"] = (time.perf_counter() - started) * 1000
                            timings["search"] = (
                                time.perf_counter() - request_started) * 1000
                            return stream
                    timings["extract"] = timings.get("extract", 0.0) + (
                        time.perf_counter() - started) * 1000
                event_name = ""
                data_lines = []
    timings["search"] = (time.perf_counter() - request_started) * 1000
    return None


@asynccontextmanager
async def lifespan(app):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=40,
                          keepalive_expiry=30)
    timeout = httpx.Timeout(connect=5, read=25, write=5, pool=5)
    async with httpx.AsyncClient(
            http2=True, limits=limits, timeout=timeout, headers=HEADERS) as client:
        app.state.http = client
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def profile_request(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    with _METRICS_LOCK:
        _METRICS["requests_total"] += 1
        status = str(response.status_code)
        _METRICS["status_codes"][status] = (
            _METRICS["status_codes"].get(status, 0) + 1)
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        _METRICS["paths"][path] = _METRICS["paths"].get(path, 0) + 1
        _METRICS["total_latency_ms"] += elapsed_ms
        _METRICS["max_latency_ms"] = max(_METRICS["max_latency_ms"], elapsed_ms)
        _METRICS["last_request_at"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if request.url.path == "/health":
        _HEALTH_LOG.info(
            "health_probe timestamp=%s path=%s status=%d latency_ms=%.2f cf_ray=%s",
            datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            request.url.path,
            response.status_code,
            elapsed_ms,
            request.headers.get("cf-ray", "-"),
        )
    if request.url.path == "/api/vivarium":
        _LOG.info("scrape_response_ms %.2f", elapsed_ms)
    return response
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
async def health():
    return {"status": "healthy"}

@app.get("/api/servers")
def servers():
    return {"status": 200, "result": {"servers": SERVERS}}


@app.get("/api/health-vivarium")
async def health_vivarium(request: Request):
    try:
        response = await request.app.state.http.get(
            f"{VIV}/api/health", timeout=httpx.Timeout(
                connect=5, read=15, write=5, pool=5))
    except httpx.HTTPError as ex:
        return {"status": 502, "result": "", "error": f"Upstream health failed: {ex}"}
    if response.status_code != 200:
        return {"status": response.status_code, "result": "", "error": "Upstream health failed"}
    return {"status": 200, "result": response.json()}


@app.get("/api/enc-vivarium", dependencies=[Depends(require_admin)])
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
    result = filter_streams(body.response, body.dub, body.provider, body.server)
    result["streams"] = [stream for stream in result["streams"]
                         if _usable_hls_stream(stream)]
    result["qualities"] = quality_list(result["streams"])
    if not result["streams"]:
        return _no_sources_response()
    return {"status": 200, "result": result}


@app.get("/api/vivarium")
async def vivarium(
    request: Request,
    id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    url: Optional[str] = Query(None),
    s: Optional[str] = Query(None),
    e: Optional[str] = Query(None),
    dub: bool = Query(False),
    provider: Optional[str] = Query(None),
    server: Optional[str] = Query(None),
    race: bool = Query(True),
):
    if server and server.lower() not in ("aster", "vexa"):
        return {"status": 400, "result": "", "error": "Invalid server",
                "hint": "server must be aster|vexa"}
    if url and (id or type):
        return {"status": 400, "result": "",
                "error": "Use either url or id and type, not both"}
    if url:
        try:
            media = await resolve_media_url(
                url, s, e, request.app.state.http)
        except ValueError as ex:
            return {"status": 400, "result": "", "error": str(ex)}
        except RuntimeError as ex:
            return {"status": 502, "result": "", "error": str(ex)}
        id, type, s, e = (
            media["id"], media["type"], media["s"], media["e"])
    elif not id or not type:
        return {"status": 400, "result": "", "error": "Expected query: id, type",
                "hint": "GET: /api/vivarium?url=[TMDB_or_Vivarium_link] or /api/vivarium?id=[tmdb_or_internal]&type=[movie|tv]&s=[season]&e=[episode]&server=[aster|vexa]"}
    try:
        path = build_qs(id, type, s, e)
    except ValueError as ex:
        return {"status": 400, "result": "", "error": str(ex)}
    key = (id, type, s, e, (server or "").lower(), bool(dub),
           (provider or "").lower(), bool(race))
    cache_started = time.perf_counter()
    res = _cache_get(_CACHE, key, _CACHE_TTL)
    cache_lookup_ms = (time.perf_counter() - cache_started) * 1000
    if res is not None:
        if not res.get("streams"):
            with _CACHE_LOCK:
                _CACHE.pop(key, None)
            _record_lookup(True)
            return _no_sources_response()
        started = time.perf_counter()
        res = dict(res)
        res["cached"] = True
        result = {"status": 200, "result": res}
        _record_lookup(not bool(res.get("streams")))
        _log_timing({"cache_lookup": cache_lookup_ms,
                     "response_generation": (time.perf_counter() - started) * 1000})
        return result
    timings = {"cache_lookup": cache_lookup_ms}
    client = request.app.state.http
    if race:
        try:
            won = await race_streams(
                client, path, server.lower() if server else (True if dub else None),
                provider, timings)
        except (httpx.HTTPError, RuntimeError) as ex:
            return {"status": 502, "result": "", "error": str(ex)}
        if won:
            started = time.perf_counter()
            res = filter_streams({"streams": [won], "subtitles": []}, dub, provider, server)
            res["streams"] = [stream for stream in res["streams"]
                              if _usable_hls_stream(stream)]
            res["qualities"] = quality_list(res["streams"])
            if not res["streams"]:
                _record_lookup(True)
                return _no_sources_response()
            timings["source_extraction"] = (time.perf_counter() - started) * 1000
            _cache_put(_CACHE, key, res)
            _record_lookup(False)
            started = time.perf_counter()
            result = {"status": 200, "result": res}
            timings["response_generation"] = (time.perf_counter() - started) * 1000
            _log_timing(timings)
            return result
        _record_lookup(True)
        _log_timing(timings)
        return _no_sources_response()
    try:
        payload = await _cached_upstream(client, path, timings)
    except (RuntimeError, httpx.HTTPError) as ex:
        return {"status": 502, "result": "", "error": str(ex)}
    started = time.perf_counter()
    res = filter_streams(payload, dub, provider, server)
    res["streams"] = [stream for stream in res["streams"]
                      if _usable_hls_stream(stream)]
    res["qualities"] = quality_list(res["streams"])
    if not res["streams"]:
        _record_lookup(True)
        return _no_sources_response()
    timings["source_extraction"] = (time.perf_counter() - started) * 1000
    _cache_put(_CACHE, key, res)
    _record_lookup(False)
    started = time.perf_counter()
    result = {"status": 200, "result": res}
    timings["response_generation"] = (time.perf_counter() - started) * 1000
    _log_timing(timings)
    return result


@app.get("/api/status")
def api_status():
    with _METRICS_LOCK:
        request_count = _METRICS["requests_total"]
        avg_latency = (_METRICS["total_latency_ms"] / request_count
                       if request_count else 0.0)
        runtime = {
            "started_at": datetime.datetime.fromtimestamp(
                _STARTED_AT, datetime.timezone.utc).isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
            "uptime_seconds": round(time.time() - _STARTED_AT, 2),
            "last_request_at": _METRICS["last_request_at"],
            "requests_total": request_count,
            "average_latency_ms": round(avg_latency, 2),
            "max_latency_ms": round(_METRICS["max_latency_ms"], 2),
            "lookups_total": _METRICS["lookups_total"],
            "empty_lookups": _METRICS["empty_lookups"],
        }
    with _CACHE_LOCK:
        cache = {"response_entries": len(_CACHE),
                 "upstream_entries": len(_UPSTREAM_CACHE)}
    with vc._NONCES_LOCK:
        nonce_count = len(vc._NONCES)
    signing_ready = bool(vc.U_HEX and vc.X_CV and vc._exp is not None)
    vg_expiry = vc._VG_EXPIRY["at"]
    if vg_expiry and vg_expiry <= time.time():
        signing_ready = False
    return {"status": 200, "result": {
        "service": "vivarium-api",
        "state": "ready" if signing_ready else "degraded",
        "runtime": runtime,
        "cache": cache,
        "crypto": {
            "vg_source": vc.KEY_SOURCE["vg"],
            "vg_expiry_utc": datetime.datetime.fromtimestamp(
                vg_expiry, datetime.timezone.utc).isoformat(timespec="seconds")
                if vg_expiry else None,
            "crypto_source": vc.KEY_SOURCE["crypto"],
            "wasm_ready": vc._exp is not None,
            "signing_ready": signing_ready,
            "nonce_pool_size": nonce_count,
            "bootstrap_running": vc._BOOT["running"],
            "bootstrap_ok": vc._BOOT["ok"],
        },
    }}


@app.get("/api/status/metrics")
def api_status_metrics():
    with _METRICS_LOCK:
        request_count = _METRICS["requests_total"]
        return {"status": 200, "result": {
            "requests_total": request_count,
            "status_codes": dict(_METRICS["status_codes"]),
            "paths": dict(_METRICS["paths"]),
            "average_latency_ms": round(
                _METRICS["total_latency_ms"] / request_count, 2)
                if request_count else 0.0,
            "max_latency_ms": round(_METRICS["max_latency_ms"], 2),
            "last_request_at": _METRICS["last_request_at"],
            "lookups_total": _METRICS["lookups_total"],
            "empty_lookups": _METRICS["empty_lookups"],
        }}


@app.get("/api/status/cache")
def api_status_cache():
    with _CACHE_LOCK:
        return {"status": 200, "result": {
            "response_entries": len(_CACHE),
            "upstream_entries": len(_UPSTREAM_CACHE),
            "entry_limit": _CACHE_LIMIT,
            "response_ttl_seconds": _CACHE_TTL,
            "upstream_ttl_seconds": _UPSTREAM_CACHE_TTL,
        }}


@app.get("/api/status/crypto")
def api_status_crypto():
    expiry = vc._VG_EXPIRY["at"]
    with vc._NONCES_LOCK:
        nonce_count = len(vc._NONCES)
    return {"status": 200, "result": {
        "vg_source": vc.KEY_SOURCE["vg"],
        "vg_expiry_utc": datetime.datetime.fromtimestamp(
            expiry, datetime.timezone.utc).isoformat(timespec="seconds")
            if expiry else None,
        "crypto_source": vc.KEY_SOURCE["crypto"],
        "wasm_ready": vc._exp is not None,
        "wasm_url": vc.WASM_URL,
        "nonce_pool_size": nonce_count,
        "bootstrap": {
            "running": vc._BOOT["running"],
            "ok": vc._BOOT["ok"],
            "last": vc._BOOT["last"],
        },
    }}


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status():
    exp = vc._VG_EXPIRY["at"]
    return {"status": 200, "result": {
        "vg_source": vc.KEY_SOURCE["vg"],
        "vg_expiry_utc": (datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat()
                          if exp else None),
        "crypto_source": vc.KEY_SOURCE["crypto"],
        "crypto_bootstrap": vc._BOOT,
        "wasm_url": vc.WASM_URL,
    }}


@app.post("/api/admin/vg", dependencies=[Depends(require_admin)])
def admin_vg(body: AdminVgBody):
    if not body.vg or len(body.vg.strip()) < 20:
        return {"status": 400, "result": "", "error": "Expected body: vg",
                "hint": "POST: { 'vg': '<vg cookie value from browser>' }"}
    vc.set_vg(body.vg, "admin")
    ok = vc.ensure_vg()
    return {"status": 200 if ok else 502,
            "result": {"vg_source": vc.KEY_SOURCE["vg"], "usable": ok}}
